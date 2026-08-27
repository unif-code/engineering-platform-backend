from typing import Any

from control_plane.app.modules.source_control.application._integration_callbacks import (
    _complete_effect_block,
    _complete_preflight_block,
    _finish_preflight_callback,
    _mark_unknown,
    _record_effect_callback,
)
from control_plane.app.modules.source_control.application._integration_common import (
    OriginatingCallbackSubject as _OriginatingCallbackSubject,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProcessIntegrationRequestResult,
)
from control_plane.app.modules.source_control.application._integration_common import (
    append_audit as _append_audit,
)
from control_plane.app.modules.source_control.application._integration_common import (
    effect_dto as _effect_dto,
)
from control_plane.app.modules.source_control.application._integration_common import (
    observation_digest as _observation_digest,
)
from control_plane.app.modules.source_control.application._integration_common import (
    observation_dto as _observation_dto,
)
from control_plane.app.modules.source_control.application._integration_common import (
    snapshot_state as _snapshot_state,
)
from control_plane.app.modules.source_control.application._integration_merge_context import (
    _MERGE_OPERATION,
    _MergeAdmission,
    _read_stored_merge_facts,
    _StoredMergeFacts,
)
from control_plane.app.modules.source_control.application._reasons import effect_reason
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectState,
    MergeRequestObservationDto,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import (
    ExternalMergeDriftResult,
    GitLabMergeRequestSnapshot,
)


def _replay_merge_effect(
    inbox: Any,
    facts: _StoredMergeFacts,
    *,
    claimed_attempts: int | None,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    effect = facts.effect
    if effect is None or effect.state is EffectState.PLANNED:
        raise RequirementCallbackUnavailable("Integration merge effect is unavailable")
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    if claimed_attempts is not None:
        with dependencies.engine.begin() as db:
            completed = repository_factory(db).complete_delivery_request(
                str(inbox["message_id"]),
                expected_attempts=claimed_attempts,
                now=dependencies.clock.now(),
            )
            if completed is None:
                raise RequirementCallbackUnavailable("Integration merge inbox lease was lost")
    callback_subject = _OriginatingCallbackSubject(
        work_item_id=str(inbox["work_item_id"]),
        work_item_revision=inbox["work_item_revision"],
    )
    if effect.callback_state is not RequirementCallbackState.ACKED:
        if effect.state is EffectState.SUCCEEDED:
            effect = _record_effect_callback(
                callback_subject,
                effect,
                kind="merged",
                binding_id=facts.binding.id,
                operation=_MERGE_OPERATION,
                dependencies=dependencies,
            )
        elif effect.state is EffectState.BLOCKED:
            reason = effect_reason(effect)
            if reason is None:
                raise SourceControlDependencyUnavailable(
                    "Blocked integration merge effect reason unavailable"
                )
            effect = _record_effect_callback(
                callback_subject,
                effect,
                kind=(
                    "external_drift"
                    if reason is SourceControlReason.EXTERNAL_MERGE_DRIFT
                    else "blocked"
                ),
                binding_id=facts.binding.id,
                reason_code=reason,
                operation=_MERGE_OPERATION,
                dependencies=dependencies,
            )
        elif effect.state in {
            EffectState.IN_FLIGHT,
            EffectState.UNKNOWN,
            EffectState.RECONCILIATION,
        }:
            effect = _record_effect_callback(
                callback_subject,
                effect,
                kind="pending",
                binding_id=facts.binding.id,
                operation=_MERGE_OPERATION,
                dependencies=dependencies,
            )
    if effect.state is EffectState.SUCCEEDED:
        blocked_reason = None
    elif effect.state in {
        EffectState.IN_FLIGHT,
        EffectState.UNKNOWN,
        EffectState.RECONCILIATION,
    }:
        blocked_reason = SourceControlReason.RECONCILIATION_PENDING.value
    else:
        final_reason = effect_reason(effect)
        if final_reason is None:
            raise SourceControlDependencyUnavailable(
                "Blocked integration merge effect reason unavailable"
            )
        blocked_reason = final_reason.value
    return ProcessIntegrationRequestResult(
        effect=effect,
        binding=facts.binding,
        observation=facts.observation,
        blocked_reason=blocked_reason,
    )


def _complete_merge_preflight_block(
    admission: _MergeAdmission,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: SourceControlReason,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    return _complete_preflight_block(
        admission.context,
        message_id=message_id,
        inbox_attempts=inbox_attempts,
        reason_code=reason_code,
        binding=admission.binding,
        observation=admission.latest_observation,
        idempotency_key=(
            f"source-control:integration-merge-blocked:{message_id}:{reason_code.value}"
        ),
        dependencies=dependencies,
    )


def _replay_merge_preflight_callback(
    inbox: Any,
    facts: _StoredMergeFacts,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: SourceControlReason,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    _finish_preflight_callback(
        _OriginatingCallbackSubject(
            work_item_id=str(inbox["work_item_id"]),
            work_item_revision=inbox["work_item_revision"],
        ),
        message_id=message_id,
        inbox_attempts=inbox_attempts,
        reason_code=reason_code,
        binding_id=facts.binding.id,
        kind=(
            "external_drift"
            if reason_code is SourceControlReason.EXTERNAL_MERGE_DRIFT
            else "blocked"
        ),
        idempotency_key=(
            f"source-control:external-merge-drift:{message_id}"
            if reason_code is SourceControlReason.EXTERNAL_MERGE_DRIFT
            else (f"source-control:integration-merge-blocked:{message_id}:{reason_code.value}")
        ),
        dependencies=dependencies,
    )
    return ProcessIntegrationRequestResult(
        effect=None,
        binding=facts.binding,
        observation=facts.observation,
        blocked_reason=reason_code.value,
    )


def _mark_merge_unknown(
    admission: _MergeAdmission,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    return _mark_unknown(
        admission.context,
        effect,
        message_id=message_id,
        inbox_attempts=inbox_attempts,
        operation=_MERGE_OPERATION,
        binding=admission.binding,
        observation=admission.latest_observation,
        dependencies=dependencies,
    )


def _complete_merge_effect_block(
    admission: _MergeAdmission,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: SourceControlReason,
    readback: GitLabMergeRequestSnapshot | None = None,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    return _complete_effect_block(
        admission.context,
        effect,
        message_id=message_id,
        inbox_attempts=inbox_attempts,
        reason_code=reason_code,
        operation=_MERGE_OPERATION,
        binding=admission.binding,
        observation=admission.latest_observation,
        readback=readback,
        audit_action="source_control.integration_merge.blocked",
        dependencies=dependencies,
    )


def _commit_external_merge_drift(
    admission: _MergeAdmission,
    readback: GitLabMergeRequestSnapshot,
    *,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    requirement = dependencies.requirement_delivery
    if repository_factory is None or requirement is None:
        raise SourceControlDependencyUnavailable("Integration merge callback unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        observation_row = repository.append_merge_request_observation(
            id=str(dependencies.random.uuid4()),
            binding_id=admission.binding.id,
            head_sha=readback.head_sha,
            state=_snapshot_state(readback).value,
            merge_commit_sha=readback.merge_commit_sha,
            external_merge_user_id=readback.merge_user_id,
            merged_at=readback.merged_at,
            observation_digest=_observation_digest(readback),
            observed_at=now,
        )
        marked = repository.record_preflight_outcome(
            message_id,
            expected_attempts=inbox_attempts,
            reason_code=SourceControlReason.EXTERNAL_MERGE_DRIFT.value,
            now=now,
        )
        if observation_row is None or marked is None:
            raise RequirementCallbackUnavailable("Integration merge drift lease was lost")
        _append_audit(
            repository,
            action="source_control.integration_merge.external_drift",
            target_type="merge_request_binding",
            target_id=admission.binding.id,
            dependencies=dependencies,
        )
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        locked = repository.delivery_request(message_id, for_update=True)
        if (
            locked is None
            or locked["state"] != "PROCESSING"
            or locked["attempts"] != inbox_attempts
            or locked["last_error_code"] != SourceControlReason.EXTERNAL_MERGE_DRIFT.value
        ):
            raise RequirementCallbackUnavailable("Integration merge drift lease was lost")
        try:
            requirement.record_external_merge_drift(
                ExternalMergeDriftResult(
                    work_item_id=admission.context.work_item_id,
                    binding_id=admission.binding.id,
                    expected_revision=admission.context.work_item_revision,
                    idempotency_key=(f"source-control:external-merge-drift:{message_id}"),
                )
            )
        except Exception:
            return ProcessIntegrationRequestResult(
                effect=None,
                binding=admission.binding,
                observation=_observation_dto(observation_row),
                blocked_reason=SourceControlReason.EXTERNAL_MERGE_DRIFT.value,
            )
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=dependencies.clock.now(),
        )
        if completed is None:
            raise RequirementCallbackUnavailable("Integration merge drift lease was lost")
    return ProcessIntegrationRequestResult(
        effect=None,
        binding=admission.binding,
        observation=_observation_dto(observation_row),
        blocked_reason=SourceControlReason.EXTERNAL_MERGE_DRIFT.value,
    )


def _commit_proven_merge_conflict(
    admission: _MergeAdmission,
    readback: GitLabMergeRequestSnapshot,
    *,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        observation_row = repository.append_merge_request_observation(
            id=str(dependencies.random.uuid4()),
            binding_id=admission.binding.id,
            head_sha=readback.head_sha,
            state=_snapshot_state(readback).value,
            merge_commit_sha=readback.merge_commit_sha,
            external_merge_user_id=readback.merge_user_id,
            merged_at=readback.merged_at,
            observation_digest=_observation_digest(readback),
            observed_at=now,
        )
        marked = repository.record_preflight_outcome(
            message_id,
            expected_attempts=inbox_attempts,
            reason_code=SourceControlReason.MR_CONFLICT.value,
            now=now,
        )
        if observation_row is None or marked is None:
            raise RequirementCallbackUnavailable("Integration merge conflict lease was lost")
        _append_audit(
            repository,
            action="source_control.integration_merge.provider_fact_conflict",
            target_type="merge_request_binding",
            target_id=admission.binding.id,
            dependencies=dependencies,
        )
    _finish_preflight_callback(
        admission.context,
        message_id=message_id,
        inbox_attempts=inbox_attempts,
        reason_code=SourceControlReason.MR_CONFLICT,
        binding_id=admission.binding.id,
        idempotency_key=f"source-control:integration-merge-blocked:{message_id}:MR_CONFLICT",
        dependencies=dependencies,
    )
    return ProcessIntegrationRequestResult(
        effect=None,
        binding=admission.binding,
        observation=_observation_dto(observation_row),
        blocked_reason=SourceControlReason.MR_CONFLICT.value,
    )


def _commit_merge_success(
    admission: _MergeAdmission,
    effect: SourceControlEffectDto,
    readback: GitLabMergeRequestSnapshot,
    *,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> tuple[SourceControlEffectDto, MergeRequestObservationDto]:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    completed_at = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        observation_row = repository.append_merge_request_observation(
            id=str(dependencies.random.uuid4()),
            binding_id=admission.binding.id,
            head_sha=readback.head_sha,
            state=_snapshot_state(readback).value,
            merge_commit_sha=readback.merge_commit_sha,
            external_merge_user_id=readback.merge_user_id,
            merged_at=readback.merged_at,
            observation_digest=_observation_digest(readback),
            observed_at=completed_at,
        )
        final_row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.IN_FLIGHT.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.SUCCEEDED.value,
                "last_error_code": None,
                "next_reconcile_at": None,
                "completed_at": completed_at,
                "updated_at": completed_at,
            },
        )
        completed_inbox = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=completed_at,
        )
        if observation_row is None or final_row is None or completed_inbox is None:
            raise RequirementCallbackUnavailable("Integration merge fact lease was lost")
        _append_audit(
            repository,
            action="source_control.integration_merge.succeeded",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
        )
    return _effect_dto(final_row), _observation_dto(observation_row)


def _resolve_merge_fact_commit(
    admission: _MergeAdmission,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    with dependencies.engine.connect() as db:
        inbox = repository_factory(db).delivery_request(message_id)
    if inbox is None:
        raise RequirementCallbackUnavailable("Integration merge commit outcome is unavailable")
    facts = _read_stored_merge_facts(inbox, dependencies=dependencies)
    persisted_effect = facts.effect
    if (
        persisted_effect is not None
        and persisted_effect.state in {EffectState.SUCCEEDED, EffectState.BLOCKED}
        and inbox["state"] == "PROCESSED"
    ):
        return _replay_merge_effect(
            inbox,
            facts,
            claimed_attempts=None,
            dependencies=dependencies,
        )
    if (
        persisted_effect is not None
        and persisted_effect.id == effect.id
        and persisted_effect.state is EffectState.IN_FLIGHT
        and persisted_effect.attempts == effect.attempts
        and inbox["state"] == "PROCESSING"
        and inbox["attempts"] == inbox_attempts
    ):
        return _mark_merge_unknown(
            admission,
            persisted_effect,
            message_id=message_id,
            inbox_attempts=inbox_attempts,
            dependencies=dependencies,
        )
    raise RequirementCallbackUnavailable("Integration merge commit outcome is inconsistent")


def _complete_merge_success(
    admission: _MergeAdmission,
    effect: SourceControlEffectDto,
    readback: GitLabMergeRequestSnapshot,
    *,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    try:
        persisted_effect, observation = _commit_merge_success(
            admission,
            effect,
            readback,
            message_id=message_id,
            inbox_attempts=inbox_attempts,
            dependencies=dependencies,
        )
    except Exception:
        return _resolve_merge_fact_commit(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=inbox_attempts,
            dependencies=dependencies,
        )
    callback_subject = _OriginatingCallbackSubject(
        work_item_id=admission.context.work_item_id,
        work_item_revision=admission.context.work_item_revision,
    )
    persisted_effect = _record_effect_callback(
        callback_subject,
        persisted_effect,
        kind="merged",
        binding_id=admission.binding.id,
        operation=_MERGE_OPERATION,
        dependencies=dependencies,
    )
    return ProcessIntegrationRequestResult(
        effect=persisted_effect,
        binding=admission.binding,
        observation=observation,
        blocked_reason=None,
    )


__all__: list[str] = []
