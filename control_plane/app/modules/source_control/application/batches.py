from dataclasses import dataclass
from typing import Any, Literal

from control_plane.app.modules.source_control.application._batch_claim import InboxClaimLost
from control_plane.app.modules.source_control.application._integration_common import (
    CREATE_TOPIC,
    MERGE_TOPIC,
)
from control_plane.app.modules.source_control.application.delivery_relay import (
    relay_integration_delivery_requests,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.application.integration import (
    process_integration_merge_candidate,
    process_integration_mr_candidate,
)
from control_plane.app.modules.source_control.application.integration_reconciliation import (
    reconcile_due_integration_effects,
)
from control_plane.app.modules.source_control.application.reconciliation import (
    process_webhook_candidate,
    reconcile_due_effects,
)
from control_plane.app.modules.source_control.application.relay import (
    relay_binding_requests,
)
from control_plane.app.modules.source_control.application.saga import (
    process_binding_candidate,
)
from control_plane.app.modules.source_control.domain import (
    SourceControlBatchResult,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason


@dataclass(frozen=True, slots=True)
class _ProcessCandidate:
    lane: Literal["binding", "delivery", "webhook"]
    identifier: str
    topic: str | None = None


def _lane_limits(limit: int, lane_count: int) -> tuple[int, ...]:
    quotient, remainder = divmod(limit, lane_count)
    return tuple(quotient + (1 if index < remainder else 0) for index in range(lane_count))


def _safe_error_codes(values: list[str | None]) -> tuple[str, ...]:
    allowed = {reason.value for reason in SourceControlReason}
    return tuple(value for value in values if value is not None and value in allowed)


def relay_due_source_control_requests(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> SourceControlBatchResult:
    if limit < 2:
        raise ValueError("Source Control relay limit must be at least two")
    binding_limit, delivery_limit = _lane_limits(limit, 2)
    binding = relay_binding_requests(limit=binding_limit, dependencies=dependencies)
    delivery = relay_integration_delivery_requests(
        limit=delivery_limit,
        dependencies=dependencies,
    )
    return SourceControlBatchResult(
        claimed=binding.claimed + delivery.claimed,
        processed=binding.accepted + delivery.accepted,
        released=binding.released + delivery.released,
    )


def _round_robin_candidates(
    lanes: tuple[list[_ProcessCandidate], ...],
    *,
    limit: int,
) -> tuple[_ProcessCandidate, ...]:
    selected: list[_ProcessCandidate] = []
    offsets = [0] * len(lanes)
    while len(selected) < limit:
        added = False
        for index, lane in enumerate(lanes):
            if len(selected) >= limit:
                break
            if offsets[index] >= len(lane):
                continue
            selected.append(lane[offsets[index]])
            offsets[index] += 1
            added = True
        if not added:
            break
    return tuple(selected)


def _pending_process_candidates(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> tuple[_ProcessCandidate, ...]:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    binding_limit, delivery_limit, webhook_limit = _lane_limits(limit, 3)
    with dependencies.engine.connect() as db:
        repository = dependencies.repository_factory(db)
        delivery_repository = repository_factory(db)
        binding = [
            _ProcessCandidate("binding", message_id)
            for message_id in repository.pending_binding_request_ids(
                limit=binding_limit,
                now=now,
            )[:binding_limit]
        ]
        delivery = [
            _ProcessCandidate(
                "delivery",
                str(row["message_id"]),
                str(row["topic"]),
            )
            for row in delivery_repository.pending_delivery_request_candidates(
                limit=delivery_limit,
                now=now,
            )[:delivery_limit]
        ]
        webhook = [
            _ProcessCandidate("webhook", inbox_id)
            for inbox_id in repository.pending_webhook_ids(limit=webhook_limit)[:webhook_limit]
        ]
    return _round_robin_candidates((binding, delivery, webhook), limit=limit)


def _result_facts(result: Any) -> tuple[str | None, str | None]:
    effect = result.effect
    return (
        None if effect is None else effect.id,
        result.blocked_reason,
    )


def process_due_source_control_inboxes(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> SourceControlBatchResult:
    if limit < 3:
        raise ValueError("Source Control process limit must be at least three")
    candidates = _pending_process_candidates(limit=limit, dependencies=dependencies)
    effect_ids: list[str] = []
    errors: list[str | None] = []
    processed = 0
    for candidate in candidates:
        result: Any
        try:
            if candidate.lane == "binding":
                result = process_binding_candidate(
                    message_id=candidate.identifier,
                    dependencies=dependencies,
                )
            elif candidate.lane == "delivery":
                if candidate.topic == CREATE_TOPIC:
                    result = process_integration_mr_candidate(
                        message_id=candidate.identifier,
                        dependencies=dependencies,
                    )
                elif candidate.topic == MERGE_TOPIC:
                    result = process_integration_merge_candidate(
                        message_id=candidate.identifier,
                        dependencies=dependencies,
                    )
                else:
                    raise SourceControlDependencyUnavailable(
                        "Delivery request operation is invalid"
                    )
            else:
                process_webhook_candidate(
                    candidate.identifier,
                    dependencies=dependencies,
                )
                processed += 1
                continue
        except InboxClaimLost:
            continue
        processed += 1
        effect_id, error_code = _result_facts(result)
        if effect_id is not None:
            effect_ids.append(effect_id)
        errors.append(error_code)
    return SourceControlBatchResult(
        claimed=processed,
        processed=processed,
        effect_ids=tuple(effect_ids),
        error_codes=_safe_error_codes(errors),
    )


def reconcile_due_source_control_effects(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> SourceControlBatchResult:
    if limit < 2:
        raise ValueError("Source Control reconciliation limit must be at least two")
    branch_limit, integration_limit = _lane_limits(limit, 2)
    branch = reconcile_due_effects(limit=branch_limit, dependencies=dependencies)
    integration = reconcile_due_integration_effects(
        limit=integration_limit,
        dependencies=dependencies,
    )
    effects = (*branch.effects, *integration.effects)
    return SourceControlBatchResult(
        claimed=len(effects),
        processed=len(effects),
        effect_ids=tuple(effect.id for effect in effects),
        error_codes=_safe_error_codes([effect.last_error_code for effect in effects]),
    )


__all__ = [
    "process_due_source_control_inboxes",
    "reconcile_due_source_control_effects",
    "relay_due_source_control_requests",
]
