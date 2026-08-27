from datetime import timedelta
from typing import Literal, NoReturn

from control_plane.app.modules.source_control.application._integration_common import (
    CREATE_OPERATION as _CREATE_OPERATION,
)
from control_plane.app.modules.source_control.application._integration_common import (
    CallbackSubject as _CallbackSubject,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProcessIntegrationRequestResult,
)
from control_plane.app.modules.source_control.application._integration_common import (
    append_audit as _append_audit,
)
from control_plane.app.modules.source_control.application._integration_common import (
    binding_dto as _binding_dto,
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
from control_plane.app.modules.source_control.application._reasons import effect_reason
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectOperation,
    EffectState,
    MergeRequestBindingDto,
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
    IntegrationDeliveryBlockedResult,
    IntegrationMergedResult,
    IntegrationMrReadyResult,
    IntegrationReconciliationPendingResult,
)


def _record_effect_callback(
    context: _CallbackSubject,
    effect: SourceControlEffectDto,
    *,
    kind: Literal["ready", "merged", "blocked", "pending", "external_drift"],
    binding_id: str | None = None,
    reason_code: SourceControlReason | None = None,
    operation: EffectOperation = _CREATE_OPERATION,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    requirement = dependencies.requirement_delivery
    repository_factory = dependencies.delivery_repository_factory
    if requirement is None or repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration callback unavailable")
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.effect_by_operation_subject(
            operation.value,
            effect.subject_key,
            for_update=True,
        )
        if row is None:
            raise RequirementCallbackUnavailable("Integration MR callback lease was lost")
        locked = _effect_dto(row)
        if (
            locked.id != effect.id
            or locked.state is not effect.state
            or locked.attempts != effect.attempts
        ):
            raise RequirementCallbackUnavailable("Integration MR callback lease was lost")
        if locked.callback_state is RequirementCallbackState.ACKED:
            return locked
        if kind in {"ready", "merged"} and binding_id is None:
            raise SourceControlDependencyUnavailable("Integration binding is unavailable")
        if kind == "blocked" and reason_code is None:
            raise SourceControlDependencyUnavailable("Blocked reason is unavailable")
        if kind == "external_drift" and binding_id is None:
            raise SourceControlDependencyUnavailable("External-drift binding is unavailable")
        try:
            if kind == "ready":
                assert binding_id is not None
                requirement.record_mr_ready(
                    IntegrationMrReadyResult(
                        work_item_id=context.work_item_id,
                        binding_id=binding_id,
                        expected_revision=context.work_item_revision,
                        idempotency_key=f"source-control:mr-ready:{locked.id}",
                        correlation_id=f"source-control:effect:{locked.id}",
                    )
                )
            elif kind == "merged":
                assert binding_id is not None
                requirement.record_merged(
                    IntegrationMergedResult(
                        work_item_id=context.work_item_id,
                        binding_id=binding_id,
                        expected_revision=context.work_item_revision,
                        idempotency_key=f"source-control:integration-merged:{locked.id}",
                        correlation_id=f"source-control:effect:{locked.id}",
                    )
                )
            elif kind == "blocked":
                assert reason_code is not None
                requirement.record_blocked(
                    IntegrationDeliveryBlockedResult(
                        work_item_id=context.work_item_id,
                        binding_id=binding_id,
                        reason_code=reason_code,
                        expected_revision=context.work_item_revision,
                        idempotency_key=(
                            f"source-control:integration-blocked:{locked.id}:{reason_code.value}"
                        ),
                        correlation_id=f"source-control:effect:{locked.id}",
                    )
                )
            elif kind == "pending":
                requirement.record_pending(
                    IntegrationReconciliationPendingResult(
                        work_item_id=context.work_item_id,
                        binding_id=binding_id,
                        expected_revision=context.work_item_revision,
                        idempotency_key=f"source-control:integration-pending:{locked.id}",
                        correlation_id=f"source-control:effect:{locked.id}",
                    )
                )
            else:
                assert binding_id is not None
                requirement.record_external_merge_drift(
                    ExternalMergeDriftResult(
                        work_item_id=context.work_item_id,
                        binding_id=binding_id,
                        expected_revision=context.work_item_revision,
                        idempotency_key=(f"source-control:external-merge-drift:{locked.id}"),
                        correlation_id=f"source-control:effect:{locked.id}",
                    )
                )
        except Exception:
            callback_state = RequirementCallbackState.FAILED
        else:
            callback_state = RequirementCallbackState.ACKED
        updated = repository.transition_effect(
            locked.id,
            expected_state=locked.state.value,
            expected_attempts=locked.attempts,
            values={
                "requirement_callback_state": callback_state.value,
                "updated_at": dependencies.clock.now(),
            },
        )
        if updated is None:
            raise RequirementCallbackUnavailable("Integration MR callback lease was lost")
    return _effect_dto(updated)


def _record_ready(
    context: _CallbackSubject,
    effect: SourceControlEffectDto,
    binding: MergeRequestBindingDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    return _record_effect_callback(
        context,
        effect,
        kind="ready",
        binding_id=binding.id,
        dependencies=dependencies,
    )


def _record_pending(
    context: _CallbackSubject,
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    return _record_effect_callback(
        context,
        effect,
        kind="pending",
        dependencies=dependencies,
    )


def _mark_unknown(
    context: _CallbackSubject,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
    operation: EffectOperation = _CREATE_OPERATION,
    binding: MergeRequestBindingDto | None = None,
    observation: MergeRequestObservationDto | None = None,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    policy = dependencies.policy
    if repository_factory is None or policy is None:
        raise SourceControlDependencyUnavailable("Integration recovery dependency unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.IN_FLIGHT.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.UNKNOWN.value,
                "last_error_code": SourceControlReason.EXTERNAL_RESULT_UNKNOWN.value,
                "next_reconcile_at": policy.next_reconcile_at(
                    now=now,
                    attempts=effect.attempts,
                ),
                "updated_at": now,
            },
        )
        if row is None:
            raise RequirementCallbackUnavailable("Integration MR effect lease was lost")
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=now,
        )
        if completed is None:
            raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
    unknown = _effect_dto(row)
    unknown = _record_effect_callback(
        context,
        unknown,
        kind="pending",
        binding_id=None if binding is None else binding.id,
        operation=operation,
        dependencies=dependencies,
    )
    return ProcessIntegrationRequestResult(
        effect=unknown,
        binding=binding,
        observation=observation,
        blocked_reason=SourceControlReason.RECONCILIATION_PENDING.value,
    )


def _replay_processed_request(
    context: _CallbackSubject,
    *,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    subject_key = f"work-item:{context.work_item_id}"
    with dependencies.engine.connect() as db:
        repository = repository_factory(db)
        effect_row = repository.effect_by_operation_subject(
            _CREATE_OPERATION.value,
            subject_key,
        )
        binding_row = repository.merge_request_binding_by_work_item(context.work_item_id)
        observation_row = (
            None
            if binding_row is None
            else repository.latest_merge_request_observation(str(binding_row["id"]))
        )
    if effect_row is None:
        raise RequirementCallbackUnavailable("Integration MR effect is unavailable")
    effect = _effect_dto(effect_row)
    binding = None if binding_row is None else _binding_dto(binding_row)
    observation = None if observation_row is None else _observation_dto(observation_row)
    if effect.callback_state is RequirementCallbackState.ACKED:
        return ProcessIntegrationRequestResult(
            effect=effect,
            binding=binding,
            observation=observation,
            blocked_reason=(
                None
                if effect.state is EffectState.SUCCEEDED
                else (
                    SourceControlReason.RECONCILIATION_PENDING.value
                    if effect.state
                    in {
                        EffectState.IN_FLIGHT,
                        EffectState.UNKNOWN,
                        EffectState.RECONCILIATION,
                    }
                    else effect.last_error_code
                )
            ),
        )
    if effect.state is EffectState.SUCCEEDED and binding is not None:
        effect = _record_ready(
            context,
            effect,
            binding,
            dependencies=dependencies,
        )
        return ProcessIntegrationRequestResult(
            effect=effect,
            binding=binding,
            observation=observation,
            blocked_reason=None,
        )
    if effect.state in {
        EffectState.IN_FLIGHT,
        EffectState.UNKNOWN,
        EffectState.RECONCILIATION,
    }:
        effect = _record_pending(context, effect, dependencies=dependencies)
        return ProcessIntegrationRequestResult(
            effect=effect,
            binding=None,
            observation=None,
            blocked_reason=SourceControlReason.RECONCILIATION_PENDING.value,
        )
    if effect.state is EffectState.BLOCKED and effect.last_error_code is not None:
        reason_code = effect_reason(effect)
        if reason_code is None:
            raise SourceControlDependencyUnavailable("Blocked reason is unavailable")
        effect = _record_effect_callback(
            context,
            effect,
            kind=(
                "external_drift"
                if reason_code is SourceControlReason.EXTERNAL_MERGE_DRIFT
                else "blocked"
            ),
            binding_id=None if binding is None else binding.id,
            reason_code=reason_code,
            dependencies=dependencies,
        )
    return ProcessIntegrationRequestResult(
        effect=effect,
        binding=binding,
        observation=observation,
        blocked_reason=effect.last_error_code,
    )


def _finish_preflight_callback(
    context: _CallbackSubject,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: SourceControlReason,
    binding_id: str | None = None,
    kind: Literal["blocked", "external_drift"] = "blocked",
    idempotency_key: str | None = None,
    dependencies: SourceControlDependencies,
) -> bool:
    repository_factory = dependencies.delivery_repository_factory
    requirement = dependencies.requirement_delivery
    if repository_factory is None or requirement is None:
        raise SourceControlDependencyUnavailable("Integration callback unavailable")
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        inbox = repository.delivery_request(message_id, for_update=True)
        if (
            inbox is None
            or inbox["state"] != "PROCESSING"
            or inbox["attempts"] != inbox_attempts
            or inbox["last_error_code"] != reason_code.value
        ):
            raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
        try:
            if kind == "external_drift":
                if binding_id is None:
                    raise SourceControlDependencyUnavailable(
                        "External-drift binding is unavailable"
                    )
                requirement.record_external_merge_drift(
                    ExternalMergeDriftResult(
                        work_item_id=context.work_item_id,
                        binding_id=binding_id,
                        expected_revision=context.work_item_revision,
                        idempotency_key=(
                            idempotency_key or f"source-control:external-merge-drift:{message_id}"
                        ),
                        correlation_id=f"source-control:inbox:{message_id}",
                    )
                )
            else:
                requirement.record_blocked(
                    IntegrationDeliveryBlockedResult(
                        work_item_id=context.work_item_id,
                        binding_id=binding_id,
                        reason_code=reason_code,
                        expected_revision=context.work_item_revision,
                        idempotency_key=(
                            idempotency_key
                            or "source-control:integration-blocked:"
                            f"{message_id}:{context.work_item_id}:{reason_code.value}"
                        ),
                        correlation_id=f"source-control:inbox:{message_id}",
                    )
                )
        except Exception:
            return False
        correlation_id = f"source-control:inbox:{message_id}"
        _append_audit(
            repository,
            action=f"source_control.integration_delivery.{kind}",
            target_type="delivery_request_inbox",
            target_id=message_id,
            correlation_id=correlation_id,
            dependencies=dependencies,
        )
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=dependencies.clock.now(),
        )
        if completed is None:
            raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
    return True


def _complete_preflight_block(
    context: _CallbackSubject,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: SourceControlReason,
    binding: MergeRequestBindingDto | None = None,
    observation: MergeRequestObservationDto | None = None,
    idempotency_key: str | None = None,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        marked = repository.record_preflight_outcome(
            message_id,
            expected_attempts=inbox_attempts,
            reason_code=reason_code.value,
            now=now,
        )
        if marked is None:
            raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
    callback_succeeded = _finish_preflight_callback(
        context,
        message_id=message_id,
        inbox_attempts=inbox_attempts,
        reason_code=reason_code,
        binding_id=None if binding is None else binding.id,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
    )
    if not callback_succeeded:
        return ProcessIntegrationRequestResult(
            effect=None,
            binding=binding,
            observation=observation,
            blocked_reason=reason_code.value,
        )
    return ProcessIntegrationRequestResult(
        effect=None,
        binding=binding,
        observation=observation,
        blocked_reason=reason_code.value,
    )


def _complete_effect_block(
    context: _CallbackSubject,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: SourceControlReason,
    operation: EffectOperation = _CREATE_OPERATION,
    binding: MergeRequestBindingDto | None = None,
    observation: MergeRequestObservationDto | None = None,
    readback: GitLabMergeRequestSnapshot | None = None,
    audit_action: str | None = None,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        observation_row = (
            None
            if readback is None
            else repository.append_merge_request_observation(
                id=str(dependencies.random.uuid4()),
                binding_id=None if binding is None else binding.id,
                head_sha=readback.head_sha,
                state=_snapshot_state(readback).value,
                merge_commit_sha=readback.merge_commit_sha,
                external_merge_user_id=readback.merge_user_id,
                merged_at=readback.merged_at,
                observation_digest=_observation_digest(readback),
                observed_at=now,
            )
        )
        row = repository.transition_effect(
            effect.id,
            expected_state=effect.state.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.BLOCKED.value,
                "last_error_code": reason_code.value,
                "next_reconcile_at": None,
                "completed_at": now,
                "updated_at": now,
            },
        )
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=now,
        )
        if row is None or completed is None or (readback is not None and observation_row is None):
            raise RequirementCallbackUnavailable("Integration MR lease was lost")
        if audit_action is not None:
            _append_audit(
                repository,
                action=audit_action,
                target_type="source_control_effect",
                target_id=effect.id,
                correlation_id=f"source-control:effect:{effect.id}",
                dependencies=dependencies,
            )
    blocked = _effect_dto(row)
    blocked = _record_effect_callback(
        context,
        blocked,
        kind=(
            "external_drift"
            if reason_code is SourceControlReason.EXTERNAL_MERGE_DRIFT
            else "blocked"
        ),
        binding_id=None if binding is None else binding.id,
        reason_code=reason_code,
        operation=operation,
        dependencies=dependencies,
    )
    final_observation = (
        observation if observation_row is None else _observation_dto(observation_row)
    )
    return ProcessIntegrationRequestResult(
        effect=blocked,
        binding=binding,
        observation=final_observation,
        blocked_reason=reason_code.value,
    )


def _release_pre_effect_transient(
    *,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> NoReturn:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        released = repository_factory(db).release_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            error_code=SourceControlReason.PROVIDER_UNAVAILABLE.value,
            retry_at=now + timedelta(minutes=1),
            now=now,
        )
        if released is None:
            raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
    raise RequirementCallbackUnavailable("Integration MR provider is unavailable")


def _resolve_atomic_fact_commit(
    context: _CallbackSubject,
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
        repository = repository_factory(db)
        effect_row = repository.effect_by_operation_subject(
            _CREATE_OPERATION.value,
            effect.subject_key,
        )
        binding_row = repository.merge_request_binding_by_work_item(context.work_item_id)
        observation_row = (
            None
            if binding_row is None
            else repository.latest_merge_request_observation(str(binding_row["id"]))
        )
        inbox = repository.delivery_request(message_id)
    if effect_row is None or inbox is None:
        raise RequirementCallbackUnavailable("Integration MR commit outcome is unavailable")
    persisted_effect = _effect_dto(effect_row)
    if (
        persisted_effect.state in {EffectState.SUCCEEDED, EffectState.BLOCKED}
        and inbox["state"] == "PROCESSED"
        and binding_row is not None
        and observation_row is not None
    ):
        binding = _binding_dto(binding_row)
        observation = _observation_dto(observation_row)
        persisted_reason = effect_reason(persisted_effect)
        if persisted_effect.state is EffectState.SUCCEEDED:
            persisted_effect = _record_ready(
                context,
                persisted_effect,
                binding,
                dependencies=dependencies,
            )
            blocked_reason = None
        elif persisted_reason in {
            SourceControlReason.MR_CLOSED,
            SourceControlReason.EXTERNAL_MERGE_DRIFT,
        }:
            persisted_effect = _record_effect_callback(
                context,
                persisted_effect,
                kind=(
                    "external_drift"
                    if persisted_reason is SourceControlReason.EXTERNAL_MERGE_DRIFT
                    else "blocked"
                ),
                binding_id=binding.id,
                reason_code=persisted_reason,
                dependencies=dependencies,
            )
            blocked_reason = persisted_reason.value
        else:
            raise RequirementCallbackUnavailable("Integration MR commit outcome is inconsistent")
        return ProcessIntegrationRequestResult(
            effect=persisted_effect,
            binding=binding,
            observation=observation,
            blocked_reason=blocked_reason,
        )
    if persisted_effect.state is EffectState.IN_FLIGHT:
        return _mark_unknown(
            context,
            persisted_effect,
            message_id=message_id,
            inbox_attempts=inbox_attempts,
            dependencies=dependencies,
        )
    raise RequirementCallbackUnavailable("Integration MR commit outcome is inconsistent")
