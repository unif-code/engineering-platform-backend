from datetime import timedelta
from typing import Any

from control_plane.app.modules.source_control.application._integration_common import (
    EffectCollision as _EffectCollision,
)
from control_plane.app.modules.source_control.application._integration_common import (
    append_audit as _append_audit,
)
from control_plane.app.modules.source_control.application._integration_common import (
    effect_dto as _effect_dto,
)
from control_plane.app.modules.source_control.application._integration_merge_context import (
    _MERGE_OPERATION,
    _MergeAdmission,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectState,
    MergeIntegrationMergeRequestEffectPayload,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
    merge_effect_subject,
)


def _validated_merge_effect(
    effect: SourceControlEffectDto,
    *,
    admission: _MergeAdmission,
    request_fingerprint: str,
) -> MergeIntegrationMergeRequestEffectPayload | None:
    payload = effect.payload
    if (
        effect.operation is not _MERGE_OPERATION
        or effect.work_item_id != admission.context.work_item_id
        or effect.requirement_id != admission.context.requirement_id
        or effect.repository_id != admission.context.repository_id
        or effect.request_fingerprint != request_fingerprint
        or not isinstance(payload, MergeIntegrationMergeRequestEffectPayload)
        or payload.binding_id != admission.binding.id
        or effect.subject_key
        != merge_effect_subject(admission.binding.id, payload.requested_head_sha)
    ):
        return None
    return payload


def _classify_current_merge_effect(
    inbox: Any,
    admission: _MergeAdmission,
    current_head_sha: str,
    stored_effect: SourceControlEffectDto | None,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto | None:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    subject_key = merge_effect_subject(admission.binding.id, current_head_sha)
    with dependencies.engine.connect() as db:
        exact_row = repository_factory(db).effect_by_operation_subject(
            _MERGE_OPERATION.value,
            subject_key,
        )
    try:
        exact_effect = None if exact_row is None else _effect_dto(exact_row)
    except (TypeError, ValueError):
        raise _EffectCollision from None
    if stored_effect is None:
        if exact_effect is not None:
            raise _EffectCollision
        return None
    stored_payload = _validated_merge_effect(
        stored_effect,
        admission=admission,
        request_fingerprint=inbox["payload_hash"],
    )
    if stored_payload is None:
        raise _EffectCollision
    if stored_payload.requested_head_sha != current_head_sha:
        if exact_effect is not None:
            raise _EffectCollision
        return stored_effect
    if exact_effect is None or exact_effect.id != stored_effect.id:
        raise _EffectCollision
    exact_payload = _validated_merge_effect(
        exact_effect,
        admission=admission,
        request_fingerprint=inbox["payload_hash"],
    )
    if exact_payload != stored_payload:
        raise _EffectCollision
    return stored_effect


def _acquire_merge_effect(
    admission: _MergeAdmission,
    *,
    requested_head_sha: str,
    request_fingerprint: str,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    subject_key = merge_effect_subject(admission.binding.id, requested_head_sha)
    payload = MergeIntegrationMergeRequestEffectPayload(
        bindingId=admission.binding.id,
        requestedHeadSha=requested_head_sha,
    )
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.effect_by_operation_subject(
            _MERGE_OPERATION.value,
            subject_key,
            for_update=True,
        )
        if row is None:
            row = repository.insert_effect(
                id=str(dependencies.random.uuid4()),
                effect_key=(
                    "source-control:merge-integration-mr:"
                    f"{admission.binding.id}:{requested_head_sha}"
                ),
                operation=_MERGE_OPERATION.value,
                subject_key=subject_key,
                payload=payload,
                work_item_id=admission.context.work_item_id,
                requirement_id=admission.context.requirement_id,
                repository_id=admission.context.repository_id,
                request_fingerprint=request_fingerprint,
                attempts=0,
                next_reconcile_at=None,
                state=EffectState.PLANNED.value,
                requirement_callback_state=RequirementCallbackState.PENDING.value,
                now=dependencies.clock.now(),
            )
            _append_audit(
                repository,
                action="source_control.integration_merge.planned",
                target_type="source_control_effect",
                target_id=str(row["id"]),
                correlation_id=f"source-control:effect:{row['id']}",
                dependencies=dependencies,
            )
    try:
        effect = _effect_dto(row)
    except (TypeError, ValueError):
        raise _EffectCollision from None
    if (
        _validated_merge_effect(
            effect,
            admission=admission,
            request_fingerprint=request_fingerprint,
        )
        != payload
    ):
        raise _EffectCollision
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        in_flight = repository.transition_effect(
            effect.id,
            expected_state=EffectState.PLANNED.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.IN_FLIGHT.value,
                "attempts": effect.attempts + 1,
                "next_reconcile_at": dependencies.clock.now() + timedelta(minutes=2),
                "updated_at": dependencies.clock.now(),
            },
        )
        if in_flight is None:
            raise RequirementCallbackUnavailable("Integration merge effect lease was lost")
        _append_audit(
            repository,
            action="source_control.integration_merge.in_flight",
            target_type="source_control_effect",
            target_id=effect.id,
            correlation_id=f"source-control:effect:{effect.id}",
            dependencies=dependencies,
        )
    return _effect_dto(in_flight)


__all__: list[str] = []
