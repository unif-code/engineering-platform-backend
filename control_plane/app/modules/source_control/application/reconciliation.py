from datetime import timedelta

from control_plane.app.modules.source_control.application.audit import (
    append_lifecycle_audit,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.application.saga import (
    _binding_dto,
    _effect_dto,
    _repository_profile,
    _set_callback_state,
)
from control_plane.app.modules.source_control.domain import (
    EffectState,
    ReconcileDueEffectsResult,
    RepositoryAuthorizationState,
    RepositoryBranchBindingDto,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlEffectDto,
    branch_effect_coordinates,
)
from control_plane.app.modules.source_control.ports import (
    BindingBlockedResult,
    BindingReadyResult,
    GitLabAccessDenied,
    GitLabBranchConflict,
    GitLabBranchNotFound,
    GitLabProviderUnavailable,
    GitLabResultUnknown,
    RequirementBindingContext,
    create_and_verify_branch,
)


def _safe_blocked_reason(internal_reason: str | None) -> str:
    if internal_reason in {
        "ACCESS_DENIED",
        "BINDING_CONFLICT",
        "OWNER_UNASSIGNED",
        "OWNER_INELIGIBLE",
        "REPOSITORY_NOT_AUTHORIZED",
    }:
        return internal_reason
    if internal_reason == "REPOSITORY_REMOVED":
        return "REPOSITORY_NOT_AUTHORIZED"
    return "CONNECTOR_UNAVAILABLE"


def _deliver_terminal_callback(
    effect: SourceControlEffectDto,
    context: RequirementBindingContext,
    *,
    binding: RepositoryBranchBindingDto | None,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    requirement = dependencies.requirement
    if requirement is None:
        return _set_callback_state(
            effect.id,
            expected_state=effect.state,
            callback_state=RequirementCallbackState.FAILED,
            dependencies=dependencies,
        )
    try:
        if effect.state is EffectState.SUCCEEDED and binding is not None:
            requirement.record_ready(
                BindingReadyResult(
                    work_item_id=binding.work_item_id,
                    repository_id=binding.repository_id,
                    base_commit_sha=binding.base_commit_sha,
                    task_branch=binding.branch_name,
                    expected_revision=context.work_item_revision,
                    idempotency_key=f"source-control:binding-ready:{effect.id}",
                )
            )
        elif effect.state is EffectState.BLOCKED:
            requirement.record_blocked(
                BindingBlockedResult(
                    work_item_id=effect.work_item_id,
                    repository_id=effect.repository_id,
                    reason_code=_safe_blocked_reason(effect.last_error_code),
                    expected_revision=context.work_item_revision,
                    idempotency_key=(
                        f"source-control:binding-blocked:{effect.id}:{effect.last_error_code}"
                    ),
                )
            )
        else:
            return effect
    except RequirementCallbackUnavailable:
        callback_state = RequirementCallbackState.FAILED
    else:
        callback_state = RequirementCallbackState.ACKED
    return _set_callback_state(
        effect.id,
        expected_state=effect.state,
        callback_state=callback_state,
        dependencies=dependencies,
    )


def _complete_success(
    effect: SourceControlEffectDto,
    context: RequirementBindingContext,
    *,
    dependencies: SourceControlDependencies,
) -> tuple[
    SourceControlEffectDto,
    RepositoryBranchBindingDto | None,
    bool,
]:
    work_item_number, branch_name, base_commit_sha = branch_effect_coordinates(effect)
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        effect_row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.SUCCEEDED.value,
                "last_error_code": None,
                "next_reconcile_at": None,
                "requirement_callback_state": RequirementCallbackState.PENDING.value,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if effect_row is None:
            current_row = repository.effect_by_id(effect.id)
            binding_row = repository.binding_by_work_item(effect.work_item_id)
            return (
                _effect_dto(current_row),
                None if binding_row is None else _binding_dto(binding_row),
                False,
            )
        binding_row = repository.binding_by_work_item(effect.work_item_id)
        binding_created = binding_row is None
        if binding_row is None:
            binding_row = repository.insert_binding(
                id=str(dependencies.random.uuid4()),
                work_item_id=effect.work_item_id,
                requirement_id=effect.requirement_id,
                workspace_id=context.workspace_id,
                repository_id=effect.repository_id,
                work_item_number=work_item_number,
                base_commit_sha=base_commit_sha,
                branch_name=branch_name,
                effect_id=effect.id,
                now=now,
            )
        if binding_created:
            append_lifecycle_audit(
                repository,
                action="source_control.binding.created",
                target_type="repository_branch_binding",
                target_id=str(binding_row["id"]),
                dependencies=dependencies,
                correlation_id=f"source-control:effect:{effect.id}",
            )
        append_lifecycle_audit(
            repository,
            action="source_control.effect.succeeded",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
            correlation_id=f"source-control:effect:{effect.id}",
        )
    return _effect_dto(effect_row), _binding_dto(binding_row), True


def _complete_block(
    effect: SourceControlEffectDto,
    *,
    reason_code: str,
    dependencies: SourceControlDependencies,
) -> tuple[SourceControlEffectDto, bool]:
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.BLOCKED.value,
                "last_error_code": reason_code,
                "next_reconcile_at": None,
                "requirement_callback_state": RequirementCallbackState.PENDING.value,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if row is None:
            return _effect_dto(repository.effect_by_id(effect.id)), False
        append_lifecycle_audit(
            repository,
            action="source_control.effect.blocked",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
            result="DENIED",
            reason=reason_code,
            correlation_id=f"source-control:effect:{effect.id}",
        )
    return _effect_dto(row), True


def _return_unknown(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    policy = dependencies.policy
    if policy is None:
        raise RequirementCallbackUnavailable("Source Control policy is unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.UNKNOWN.value,
                "last_error_code": "EXTERNAL_RESULT_UNKNOWN",
                "next_reconcile_at": policy.next_reconcile_at(
                    now=now,
                    attempts=effect.attempts,
                ),
                "updated_at": now,
            },
        )
        if row is None:
            return _effect_dto(repository.effect_by_id(effect.id))
        append_lifecycle_audit(
            repository,
            action="source_control.effect.unknown",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
            result="UNKNOWN",
            reason="EXTERNAL_RESULT_UNKNOWN",
            correlation_id=f"source-control:effect:{effect.id}",
        )
    return _effect_dto(row)


def _reconcile_effect(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    _work_item_number, branch_name, base_commit_sha = branch_effect_coordinates(effect)
    requirement = dependencies.requirement
    eligibility = dependencies.eligibility
    gitlab = dependencies.gitlab
    if requirement is None or eligibility is None or gitlab is None:
        return _return_unknown(effect, dependencies=dependencies)
    context = requirement.binding_context(effect.work_item_id)
    if context.assignment_state != "ASSIGNED" or context.human_owner_id is None:
        blocked, completed = _complete_block(
            effect,
            reason_code="OWNER_UNASSIGNED",
            dependencies=dependencies,
        )
        if not completed:
            return blocked
        return _deliver_terminal_callback(
            blocked,
            context,
            binding=None,
            dependencies=dependencies,
        )
    owner = eligibility.evaluate(context)
    if not owner.eligible:
        blocked, completed = _complete_block(
            effect,
            reason_code=owner.reason_code or "OWNER_INELIGIBLE",
            dependencies=dependencies,
        )
        if not completed:
            return blocked
        return _deliver_terminal_callback(
            blocked,
            context,
            binding=None,
            dependencies=dependencies,
        )
    with dependencies.engine.connect() as db:
        repository_row = dependencies.repository_factory(db).workspace_repository(
            effect.repository_id
        )
    if (
        repository_row is None
        or repository_row["status"] != RepositoryAuthorizationState.AUTHORIZED.value
        or str(repository_row["workspace_id"]) != context.workspace_id
        or context.repository_id != effect.repository_id
    ):
        blocked, completed = _complete_block(
            effect,
            reason_code="REPOSITORY_REMOVED",
            dependencies=dependencies,
        )
        if not completed:
            return blocked
        return _deliver_terminal_callback(
            blocked,
            context,
            binding=None,
            dependencies=dependencies,
        )
    profile = _repository_profile(repository_row)
    try:
        gitlab.validate_repository(profile)
        observed = gitlab.get_branch(profile, branch_name)
    except GitLabBranchNotFound:
        try:
            observed = create_and_verify_branch(
                gitlab,
                profile,
                branch_name=branch_name,
                base_commit_sha=base_commit_sha,
            )
        except (GitLabProviderUnavailable, GitLabResultUnknown):
            return _return_unknown(effect, dependencies=dependencies)
        except GitLabAccessDenied:
            blocked, completed = _complete_block(
                effect,
                reason_code="ACCESS_DENIED",
                dependencies=dependencies,
            )
            if not completed:
                return blocked
            return _deliver_terminal_callback(
                blocked,
                context,
                binding=None,
                dependencies=dependencies,
            )
        except GitLabBranchConflict:
            observed = None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        return _return_unknown(effect, dependencies=dependencies)
    except GitLabAccessDenied:
        blocked, completed = _complete_block(
            effect,
            reason_code="ACCESS_DENIED",
            dependencies=dependencies,
        )
        if not completed:
            return blocked
        return _deliver_terminal_callback(
            blocked,
            context,
            binding=None,
            dependencies=dependencies,
        )
    if observed is not None and observed.commit_sha == base_commit_sha:
        succeeded, binding, completed = _complete_success(
            effect,
            context,
            dependencies=dependencies,
        )
        if not completed:
            return succeeded
        return _deliver_terminal_callback(
            succeeded,
            context,
            binding=binding,
            dependencies=dependencies,
        )
    blocked, completed = _complete_block(
        effect,
        reason_code="BINDING_CONFLICT",
        dependencies=dependencies,
    )
    if not completed:
        return blocked
    return _deliver_terminal_callback(
        blocked,
        context,
        binding=None,
        dependencies=dependencies,
    )


def reconcile_due_effects(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> ReconcileDueEffectsResult:
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        claimed_rows = repository.claim_unknown_effects(
            limit=limit,
            now=now,
            lease_until=now + timedelta(minutes=2),
        )
        for row in claimed_rows:
            append_lifecycle_audit(
                repository,
                action="source_control.reconciliation.started",
                target_type="source_control_effect",
                target_id=str(row["id"]),
                dependencies=dependencies,
                correlation_id=f"source-control:effect:{row['id']}",
            )
    effects = [
        _reconcile_effect(_effect_dto(row), dependencies=dependencies) for row in claimed_rows
    ]
    processed_ids = {effect.id for effect in effects}
    with dependencies.engine.connect() as db:
        pending_rows = dependencies.repository_factory(db).pending_callback_effects(limit=limit)
    for row in pending_rows:
        pending = _effect_dto(row)
        if pending.id in processed_ids:
            continue
        requirement = dependencies.requirement
        if requirement is None:
            continue
        context = requirement.binding_context(pending.work_item_id)
        with dependencies.engine.connect() as db:
            binding_row = dependencies.repository_factory(db).binding_by_work_item(
                pending.work_item_id
            )
        binding = None if binding_row is None else _binding_dto(binding_row)
        effects.append(
            _deliver_terminal_callback(
                pending,
                context,
                binding=binding,
                dependencies=dependencies,
            )
        )
    return ReconcileDueEffectsResult(effects=tuple(effects))


def process_webhook_inbox(
    inbox_id: str,
    *,
    dependencies: SourceControlDependencies,
) -> int:
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = dependencies.repository_factory(db)
        inbox = repository.webhook_by_id(inbox_id, for_update=True)
        if inbox is None or inbox["state"] != "RECEIVED":
            return 0
        if inbox["object_kind"] == "merge_request":
            scheduled = repository.make_integration_effect_due(
                repository_id=str(inbox["repository_id"]),
                project_id=str(inbox["project_id"]),
                mr_iid=int(inbox["mr_iid"]),
                source_branch=str(inbox["source_branch"]),
                target_branch=str(inbox["target_branch"]),
                now=now,
            )
        else:
            ref = inbox["ref"]
            branch_name = ref.removeprefix("refs/heads/") if isinstance(ref, str) else ""
            scheduled = (
                repository.make_unknown_effect_due(
                    repository_id=str(inbox["repository_id"]),
                    branch_name=branch_name,
                    now=now,
                )
                if branch_name
                else 0
            )
        repository.complete_webhook(inbox_id, now=now)
        append_lifecycle_audit(
            repository,
            action="source_control.webhook.processed",
            target_type="webhook_inbox",
            target_id=inbox_id,
            dependencies=dependencies,
            correlation_id=f"source-control:webhook:{inbox['repository_id']}",
        )
    return scheduled
