from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from control_plane.app.modules.source_control.application._integration_callbacks import (
    _record_effect_callback,
)
from control_plane.app.modules.source_control.application._integration_common import (
    OriginatingCallbackSubject,
    append_audit,
    binding_dto,
    effect_dto,
    observation_digest,
    observation_dto,
    snapshot_state,
)
from control_plane.app.modules.source_control.application._integration_reconcile_context import (
    CreateReconciliationContext,
)
from control_plane.app.modules.source_control.application._integration_reconcile_provider import (
    CreateProviderProof,
)
from control_plane.app.modules.source_control.application._reasons import effect_reason
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectOperation,
    EffectState,
    MergeRequestBindingDto,
    MergeRequestKind,
    MergeRequestObservationDto,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason

from ._integration_reconcile_merge_context import MergeReconciliationContext


@dataclass(frozen=True, slots=True)
class CreateCompletion:
    effect: SourceControlEffectDto
    binding: MergeRequestBindingDto | None
    observation: MergeRequestObservationDto | None
    reason: SourceControlReason | None
    completed_by_worker: bool


@dataclass(frozen=True, slots=True)
class EffectCompletion:
    effect: SourceControlEffectDto
    reason: SourceControlReason | None
    completed_by_worker: bool


@dataclass(frozen=True, slots=True)
class MergeCompletion:
    effect: SourceControlEffectDto
    observation: MergeRequestObservationDto | None
    reason: SourceControlReason | None
    completed_by_worker: bool


def renew_reconciliation_lease(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto | None:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        row = repository_factory(db).transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=effect.attempts,
            values={
                "next_reconcile_at": now + timedelta(minutes=2),
                "updated_at": now,
            },
        )
    return None if row is None else effect_dto(row)


def return_reconciliation_unknown(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    repository_factory = dependencies.delivery_repository_factory
    policy = dependencies.policy
    if repository_factory is None or policy is None:
        raise SourceControlDependencyUnavailable("Integration recovery unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
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
        if row is not None:
            append_audit(
                repository,
                action="source_control.integration_reconciliation.unknown",
                target_type="source_control_effect",
                target_id=effect.id,
                dependencies=dependencies,
            )
            return effect_dto(row)
        current = repository.effect_by_operation_subject(
            effect.operation.value,
            effect.subject_key,
        )
    if current is None:
        raise SourceControlDependencyUnavailable("Integration effect unavailable")
    return effect_dto(current)


def complete_reconciliation_block(
    effect: SourceControlEffectDto,
    *,
    reason: SourceControlReason,
    dependencies: SourceControlDependencies,
) -> EffectCompletion:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        locked_row = repository.effect_by_operation_subject(
            effect.operation.value,
            effect.subject_key,
            for_update=True,
        )
        if locked_row is None:
            raise SourceControlDependencyUnavailable("Integration effect unavailable")
        locked = effect_dto(locked_row)
        if (
            locked.id != effect.id
            or locked.state is not EffectState.RECONCILIATION
            or locked.attempts != effect.attempts
        ):
            return EffectCompletion(locked, effect_reason(locked), False)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.BLOCKED.value,
                "last_error_code": reason.value,
                "next_reconcile_at": None,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if row is None:
            raise SourceControlDependencyUnavailable("Integration effect lease was lost")
        append_audit(
            repository,
            action="source_control.integration_reconciliation.blocked",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
        )
    return EffectCompletion(effect_dto(row), reason, True)


def deliver_terminal_effect(
    completion: EffectCompletion,
    *,
    binding_id: str | None,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    if not completion.completed_by_worker:
        return completion.effect
    if completion.reason is None:
        raise SourceControlDependencyUnavailable("Blocked reason is unavailable")
    requirement = dependencies.requirement_delivery
    if requirement is None:
        return completion.effect
    context = requirement.delivery_context(completion.effect.work_item_id)
    if (
        context.work_item_id != completion.effect.work_item_id
        or context.requirement_id != completion.effect.requirement_id
        or context.repository_id != completion.effect.repository_id
    ):
        return completion.effect
    kind: Literal["external_drift", "blocked"] = (
        "external_drift"
        if completion.reason is SourceControlReason.EXTERNAL_MERGE_DRIFT
        else "blocked"
    )
    return _record_effect_callback(
        OriginatingCallbackSubject(
            work_item_id=context.work_item_id,
            work_item_revision=context.work_item_revision,
        ),
        completion.effect,
        kind=kind,
        binding_id=binding_id,
        reason_code=completion.reason,
        operation=completion.effect.operation,
        dependencies=dependencies,
    )


def complete_create_reconciliation(
    context: CreateReconciliationContext,
    proof: CreateProviderProof,
    *,
    final_state: EffectState,
    reason: SourceControlReason | None,
    dependencies: SourceControlDependencies,
) -> CreateCompletion:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    effect = context.effect
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        locked_row = repository.effect_by_operation_subject(
            effect.operation.value,
            effect.subject_key,
            for_update=True,
        )
        if locked_row is None:
            raise SourceControlDependencyUnavailable("Integration effect unavailable")
        locked = effect_dto(locked_row)
        if (
            locked.id != effect.id
            or locked.state is not EffectState.RECONCILIATION
            or locked.attempts != effect.attempts
        ):
            return CreateCompletion(locked, None, None, effect_reason(locked), False)
        binding_row = repository.insert_merge_request_binding(
            id=str(dependencies.random.uuid4()),
            kind=MergeRequestKind.INTEGRATION.value,
            work_item_id=effect.work_item_id,
            requirement_id=effect.requirement_id,
            workspace_id=context.workspace_id,
            repository_id=effect.repository_id,
            branch_binding_id=context.branch_binding_id,
            external_project_id=proof.snapshot.project_id,
            merge_request_iid=proof.snapshot.iid,
            source_branch=proof.snapshot.source_branch,
            target_branch=proof.snapshot.target_branch,
            create_effect_id=effect.id,
            head_sha=proof.snapshot.head_sha,
            creation_origin=proof.creation_origin.value,
            now=now,
        )
        observation_row = repository.append_merge_request_observation(
            id=str(dependencies.random.uuid4()),
            binding_id=str(binding_row["id"]),
            head_sha=proof.snapshot.head_sha,
            state=snapshot_state(proof.snapshot).value,
            merge_commit_sha=proof.snapshot.merge_commit_sha,
            external_merge_user_id=proof.snapshot.merge_user_id,
            merged_at=proof.snapshot.merged_at,
            observation_digest=observation_digest(proof.snapshot),
            observed_at=now,
        )
        final_row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=effect.attempts,
            values={
                "state": final_state.value,
                "last_error_code": None if reason is None else reason.value,
                "next_reconcile_at": None,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if observation_row is None or final_row is None:
            raise SourceControlDependencyUnavailable("Integration effect lease was lost")
        append_audit(
            repository,
            action=(
                "source_control.integration_reconciliation.succeeded"
                if final_state is EffectState.SUCCEEDED
                else "source_control.integration_reconciliation.blocked"
            ),
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
        )
    return CreateCompletion(
        effect=effect_dto(final_row),
        binding=binding_dto(binding_row),
        observation=observation_dto(observation_row),
        reason=reason,
        completed_by_worker=True,
    )


def complete_merge_reconciliation(
    context: MergeReconciliationContext,
    snapshot: object,
    *,
    final_state: EffectState,
    reason: SourceControlReason | None,
    dependencies: SourceControlDependencies,
) -> MergeCompletion:
    from control_plane.app.modules.source_control.ports import GitLabMergeRequestSnapshot

    if not isinstance(snapshot, GitLabMergeRequestSnapshot):
        raise SourceControlDependencyUnavailable("Integration merge proof is invalid")
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    effect = context.effect
    now = dependencies.clock.now()
    digest = observation_digest(snapshot)
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        locked_row = repository.effect_by_operation_subject(
            effect.operation.value,
            effect.subject_key,
            for_update=True,
        )
        if locked_row is None:
            raise SourceControlDependencyUnavailable("Integration effect unavailable")
        locked = effect_dto(locked_row)
        if (
            locked.id != effect.id
            or locked.state is not EffectState.RECONCILIATION
            or locked.attempts != effect.attempts
        ):
            return MergeCompletion(locked, None, effect_reason(locked), False)
        observation_row = repository.append_merge_request_observation(
            id=str(dependencies.random.uuid4()),
            binding_id=context.binding.id,
            head_sha=snapshot.head_sha,
            state=snapshot_state(snapshot).value,
            merge_commit_sha=snapshot.merge_commit_sha,
            external_merge_user_id=snapshot.merge_user_id,
            merged_at=snapshot.merged_at,
            observation_digest=digest,
            observed_at=now,
        )
        if observation_row is None:
            existing = repository.latest_merge_request_observation(context.binding.id)
            if existing is None or existing["observation_digest"] != digest:
                raise SourceControlDependencyUnavailable(
                    "Integration merge observation is unavailable"
                )
            observation_row = existing
        final_row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=effect.attempts,
            values={
                "state": final_state.value,
                "last_error_code": None if reason is None else reason.value,
                "next_reconcile_at": None,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if final_row is None:
            raise SourceControlDependencyUnavailable("Integration merge lease was lost")
        append_audit(
            repository,
            action=(
                "source_control.integration_merge_reconciliation.succeeded"
                if final_state is EffectState.SUCCEEDED
                else "source_control.integration_merge_reconciliation.blocked"
            ),
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
        )
    return MergeCompletion(
        effect=effect_dto(final_row),
        observation=observation_dto(observation_row),
        reason=reason,
        completed_by_worker=True,
    )


def deliver_merge_completion(
    completion: MergeCompletion,
    *,
    binding_id: str,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    if not completion.completed_by_worker:
        return completion.effect
    if completion.effect.state is EffectState.BLOCKED and completion.reason is None:
        raise SourceControlDependencyUnavailable("Blocked reason is unavailable")
    requirement = dependencies.requirement_delivery
    if requirement is None:
        return completion.effect
    context = requirement.delivery_context(completion.effect.work_item_id)
    if (
        context.work_item_id != completion.effect.work_item_id
        or context.requirement_id != completion.effect.requirement_id
        or context.repository_id != completion.effect.repository_id
    ):
        return completion.effect
    return _record_effect_callback(
        OriginatingCallbackSubject(
            work_item_id=context.work_item_id,
            work_item_revision=context.work_item_revision,
        ),
        completion.effect,
        kind=("merged" if completion.effect.state is EffectState.SUCCEEDED else "blocked"),
        binding_id=binding_id,
        reason_code=completion.reason,
        operation=EffectOperation.MERGE_INTEGRATION_MR,
        dependencies=dependencies,
    )


def deliver_create_completion(
    completion: CreateCompletion,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    if not completion.completed_by_worker or completion.binding is None:
        return completion.effect
    if completion.effect.state is EffectState.BLOCKED and completion.reason is None:
        raise SourceControlDependencyUnavailable("Blocked reason is unavailable")
    requirement = dependencies.requirement_delivery
    if requirement is None:
        return completion.effect
    context = requirement.delivery_context(completion.effect.work_item_id)
    if (
        context.work_item_id != completion.effect.work_item_id
        or context.requirement_id != completion.effect.requirement_id
        or context.repository_id != completion.effect.repository_id
    ):
        return completion.effect
    callback_subject = OriginatingCallbackSubject(
        work_item_id=context.work_item_id,
        work_item_revision=context.work_item_revision,
    )
    kind: Literal["ready", "external_drift", "blocked"]
    if completion.effect.state is EffectState.SUCCEEDED:
        kind = "ready"
    elif completion.reason is SourceControlReason.EXTERNAL_MERGE_DRIFT:
        kind = "external_drift"
    else:
        kind = "blocked"
    return _record_effect_callback(
        callback_subject,
        completion.effect,
        kind=kind,
        binding_id=completion.binding.id,
        reason_code=completion.reason,
        operation=EffectOperation.CREATE_INTEGRATION_MR,
        dependencies=dependencies,
    )


__all__: list[str] = []
