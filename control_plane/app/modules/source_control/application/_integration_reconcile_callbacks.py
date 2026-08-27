from typing import Literal

from control_plane.app.modules.source_control.application._integration_callbacks import (
    _record_effect_callback,
)
from control_plane.app.modules.source_control.application._integration_common import (
    OriginatingCallbackSubject,
    binding_dto,
    effect_dto,
)
from control_plane.app.modules.source_control.application._reasons import effect_reason
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectOperation,
    EffectState,
    MergeIntegrationMergeRequestEffectPayload,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason


def replay_pending_integration_callbacks(
    *,
    limit: int,
    excluded_effect_ids: frozenset[str],
    dependencies: SourceControlDependencies,
) -> tuple[SourceControlEffectDto, ...]:
    if limit < 1:
        return ()
    repository_factory = dependencies.delivery_repository_factory
    requirement = dependencies.requirement_delivery
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    if requirement is None:
        return ()
    with dependencies.engine.connect() as db:
        rows = repository_factory(db).pending_callback_effects(
            limit=limit + len(excluded_effect_ids)
        )
    replayed: list[SourceControlEffectDto] = []
    for row in rows:
        effect = effect_dto(row)
        if effect.id in excluded_effect_ids:
            continue
        with dependencies.engine.connect() as db:
            repository = repository_factory(db)
            if effect.operation is EffectOperation.CREATE_INTEGRATION_MR:
                binding_row = repository.merge_request_binding_by_work_item(effect.work_item_id)
            else:
                payload = effect.payload
                binding_row = (
                    repository.merge_request_binding_by_id(payload.binding_id)
                    if isinstance(payload, MergeIntegrationMergeRequestEffectPayload)
                    else None
                )
        binding = None if binding_row is None else binding_dto(binding_row)
        context = requirement.delivery_context(effect.work_item_id)
        if (
            context.work_item_id != effect.work_item_id
            or context.requirement_id != effect.requirement_id
            or context.repository_id != effect.repository_id
        ):
            continue
        kind: Literal["ready", "merged", "blocked", "external_drift"]
        reason: SourceControlReason | None = None
        if effect.state is EffectState.SUCCEEDED:
            if binding is None:
                continue
            kind = (
                "ready" if effect.operation is EffectOperation.CREATE_INTEGRATION_MR else "merged"
            )
        elif effect.state is EffectState.BLOCKED:
            reason = effect_reason(effect)
            if reason is None:
                raise SourceControlDependencyUnavailable("Blocked reason is unavailable")
            kind = (
                "external_drift"
                if reason is SourceControlReason.EXTERNAL_MERGE_DRIFT
                else "blocked"
            )
        else:
            continue
        replayed.append(
            _record_effect_callback(
                OriginatingCallbackSubject(
                    work_item_id=context.work_item_id,
                    work_item_revision=context.work_item_revision,
                ),
                effect,
                kind=kind,
                binding_id=None if binding is None else binding.id,
                reason_code=(reason if effect.state is EffectState.BLOCKED else None),
                operation=effect.operation,
                dependencies=dependencies,
            )
        )
        if len(replayed) >= limit:
            break
    return tuple(replayed)


__all__: list[str] = []
