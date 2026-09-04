from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from control_plane.app.modules.source_control.application._batch_claim import (
    InboxClaimLost,
    InboxProcessingFailed,
)
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
from control_plane.app.modules.source_control.application.evidence import (
    process_integration_baseline_candidate,
)
from control_plane.app.modules.source_control.application.evidence_relay import (
    relay_requirement_evidence_requests,
)
from control_plane.app.modules.source_control.application.formal import (
    process_formal_delivery_candidate,
    reconcile_due_formal_effects,
)
from control_plane.app.modules.source_control.application.formal_relay import (
    relay_requirement_formal_delivery_requests,
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
    EvidenceStale,
    EvidenceUnavailable,
    FormalDeliveryConflict,
    SourceControlBatchResult,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason


@dataclass(frozen=True, slots=True)
class _ProcessCandidate:
    lane: Literal["binding", "delivery", "webhook", "evidence", "formal"]
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
    if limit < 4:
        raise ValueError("Source Control relay limit must be at least four")
    _require_relay_dependencies(dependencies)
    claimed = processed = released = 0
    errors: list[str | None] = []
    lanes = (
        relay_binding_requests,
        relay_integration_delivery_requests,
        relay_requirement_evidence_requests,
        relay_requirement_formal_delivery_requests,
    )
    for lane, quota in zip(lanes, _lane_limits(limit, 4), strict=True):
        result: Any
        try:
            result = lane(limit=quota, dependencies=dependencies)
        except Exception as error:
            errors.append(_candidate_error_code(error))
            continue
        claimed += result.claimed
        processed += result.accepted
        released += result.released
    return SourceControlBatchResult(
        claimed=claimed,
        processed=processed,
        released=released,
        error_codes=_safe_error_codes(errors),
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
    evidence_factory = dependencies.evidence_repository_factory
    formal_factory = dependencies.formal_repository_factory
    if evidence_factory is None or formal_factory is None:
        raise SourceControlDependencyUnavailable("Delivery repositories unavailable")
    binding_limit, delivery_limit, webhook_limit, evidence_limit, formal_limit = _lane_limits(
        limit, 5
    )
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
        evidence = [
            _ProcessCandidate("evidence", message_id)
            for message_id in evidence_factory(db).pending_evidence_request_ids(
                limit=evidence_limit,
                now=now,
            )[:evidence_limit]
        ]
        formal = [
            _ProcessCandidate("formal", str(row["message_id"]))
            for row in formal_factory(db).pending_formal_request_candidates(
                limit=formal_limit,
                now=now,
            )[:formal_limit]
        ]
    return _round_robin_candidates((binding, delivery, webhook, evidence, formal), limit=limit)


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
    if limit < 5:
        raise ValueError("Source Control process limit must be at least five")
    _require_processing_dependencies(dependencies)
    candidates = _pending_process_candidates(limit=limit, dependencies=dependencies)
    effect_ids: list[str] = []
    errors: list[str | None] = []
    claimed = processed = released = 0
    for candidate in candidates:
        result: Any
        try:
            if candidate.lane in {"evidence", "formal"}:
                delivery = _process_delivery_candidate(candidate, dependencies=dependencies)
                claimed += delivery.claimed
                processed += delivery.processed
                released += delivery.released
                effect_ids.extend(delivery.effect_ids)
                errors.extend(delivery.error_codes)
                continue
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
                claimed += 1
                processed += 1
                continue
        except InboxClaimLost:
            continue
        except Exception as error:
            # Per-candidate workers own their persisted claim/retry state.
            # An unexpected exception proves neither a claim nor a release.
            errors.append(_candidate_error_code(error))
            continue
        claimed += 1
        processed += 1
        effect_id, error_code = _result_facts(result)
        if effect_id is not None:
            effect_ids.append(effect_id)
        errors.append(error_code)
    return SourceControlBatchResult(
        claimed=claimed,
        processed=processed,
        released=released,
        effect_ids=tuple(effect_ids),
        error_codes=_safe_error_codes(errors),
    )


def reconcile_due_source_control_effects(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> SourceControlBatchResult:
    if limit < 3:
        raise ValueError("Source Control reconciliation limit must be at least three")
    _require_reconciliation_dependencies(dependencies)
    effect_ids: list[str] = []
    errors: list[str | None] = []
    lanes = (reconcile_due_effects, reconcile_due_integration_effects, reconcile_due_formal_effects)
    for index, (lane, quota) in enumerate(zip(lanes, _lane_limits(limit, 3), strict=True)):
        result: Any
        try:
            result = lane(limit=quota, dependencies=dependencies)
            effects = result if index == 2 else result.effects
        except Exception as error:
            errors.append(_candidate_error_code(error))
            continue
        effect_ids.extend(effect.id for effect in effects)
        errors.extend(effect.last_error_code for effect in effects)
    return SourceControlBatchResult(
        claimed=len(effect_ids),
        processed=len(effect_ids),
        effect_ids=tuple(effect_ids),
        error_codes=_safe_error_codes(errors),
    )


__all__ = [
    "process_due_source_control_inboxes",
    "reconcile_due_source_control_effects",
    "relay_due_source_control_requests",
]

_SYSTEM_ACTOR = "SYSTEM:SOURCE_CONTROL"
_MAX_INBOX_RETRY_SECONDS = 300


def _retry_at(now: datetime, *, next_attempt: int) -> datetime:
    exponent = min(max(next_attempt - 1, 0), 6)
    seconds = min(5 * (2**exponent), _MAX_INBOX_RETRY_SECONDS)
    return now + timedelta(seconds=seconds)


def _candidate_error_code(error: Exception) -> str:
    if isinstance(error, EvidenceStale):
        return SourceControlReason.EVIDENCE_STALE.value
    if isinstance(error, EvidenceUnavailable):
        return SourceControlReason.EVIDENCE_UNAVAILABLE.value
    if isinstance(error, FormalDeliveryConflict):
        return SourceControlReason.FORMAL_DELIVERY_CONFLICT.value
    return SourceControlReason.CONNECTOR_UNAVAILABLE.value


def _require_relay_dependencies(dependencies: SourceControlDependencies) -> None:
    if (
        dependencies.requirement_evidence is None
        or dependencies.evidence_repository_factory is None
        or dependencies.requirement_formal_delivery is None
        or dependencies.formal_repository_factory is None
    ):
        raise SourceControlDependencyUnavailable("Delivery relay unavailable")


def _require_processing_dependencies(dependencies: SourceControlDependencies) -> None:
    if (
        dependencies.evidence_repository_factory is None
        or dependencies.formal_repository_factory is None
        or dependencies.requirement_formal_delivery is None
        or dependencies.gitlab_formal_merge_requests is None
        or dependencies.formal_review_routing is None
    ):
        raise SourceControlDependencyUnavailable("Delivery processing unavailable")


def _require_reconciliation_dependencies(dependencies: SourceControlDependencies) -> None:
    if (
        dependencies.formal_repository_factory is None
        or dependencies.requirement_formal_delivery is None
        or dependencies.gitlab_formal_merge_requests is None
        or dependencies.formal_review_routing is None
    ):
        raise SourceControlDependencyUnavailable("Delivery reconciliation unavailable")


def _process_delivery_candidate(
    candidate: _ProcessCandidate,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlBatchResult:
    evidence_factory = dependencies.evidence_repository_factory
    formal_factory = dependencies.formal_repository_factory
    if evidence_factory is None or formal_factory is None:
        raise SourceControlDependencyUnavailable("Delivery repositories unavailable")
    if candidate.lane == "evidence":
        with dependencies.engine.begin() as db:
            repository = evidence_factory(db)
            # Hold the row lock outside the savepoint: rollback must not let another
            # worker reclaim between the failed attempt and its retry scheduling.
            locked = repository.evidence_request(candidate.identifier, for_update=True)
            if locked is None:
                raise EvidenceUnavailable(candidate.identifier)
            try:
                with db.begin_nested():
                    process_integration_baseline_candidate(
                        repository,
                        message_id=candidate.identifier,
                        generated_by=_SYSTEM_ACTOR,
                        dependencies=dependencies,
                    )
            except InboxProcessingFailed as error:
                code = _candidate_error_code(error.cause)
                now = dependencies.clock.now()
                # The savepoint rolled back the acquired attempt increment; the
                # outer lock still fences the original attempt used by this CAS.
                failed = repository.fail_evidence_request(
                    candidate.identifier,
                    expected_attempts=locked["attempts"],
                    now=now,
                    retry_at=_retry_at(now, next_attempt=error.expected_attempts),
                    error_code=code,
                )
                return SourceControlBatchResult(
                    claimed=1,
                    processed=0,
                    released=int(failed is not None),
                    error_codes=(code,),
                )
        return SourceControlBatchResult(claimed=1, processed=1)
    try:
        result = process_formal_delivery_candidate(
            message_id=candidate.identifier,
            dependencies=dependencies,
        )
    except InboxProcessingFailed as error:
        code = _candidate_error_code(error.cause)
        with dependencies.engine.begin() as db:
            now = dependencies.clock.now()
            failed = formal_factory(db).fail_formal_request(
                candidate.identifier,
                expected_attempts=error.expected_attempts,
                now=now,
                retry_at=_retry_at(now, next_attempt=error.expected_attempts),
                error_code=code,
            )
        return SourceControlBatchResult(
            claimed=1,
            processed=0,
            released=int(failed is not None),
            error_codes=(code,),
        )
    return SourceControlBatchResult(
        claimed=1,
        processed=1,
        effect_ids=() if result.effect is None else (result.effect.id,),
        error_codes=_safe_error_codes(
            [
                result.blocked_reason if result.effect is None else result.effect.last_error_code,
            ]
        ),
    )
