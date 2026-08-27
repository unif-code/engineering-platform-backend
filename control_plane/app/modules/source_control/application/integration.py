import hashlib
import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    CreateIntegrationMergeRequestEffectPayload,
    EffectOperation,
    EffectState,
    MergeRequestBindingDto,
    MergeRequestCreationOrigin,
    MergeRequestKind,
    MergeRequestObservationDto,
    MergeRequestState,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.ports import (
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectNotFound,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
    IntegrationDeliveryBlockedResult,
    IntegrationMrReadyResult,
    IntegrationReconciliationPendingResult,
    RequirementDeliveryContext,
    SourceControlIntegrationRepository,
)

_TARGET_BRANCH = "dev"
_CREATE_OPERATION = EffectOperation.CREATE_INTEGRATION_MR
_CREATE_TOPIC = "requirement.integration-merge-request.requested"
_REQUIREMENT_TYPE_PREFIXES = frozenset({"feat", "fix", "refactor", "chore"})


class ProcessIntegrationRequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    effect: SourceControlEffectDto | None
    binding: MergeRequestBindingDto | None
    observation: MergeRequestObservationDto | None
    blocked_reason: str | None


def _effect_dto(row: Any) -> SourceControlEffectDto:
    return SourceControlEffectDto.model_validate(
        {
            "id": str(row["id"]),
            "effect_key": row["effect_key"],
            "operation": row["operation"],
            "subject_key": row["subject_key"],
            "payload": dict(row["payload"]),
            "work_item_id": str(row["work_item_id"]),
            "requirement_id": str(row["requirement_id"]),
            "repository_id": str(row["repository_id"]),
            "work_item_number": row["work_item_number"],
            "branch_name": row["branch_name"],
            "base_commit_sha": row["base_commit_sha"],
            "request_fingerprint": row["request_fingerprint"],
            "attempts": row["attempts"],
            "next_reconcile_at": row["next_reconcile_at"],
            "state": row["state"],
            "last_error_code": row["last_error_code"],
            "callback_state": row["requirement_callback_state"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }
    )


def _binding_dto(row: Any) -> MergeRequestBindingDto:
    return MergeRequestBindingDto(
        id=str(row["id"]),
        kind=row["kind"],
        work_item_id=str(row["work_item_id"]),
        requirement_id=str(row["requirement_id"]),
        workspace_id=str(row["workspace_id"]),
        repository_id=str(row["repository_id"]),
        branch_binding_id=str(row["branch_binding_id"]),
        external_project_id=row["external_project_id"],
        merge_request_iid=row["merge_request_iid"],
        source_branch=row["source_branch"],
        target_branch=row["target_branch"],
        create_effect_id=str(row["create_effect_id"]),
        head_sha=row["head_sha"],
        creation_origin=row["creation_origin"],
        created_at=row["created_at"],
    )


def _observation_dto(row: Any) -> MergeRequestObservationDto:
    return MergeRequestObservationDto(
        id=str(row["id"]),
        binding_id=str(row["binding_id"]),
        head_sha=row["head_sha"],
        state=row["state"],
        merge_commit_sha=row["merge_commit_sha"],
        external_merge_user_id=row["external_merge_user_id"],
        merged_at=row["merged_at"],
        observation_digest=row["observation_digest"],
        observed_at=row["observed_at"],
    )


def _repository_profile(row: Any) -> GitLabRepositoryProfile:
    return GitLabRepositoryProfile(
        repository_id=str(row["id"]),
        project_id=row["project_id"],
        project_path=row["project_path"],
        connection_ref=row["connection_ref"],
        default_branch=row["default_branch"],
        credential_secret_ref=row["credential_secret_ref"],
    )


def _append_audit(
    repository: SourceControlIntegrationRepository,
    *,
    action: str,
    target_type: str,
    target_id: str,
    dependencies: SourceControlDependencies,
) -> None:
    dependencies.audit.append_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor="SYSTEM:SOURCE_CONTROL",
            actor_type="SYSTEM",
            action=action,
            target_type=target_type,
            target_id=target_id,
            result="SUCCESS",
            correlation_id=f"source-control:effect:{target_id}",
        ),
    )


def _observation_digest(snapshot: GitLabMergeRequestSnapshot) -> str:
    payload = {
        "projectId": snapshot.project_id,
        "iid": snapshot.iid,
        "sourceBranch": snapshot.source_branch,
        "targetBranch": snapshot.target_branch,
        "headSha": snapshot.head_sha,
        "state": snapshot.state,
        "mergeCommitSha": snapshot.merge_commit_sha,
        "mergeUserId": snapshot.merge_user_id,
        "mergedAt": None if snapshot.merged_at is None else snapshot.merged_at.isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _snapshot_state(snapshot: GitLabMergeRequestSnapshot) -> MergeRequestState:
    return {
        "opened": MergeRequestState.OPEN,
        "merged": MergeRequestState.MERGED,
        "closed": MergeRequestState.CLOSED,
        "locked": MergeRequestState.LOCKED,
    }[snapshot.state]


def _set_callback_state(
    effect: SourceControlEffectDto,
    *,
    callback_state: RequirementCallbackState,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=effect.state.value,
            expected_attempts=effect.attempts,
            values={
                "requirement_callback_state": callback_state.value,
                "updated_at": dependencies.clock.now(),
            },
        )
    if row is None:
        return effect
    return _effect_dto(row)


def _record_ready(
    context: RequirementDeliveryContext,
    effect: SourceControlEffectDto,
    binding: MergeRequestBindingDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    requirement = dependencies.requirement_delivery
    if requirement is None:
        raise SourceControlDependencyUnavailable("Requirement delivery dependency unavailable")
    try:
        requirement.record_mr_ready(
            IntegrationMrReadyResult(
                work_item_id=context.work_item_id,
                binding_id=binding.id,
                expected_revision=context.work_item_revision,
                idempotency_key=f"source-control:mr-ready:{effect.id}",
            )
        )
    except Exception:
        callback_state = RequirementCallbackState.FAILED
    else:
        callback_state = RequirementCallbackState.ACKED
    return _set_callback_state(
        effect,
        callback_state=callback_state,
        dependencies=dependencies,
    )


def _record_blocked(
    context: RequirementDeliveryContext,
    *,
    effect: SourceControlEffectDto | None,
    binding_id: str | None,
    reason_code: str,
    dependencies: SourceControlDependencies,
) -> RequirementCallbackState:
    requirement = dependencies.requirement_delivery
    if requirement is None:
        raise SourceControlDependencyUnavailable("Requirement delivery dependency unavailable")
    effect_marker = "preflight" if effect is None else effect.id
    try:
        requirement.record_blocked(
            IntegrationDeliveryBlockedResult(
                work_item_id=context.work_item_id,
                binding_id=binding_id,
                reason_code=reason_code,
                expected_revision=context.work_item_revision,
                idempotency_key=(
                    f"source-control:integration-blocked:{effect_marker}:{reason_code}"
                ),
            )
        )
    except Exception:
        return RequirementCallbackState.FAILED
    return RequirementCallbackState.ACKED


def _record_pending(
    context: RequirementDeliveryContext,
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    requirement = dependencies.requirement_delivery
    if requirement is None:
        raise SourceControlDependencyUnavailable("Requirement delivery dependency unavailable")
    try:
        requirement.record_pending(
            IntegrationReconciliationPendingResult(
                work_item_id=context.work_item_id,
                binding_id=None,
                expected_revision=context.work_item_revision,
                idempotency_key=f"source-control:integration-pending:{effect.id}",
            )
        )
    except Exception:
        callback_state = RequirementCallbackState.FAILED
    else:
        callback_state = RequirementCallbackState.ACKED
    return _set_callback_state(
        effect,
        callback_state=callback_state,
        dependencies=dependencies,
    )


def _mark_unknown(
    context: RequirementDeliveryContext,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
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
                "last_error_code": "EXTERNAL_RESULT_UNKNOWN",
                "next_reconcile_at": policy.next_reconcile_at(
                    now=now,
                    attempts=effect.attempts,
                ),
                "updated_at": now,
            },
        )
        if row is None:
            row = repository.effect_by_operation_subject(
                _CREATE_OPERATION.value,
                effect.subject_key,
            )
            if row is None:
                raise RequirementCallbackUnavailable("Integration MR effect is unavailable")
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=now,
        )
        if completed is None and row["state"] == EffectState.UNKNOWN.value:
            current_inbox = repository.delivery_request(message_id)
            if current_inbox is None or current_inbox["state"] != "PROCESSED":
                raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
    unknown = _effect_dto(row)
    unknown = _record_pending(context, unknown, dependencies=dependencies)
    return ProcessIntegrationRequestResult(
        effect=unknown,
        binding=None,
        observation=None,
        blocked_reason="RECONCILIATION_PENDING",
    )


def _replay_processed_request(
    context: RequirementDeliveryContext,
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
    if effect.state is EffectState.UNKNOWN:
        effect = _record_pending(context, effect, dependencies=dependencies)
        return ProcessIntegrationRequestResult(
            effect=effect,
            binding=None,
            observation=None,
            blocked_reason="RECONCILIATION_PENDING",
        )
    if effect.state is EffectState.BLOCKED and effect.last_error_code is not None:
        callback_state = _record_blocked(
            context,
            effect=effect,
            binding_id=None,
            reason_code=effect.last_error_code,
            dependencies=dependencies,
        )
        effect = _set_callback_state(
            effect,
            callback_state=callback_state,
            dependencies=dependencies,
        )
    return ProcessIntegrationRequestResult(
        effect=effect,
        binding=binding,
        observation=observation,
        blocked_reason=effect.last_error_code,
    )


def _complete_preflight_block(
    context: RequirementDeliveryContext,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    callback_state = _record_blocked(
        context,
        effect=None,
        binding_id=None,
        reason_code=reason_code,
        dependencies=dependencies,
    )
    if callback_state is RequirementCallbackState.FAILED:
        return ProcessIntegrationRequestResult(
            effect=None,
            binding=None,
            observation=None,
            blocked_reason=reason_code,
        )
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=now,
        )
        if completed is None:
            raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
    return ProcessIntegrationRequestResult(
        effect=None,
        binding=None,
        observation=None,
        blocked_reason=reason_code,
    )


def _complete_effect_block(
    context: RequirementDeliveryContext,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.IN_FLIGHT.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.BLOCKED.value,
                "last_error_code": reason_code,
                "next_reconcile_at": None,
                "completed_at": now,
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
    blocked = _effect_dto(row)
    callback_state = _record_blocked(
        context,
        effect=blocked,
        binding_id=None,
        reason_code=reason_code,
        dependencies=dependencies,
    )
    blocked = _set_callback_state(
        blocked,
        callback_state=callback_state,
        dependencies=dependencies,
    )
    return ProcessIntegrationRequestResult(
        effect=blocked,
        binding=None,
        observation=None,
        blocked_reason=reason_code,
    )


def process_integration_mr_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    requirement_delivery = dependencies.requirement_delivery
    requirement_binding = dependencies.requirement
    eligibility = dependencies.eligibility
    gitlab = dependencies.gitlab_merge_requests
    if (
        repository_factory is None
        or requirement_delivery is None
        or requirement_binding is None
        or eligibility is None
        or gitlab is None
        or dependencies.policy is None
    ):
        raise SourceControlDependencyUnavailable("Integration MR dependency unavailable")

    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        claimed = repository.claim_delivery_request(
            message_id,
            now=now,
            lease_until=now + timedelta(minutes=2),
        )
        inbox = claimed or repository.delivery_request(message_id)
    if inbox is None:
        raise RequirementCallbackUnavailable("Delivery request is unavailable")
    if inbox["topic"] != _CREATE_TOPIC:
        raise SourceControlDependencyUnavailable("Delivery request operation is invalid")

    context = requirement_delivery.delivery_context(str(inbox["work_item_id"]))
    binding_context = requirement_binding.binding_context(str(inbox["work_item_id"]))
    if (
        context.requirement_id != str(inbox["requirement_id"])
        or context.work_item_id != str(inbox["work_item_id"])
        or context.repository_id != str(inbox["repository_id"])
        or context.work_item_revision != inbox["work_item_revision"]
        or context.request_actor_id != inbox["actor_id"]
        or context.requirement_state != "IN_PROGRESS"
        or context.work_item_state != "IN_PROGRESS"
        or context.integration_delivery_state != "MR_PENDING"
        or context.integration_merge_request_binding_id is not None
        or binding_context.requirement_id != context.requirement_id
        or binding_context.workspace_id != context.workspace_id
        or binding_context.work_item_id != context.work_item_id
        or binding_context.work_item_revision != context.work_item_revision
        or binding_context.repository_id != context.repository_id
        or binding_context.required_capabilities != context.required_capabilities
        or binding_context.requirement_type not in _REQUIREMENT_TYPE_PREFIXES
    ):
        raise SourceControlDependencyUnavailable("Integration MR context is invalid")

    if claimed is None:
        if inbox["state"] == "PROCESSED":
            return _replay_processed_request(context, dependencies=dependencies)
        raise RequirementCallbackUnavailable("Delivery request is unavailable")

    if (
        context.human_owner_id != inbox["actor_id"]
        or binding_context.assignment_state != "ASSIGNED"
        or binding_context.human_owner_id != context.human_owner_id
        or binding_context.human_owner_id != inbox["actor_id"]
    ):
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="OWNER_MISMATCH",
            dependencies=dependencies,
        )

    if context.repository_state != "BOUND":
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="REPOSITORY_NOT_AUTHORIZED",
            dependencies=dependencies,
        )

    if context.base_commit_sha is None or context.task_branch is None:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="BRANCH_BINDING_MISSING",
            dependencies=dependencies,
        )

    owner = eligibility.evaluate(binding_context)
    if not owner.eligible:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="OWNER_INELIGIBLE",
            dependencies=dependencies,
        )

    with dependencies.engine.connect() as db:
        source_control = dependencies.repository_factory(db)
        repository_row = source_control.workspace_repository(context.repository_id)
        branch_row = repository_factory(db).branch_binding_by_work_item(context.work_item_id)
    if (
        repository_row is None
        or repository_row["status"] != "AUTHORIZED"
        or str(repository_row["workspace_id"]) != context.workspace_id
    ):
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="REPOSITORY_NOT_AUTHORIZED",
            dependencies=dependencies,
        )
    if (
        branch_row is None
        or str(branch_row["id"]) == ""
        or str(branch_row["requirement_id"]) != context.requirement_id
        or str(branch_row["workspace_id"]) != context.workspace_id
        or str(branch_row["repository_id"]) != context.repository_id
        or branch_row["branch_name"] != context.task_branch
        or branch_row["base_commit_sha"] != context.base_commit_sha
    ):
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="BRANCH_BINDING_MISSING",
            dependencies=dependencies,
        )

    profile = _repository_profile(repository_row)
    try:
        project = gitlab.get_project_delivery_profile(profile)
    except (GitLabAccessDenied, GitLabProjectNotFound):
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="REPOSITORY_NOT_AUTHORIZED",
            dependencies=dependencies,
        )
    except GitLabProviderUnavailable:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="PROVIDER_UNAVAILABLE",
            dependencies=dependencies,
        )
    try:
        source = gitlab.get_branch(profile, context.task_branch)
    except GitLabBranchNotFound:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="BRANCH_BINDING_MISSING",
            dependencies=dependencies,
        )
    except GitLabAccessDenied:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="REPOSITORY_NOT_AUTHORIZED",
            dependencies=dependencies,
        )
    except GitLabProviderUnavailable:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="PROVIDER_UNAVAILABLE",
            dependencies=dependencies,
        )
    try:
        target = gitlab.get_branch(profile, _TARGET_BRANCH)
    except GitLabBranchNotFound:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="TARGET_BRANCH_NOT_FOUND",
            dependencies=dependencies,
        )
    except GitLabAccessDenied:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="REPOSITORY_NOT_AUTHORIZED",
            dependencies=dependencies,
        )
    except GitLabProviderUnavailable:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="PROVIDER_UNAVAILABLE",
            dependencies=dependencies,
        )
    if (
        project.project_id != profile.project_id
        or project.project_path != profile.project_path
        or project.default_branch != profile.default_branch
        or project.merge_method != "merge"
        or source.name != context.task_branch
        or target.name != _TARGET_BRANCH
    ):
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="PROJECT_PROFILE_UNSUPPORTED",
            dependencies=dependencies,
        )
    if source.commit_sha == context.base_commit_sha:
        return _complete_preflight_block(
            context,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="NO_DELIVERY_COMMIT",
            dependencies=dependencies,
        )

    subject_key = f"work-item:{context.work_item_id}"
    payload = CreateIntegrationMergeRequestEffectPayload(
        branchBindingId=str(branch_row["id"]),
        headSha=source.commit_sha,
    )
    try:
        with dependencies.engine.begin() as db:
            repository = repository_factory(db)
            effect_row = repository.effect_by_operation_subject(
                _CREATE_OPERATION.value,
                subject_key,
                for_update=True,
            )
            if effect_row is None:
                effect_row = repository.insert_effect(
                    id=str(dependencies.random.uuid4()),
                    effect_key=f"source-control:create-integration-mr:{context.work_item_id}",
                    operation=_CREATE_OPERATION.value,
                    subject_key=subject_key,
                    payload=payload,
                    work_item_id=context.work_item_id,
                    requirement_id=context.requirement_id,
                    repository_id=context.repository_id,
                    request_fingerprint=inbox["payload_hash"],
                    attempts=0,
                    next_reconcile_at=None,
                    state=EffectState.PLANNED.value,
                    requirement_callback_state=RequirementCallbackState.PENDING.value,
                    now=dependencies.clock.now(),
                )
                _append_audit(
                    repository,
                    action="source_control.integration_mr.planned",
                    target_type="source_control_effect",
                    target_id=str(effect_row["id"]),
                    dependencies=dependencies,
                )
    except IntegrityError:
        with dependencies.engine.connect() as db:
            effect_row = repository_factory(db).effect_by_operation_subject(
                _CREATE_OPERATION.value,
                subject_key,
            )
        if effect_row is None:
            raise
    effect = _effect_dto(effect_row)

    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        in_flight_row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.PLANNED.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.IN_FLIGHT.value,
                "attempts": effect.attempts + 1,
                "next_reconcile_at": now + timedelta(minutes=2),
                "updated_at": dependencies.clock.now(),
            },
        )
        if in_flight_row is not None:
            _append_audit(
                repository,
                action="source_control.integration_mr.in_flight",
                target_type="source_control_effect",
                target_id=effect.id,
                dependencies=dependencies,
            )
    if in_flight_row is None:
        raise RequirementCallbackUnavailable("Integration MR effect lease was lost")
    effect = _effect_dto(in_flight_row)

    try:
        candidates = gitlab.list_merge_requests(
            profile,
            source_branch=context.task_branch,
            target_branch=_TARGET_BRANCH,
        )
    except GitLabResultUnknown:
        return _complete_effect_block(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MR_CONFLICT",
            dependencies=dependencies,
        )
    except (GitLabAccessDenied, GitLabProjectNotFound):
        return _complete_effect_block(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="REPOSITORY_NOT_AUTHORIZED",
            dependencies=dependencies,
        )
    except GitLabProviderUnavailable:
        return _complete_effect_block(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="PROVIDER_UNAVAILABLE",
            dependencies=dependencies,
        )
    if len(candidates) > 1:
        return _complete_effect_block(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MR_CONFLICT",
            dependencies=dependencies,
        )
    if candidates:
        created = candidates[0]
        if (
            created.project_id != profile.project_id
            or created.source_branch != context.task_branch
            or created.target_branch != _TARGET_BRANCH
            or created.head_sha != source.commit_sha
            or created.state != "opened"
        ):
            return _complete_effect_block(
                context,
                effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                reason_code="MR_CONFLICT",
                dependencies=dependencies,
            )
        creation_origin = MergeRequestCreationOrigin.EXTERNAL_ADOPTED
    else:
        try:
            created = gitlab.create_merge_request(
                profile,
                source_branch=context.task_branch,
                target_branch=_TARGET_BRANCH,
                expected_head_sha=source.commit_sha,
                title=f"{binding_context.requirement_type}: integrate {context.work_item_id}",
                description=(
                    f"Requirement: {context.requirement_id}\n"
                    f"Work-Item: {context.work_item_id}\n"
                    f"Source-Control-Effect: {effect.id}"
                ),
            )
        except (GitLabAccessDenied, GitLabProjectNotFound):
            return _complete_effect_block(
                context,
                effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                reason_code="REPOSITORY_NOT_AUTHORIZED",
                dependencies=dependencies,
            )
        except (GitLabResultUnknown, GitLabProviderUnavailable):
            return _mark_unknown(
                context,
                effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                dependencies=dependencies,
            )
        creation_origin = MergeRequestCreationOrigin.PLATFORM_CREATED
    try:
        readback = gitlab.get_merge_request(profile, iid=created.iid)
    except (
        GitLabMergeRequestNotFound,
        GitLabAccessDenied,
        GitLabProjectNotFound,
        GitLabProviderUnavailable,
        GitLabResultUnknown,
    ):
        return _mark_unknown(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    if (
        readback.project_id != profile.project_id
        or readback.source_branch != context.task_branch
        or readback.target_branch != _TARGET_BRANCH
        or readback.state != "opened"
    ):
        return _complete_effect_block(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MR_CONFLICT",
            dependencies=dependencies,
        )

    completed_at = dependencies.clock.now()
    observation_digest = _observation_digest(readback)
    try:
        with dependencies.engine.begin() as db:
            repository = repository_factory(db)
            binding_row = repository.insert_merge_request_binding(
                id=str(dependencies.random.uuid4()),
                kind=MergeRequestKind.INTEGRATION.value,
                work_item_id=context.work_item_id,
                requirement_id=context.requirement_id,
                workspace_id=context.workspace_id,
                repository_id=context.repository_id,
                branch_binding_id=str(branch_row["id"]),
                external_project_id=readback.project_id,
                merge_request_iid=readback.iid,
                source_branch=readback.source_branch,
                target_branch=readback.target_branch,
                create_effect_id=effect.id,
                head_sha=source.commit_sha,
                creation_origin=creation_origin.value,
                now=completed_at,
            )
            observation_row = repository.append_merge_request_observation(
                id=str(dependencies.random.uuid4()),
                binding_id=str(binding_row["id"]),
                head_sha=readback.head_sha,
                state=_snapshot_state(readback).value,
                merge_commit_sha=readback.merge_commit_sha,
                external_merge_user_id=readback.merge_user_id,
                merged_at=readback.merged_at,
                observation_digest=observation_digest,
                observed_at=completed_at,
            )
            succeeded_row = repository.transition_effect(
                effect.id,
                expected_state=EffectState.IN_FLIGHT.value,
                expected_attempts=effect.attempts,
                values={
                    "state": EffectState.SUCCEEDED.value,
                    "next_reconcile_at": None,
                    "completed_at": completed_at,
                    "updated_at": completed_at,
                },
            )
            if succeeded_row is None or observation_row is None:
                raise RequirementCallbackUnavailable("Integration MR effect lease was lost")
            completed_inbox = repository.complete_delivery_request(
                message_id,
                expected_attempts=claimed["attempts"],
                now=completed_at,
            )
            if completed_inbox is None:
                raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
            _append_audit(
                repository,
                action="source_control.integration_mr.succeeded",
                target_type="source_control_effect",
                target_id=effect.id,
                dependencies=dependencies,
            )
    except Exception:
        return _mark_unknown(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    effect = _effect_dto(succeeded_row)
    binding = _binding_dto(binding_row)
    observation = _observation_dto(observation_row)
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
