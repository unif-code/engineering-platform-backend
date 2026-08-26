from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectState,
    ProcessBindingRequestResult,
    RepositoryAuthorizationState,
    RepositoryBranchBindingDto,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlEffectDto,
    build_task_branch_name,
)
from control_plane.app.modules.source_control.ports import (
    BindingBlockedResult,
    BindingReadyResult,
    GitLabAccessDenied,
    GitLabBranchConflict,
    GitLabDefaultBranchNotFound,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
    RequirementBindingContext,
    create_and_verify_branch,
)


def _effect_dto(row: Any) -> SourceControlEffectDto:
    return SourceControlEffectDto(
        id=str(row["id"]),
        effect_key=row["effect_key"],
        operation=row["operation"],
        work_item_id=str(row["work_item_id"]),
        requirement_id=str(row["requirement_id"]),
        repository_id=str(row["repository_id"]),
        work_item_number=row["work_item_number"],
        branch_name=row["branch_name"],
        base_commit_sha=row["base_commit_sha"],
        request_fingerprint=row["request_fingerprint"],
        attempts=row["attempts"],
        next_reconcile_at=row["next_reconcile_at"],
        state=EffectState(row["state"]),
        last_error_code=row["last_error_code"],
        callback_state=RequirementCallbackState(row["requirement_callback_state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def _binding_dto(row: Any) -> RepositoryBranchBindingDto:
    return RepositoryBranchBindingDto(
        id=str(row["id"]),
        work_item_id=str(row["work_item_id"]),
        requirement_id=str(row["requirement_id"]),
        workspace_id=str(row["workspace_id"]),
        repository_id=str(row["repository_id"]),
        work_item_number=row["work_item_number"],
        base_commit_sha=row["base_commit_sha"],
        branch_name=row["branch_name"],
        effect_id=str(row["effect_id"]),
        created_at=row["created_at"],
    )


def _repository_profile(row: Any) -> GitLabRepositoryProfile:
    return GitLabRepositoryProfile(
        repository_id=str(row["id"]),
        project_id=row["project_id"],
        default_branch=row["default_branch"],
        credential_secret_ref=row["credential_secret_ref"],
    )


def _set_callback_state(
    effect_id: str,
    *,
    expected_state: EffectState,
    callback_state: RequirementCallbackState,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        updated = repository.transition_effect(
            effect_id,
            expected_state=expected_state.value,
            values={
                "requirement_callback_state": callback_state.value,
                "updated_at": now,
            },
        )
    return _effect_dto(updated)


def _record_blocked(
    context: RequirementBindingContext,
    *,
    reason_code: str,
    idempotency_key: str,
    dependencies: SourceControlDependencies,
) -> None:
    requirement = dependencies.requirement
    if requirement is None:
        raise RequirementCallbackUnavailable("Requirement binding port is unavailable")
    requirement.record_blocked(
        BindingBlockedResult(
            work_item_id=context.work_item_id,
            repository_id=context.repository_id,
            reason_code=reason_code,
            expected_revision=context.work_item_revision,
            idempotency_key=idempotency_key,
        )
    )


def _complete_preflight_block(
    context: RequirementBindingContext,
    *,
    message_id: str,
    reason_code: str,
    dependencies: SourceControlDependencies,
) -> ProcessBindingRequestResult:
    _record_blocked(
        context,
        reason_code=reason_code,
        idempotency_key=f"source-control:block:{message_id}:{reason_code}",
        dependencies=dependencies,
    )
    with dependencies.engine.begin() as db:
        dependencies.repository_factory(db).complete_binding_request(
            message_id,
            now=dependencies.clock.now(),
        )
    return ProcessBindingRequestResult(
        effect=None,
        binding=None,
        blocked_reason=reason_code,
    )


def _complete_effect_block(
    effect: SourceControlEffectDto,
    context: RequirementBindingContext,
    *,
    message_id: str,
    reason_code: str,
    dependencies: SourceControlDependencies,
) -> ProcessBindingRequestResult:
    completed_at = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        repository.transition_effect(
            effect.id,
            expected_state=EffectState.IN_FLIGHT.value,
            values={
                "state": EffectState.BLOCKED.value,
                "last_error_code": reason_code,
                "completed_at": completed_at,
                "updated_at": completed_at,
            },
        )
        repository.complete_binding_request(message_id, now=completed_at)
    try:
        _record_blocked(
            context,
            reason_code=reason_code,
            idempotency_key=f"source-control:blocked:{effect.id}",
            dependencies=dependencies,
        )
    except RequirementCallbackUnavailable:
        callback_state = RequirementCallbackState.FAILED
    else:
        callback_state = RequirementCallbackState.ACKED
    blocked = _set_callback_state(
        effect.id,
        expected_state=EffectState.BLOCKED,
        callback_state=callback_state,
        dependencies=dependencies,
    )
    return ProcessBindingRequestResult(
        effect=blocked,
        binding=None,
        blocked_reason=reason_code,
    )


def _replay_existing_binding(
    binding: RepositoryBranchBindingDto,
    effect: SourceControlEffectDto,
    context: RequirementBindingContext,
    dependencies: SourceControlDependencies,
) -> ProcessBindingRequestResult:
    requirement = dependencies.requirement
    if requirement is None:
        raise RequirementCallbackUnavailable("Requirement binding port is unavailable")
    try:
        requirement.record_ready(
            BindingReadyResult(
                work_item_id=binding.work_item_id,
                repository_id=binding.repository_id,
                base_commit_sha=binding.base_commit_sha,
                task_branch=binding.branch_name,
                expected_revision=context.work_item_revision,
                idempotency_key=f"source-control:ready:{effect.id}",
            )
        )
    except RequirementCallbackUnavailable:
        effect = _set_callback_state(
            effect.id,
            expected_state=EffectState.SUCCEEDED,
            callback_state=RequirementCallbackState.FAILED,
            dependencies=dependencies,
        )
    else:
        effect = _set_callback_state(
            effect.id,
            expected_state=EffectState.SUCCEEDED,
            callback_state=RequirementCallbackState.ACKED,
            dependencies=dependencies,
        )
    return ProcessBindingRequestResult(effect=effect, binding=binding, blocked_reason=None)


def get_repository_branch_binding(
    work_item_id: str,
    *,
    dependencies: SourceControlDependencies,
) -> RepositoryBranchBindingDto | None:
    with dependencies.engine.connect() as db:
        row = dependencies.repository_factory(db).binding_by_work_item(work_item_id)
    return None if row is None else _binding_dto(row)


def process_binding_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessBindingRequestResult:
    requirement = dependencies.requirement
    eligibility = dependencies.eligibility
    gitlab = dependencies.gitlab
    policy = dependencies.policy
    if requirement is None or eligibility is None or gitlab is None or policy is None:
        raise RequirementCallbackUnavailable("Source Control dependency is unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        claimed = repository.claim_binding_request(
            message_id,
            now=now,
            lease_until=now + timedelta(minutes=2),
        )
        inbox = claimed or repository.binding_request(message_id)
    if inbox is None:
        raise RequirementCallbackUnavailable("Binding request is unavailable")

    context = requirement.binding_context(str(inbox["work_item_id"]))
    with dependencies.engine.connect() as db:
        repository = dependencies.repository_factory(db)
        binding_row = repository.binding_by_work_item(context.work_item_id)
        effect_row = repository.effect_by_work_item(context.work_item_id)
    if binding_row is not None and effect_row is not None:
        return _replay_existing_binding(
            _binding_dto(binding_row),
            _effect_dto(effect_row),
            context,
            dependencies,
        )
    if claimed is None:
        if effect_row is not None:
            return ProcessBindingRequestResult(
                effect=_effect_dto(effect_row),
                binding=None,
                blocked_reason=effect_row["last_error_code"],
            )
        raise RequirementCallbackUnavailable("Binding request is already processing")

    if context.assignment_state != "ASSIGNED" or context.human_owner_id is None:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            reason_code="OWNER_UNASSIGNED",
            dependencies=dependencies,
        )
    owner = eligibility.evaluate(context)
    if not owner.eligible:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            reason_code=owner.reason_code or "OWNER_INELIGIBLE",
            dependencies=dependencies,
        )
    with dependencies.engine.connect() as db:
        repository_row = dependencies.repository_factory(db).workspace_repository(
            context.repository_id
        )
    if (
        repository_row is None
        or repository_row["status"] != RepositoryAuthorizationState.AUTHORIZED.value
        or str(repository_row["workspace_id"]) != context.workspace_id
        or str(inbox["repository_id"]) != context.repository_id
    ):
        return _complete_preflight_block(
            context,
            message_id=message_id,
            reason_code="REPOSITORY_NOT_AUTHORIZED",
            dependencies=dependencies,
        )
    profile = _repository_profile(repository_row)
    try:
        base = gitlab.get_branch(profile, profile.default_branch)
    except GitLabAccessDenied:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            reason_code="ACCESS_DENIED",
            dependencies=dependencies,
        )
    except GitLabDefaultBranchNotFound:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            reason_code="REPOSITORY_NOT_FOUND",
            dependencies=dependencies,
        )
    except GitLabProviderUnavailable:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            reason_code="CONNECTOR_UNAVAILABLE",
            dependencies=dependencies,
        )

    try:
        with dependencies.engine.begin() as db:
            repository = dependencies.repository_factory(db)
            effect_row = repository.effect_by_work_item(context.work_item_id, for_update=True)
            if effect_row is None:
                number = repository.next_work_item_number()
                effect_row = repository.insert_effect(
                    id=str(dependencies.random.uuid4()),
                    effect_key=f"source-control:create-task-branch:{context.work_item_id}",
                    operation="CREATE_TASK_BRANCH",
                    work_item_id=context.work_item_id,
                    requirement_id=context.requirement_id,
                    repository_id=context.repository_id,
                    work_item_number=number,
                    branch_name=build_task_branch_name(
                        requirement_type=context.requirement_type,
                        work_item_number=number,
                        title=context.requirement_title,
                    ),
                    base_commit_sha=base.commit_sha,
                    request_fingerprint=inbox["payload_hash"],
                    attempts=0,
                    state=EffectState.PLANNED.value,
                    requirement_callback_state=RequirementCallbackState.PENDING.value,
                    next_reconcile_at=None,
                    now=dependencies.clock.now(),
                )
    except IntegrityError:
        with dependencies.engine.connect() as db:
            effect_row = dependencies.repository_factory(db).effect_by_work_item(
                context.work_item_id
            )
        if effect_row is None:
            raise
    effect = _effect_dto(effect_row)
    if effect.state is not EffectState.PLANNED:
        return ProcessBindingRequestResult(effect=effect, binding=None, blocked_reason=None)
    with dependencies.engine.begin() as db:
        in_flight_row = dependencies.repository_factory(db).transition_effect(
            effect.id,
            expected_state=EffectState.PLANNED.value,
            values={
                "state": EffectState.IN_FLIGHT.value,
                "attempts": effect.attempts + 1,
                "updated_at": dependencies.clock.now(),
            },
        )
    if in_flight_row is None:
        with dependencies.engine.connect() as db:
            current = dependencies.repository_factory(db).effect_by_work_item(context.work_item_id)
        return ProcessBindingRequestResult(
            effect=_effect_dto(current),
            binding=None,
            blocked_reason=None,
        )
    effect = _effect_dto(in_flight_row)

    try:
        verified = create_and_verify_branch(
            gitlab,
            profile,
            branch_name=effect.branch_name,
            base_commit_sha=effect.base_commit_sha,
        )
    except GitLabResultUnknown:
        with dependencies.engine.begin() as db:
            unknown_row = dependencies.repository_factory(db).transition_effect(
                effect.id,
                expected_state=EffectState.IN_FLIGHT.value,
                values={
                    "state": EffectState.UNKNOWN.value,
                    "last_error_code": "EXTERNAL_RESULT_UNKNOWN",
                    "next_reconcile_at": policy.next_reconcile_at(
                        now=dependencies.clock.now(),
                        attempts=effect.attempts,
                    ),
                    "updated_at": dependencies.clock.now(),
                },
            )
            dependencies.repository_factory(db).complete_binding_request(
                message_id,
                now=dependencies.clock.now(),
            )
        effect = _effect_dto(unknown_row)
        try:
            _record_blocked(
                context,
                reason_code="RECONCILIATION_PENDING",
                idempotency_key=f"source-control:blocked:{effect.id}",
                dependencies=dependencies,
            )
        except RequirementCallbackUnavailable:
            callback_state = RequirementCallbackState.FAILED
        else:
            callback_state = RequirementCallbackState.ACKED
        effect = _set_callback_state(
            effect.id,
            expected_state=EffectState.UNKNOWN,
            callback_state=callback_state,
            dependencies=dependencies,
        )
        return ProcessBindingRequestResult(
            effect=effect,
            binding=None,
            blocked_reason="RECONCILIATION_PENDING",
        )
    except GitLabAccessDenied:
        return _complete_effect_block(
            effect,
            context,
            message_id=message_id,
            reason_code="ACCESS_DENIED",
            dependencies=dependencies,
        )
    except GitLabProviderUnavailable:
        return _complete_effect_block(
            effect,
            context,
            message_id=message_id,
            reason_code="CONNECTOR_UNAVAILABLE",
            dependencies=dependencies,
        )
    except GitLabBranchConflict:
        return _complete_effect_block(
            effect,
            context,
            message_id=message_id,
            reason_code="BINDING_CONFLICT",
            dependencies=dependencies,
        )

    completed_at = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        succeeded_row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.IN_FLIGHT.value,
            values={
                "state": EffectState.SUCCEEDED.value,
                "completed_at": completed_at,
                "updated_at": completed_at,
            },
        )
        binding_row = repository.insert_binding(
            id=str(dependencies.random.uuid4()),
            work_item_id=context.work_item_id,
            requirement_id=context.requirement_id,
            workspace_id=context.workspace_id,
            repository_id=context.repository_id,
            work_item_number=effect.work_item_number,
            base_commit_sha=verified.commit_sha,
            branch_name=effect.branch_name,
            effect_id=effect.id,
            now=completed_at,
        )
        repository.complete_binding_request(message_id, now=completed_at)
    return _replay_existing_binding(
        _binding_dto(binding_row),
        _effect_dto(succeeded_row),
        context,
        dependencies,
    )
