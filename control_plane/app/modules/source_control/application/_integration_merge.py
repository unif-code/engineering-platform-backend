from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from control_plane.app.modules.source_control.application._integration_callbacks import (
    _record_effect_callback,
)
from control_plane.app.modules.source_control.application._integration_common import (
    TARGET_BRANCH as _TARGET_BRANCH,
)
from control_plane.app.modules.source_control.application._integration_common import (
    EffectCollision as _EffectCollision,
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
    repository_profile as _repository_profile,
)
from control_plane.app.modules.source_control.application._integration_common import (
    snapshot_state as _snapshot_state,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectOperation,
    EffectState,
    MergeIntegrationMergeRequestEffectPayload,
    MergeRequestBindingDto,
    MergeRequestObservationDto,
    MergeRequestState,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
    merge_effect_subject,
)
from control_plane.app.modules.source_control.ports import (
    BranchSnapshot,
    ExternalMergeDriftResult,
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabMergeRequestBlocked,
    GitLabMergeRequestHeadChanged,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
    GitLabProjectNotFound,
    GitLabProjectPolicyUnsupported,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
    GitLabTargetBranchNotProtected,
    IntegrationDeliveryBlockedResult,
    RequirementBindingContext,
    RequirementDeliveryContext,
)

_MERGE_OPERATION = EffectOperation.MERGE_INTEGRATION_MR
_MERGE_TOPIC = "requirement.integration-merge.requested"
_MERGE_CAPABILITY = "merge_request.merge"
_MERGE_PREFLIGHT_OUTCOME_REASONS = frozenset(
    {
        "BRANCH_BINDING_MISSING",
        "EXTERNAL_MERGE_DRIFT",
        "HEAD_SHA_CHANGED",
        "MERGE_ACTOR_INELIGIBLE",
        "MERGE_CONFLICT",
        "MR_CHECKS_BLOCKED",
        "MR_CLOSED",
        "MR_CONFLICT",
        "OWNER_MISMATCH",
        "PROJECT_PROFILE_UNSUPPORTED",
        "REPOSITORY_NOT_AUTHORIZED",
        "TARGET_BRANCH_NOT_FOUND",
        "TARGET_BRANCH_NOT_PROTECTED",
    }
)


@dataclass(frozen=True, slots=True)
class _MergeAdmission:
    context: RequirementDeliveryContext
    binding_context: RequirementBindingContext
    repository_profile: GitLabRepositoryProfile
    branch_binding_id: str
    binding: MergeRequestBindingDto
    latest_observation: MergeRequestObservationDto
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class _MergeProviderProof:
    project: GitLabProjectDeliveryProfile
    source: BranchSnapshot
    target: BranchSnapshot
    merge_request: GitLabMergeRequestSnapshot

    @property
    def current_head_sha(self) -> str:
        return self.source.commit_sha


@dataclass(frozen=True, slots=True)
class _MergePreflightBlocked(Exception):
    reason_code: str


class _MergePreflightTransient(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _StoredMergeFacts:
    binding: MergeRequestBindingDto
    observation: MergeRequestObservationDto
    effect: SourceControlEffectDto | None


def _read_stored_merge_facts(
    inbox: Any,
    *,
    include_effect: bool = True,
    dependencies: SourceControlDependencies,
) -> _StoredMergeFacts:
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    binding_id = inbox["integration_merge_request_binding_id"]
    if binding_id is None:
        raise SourceControlDependencyUnavailable("Integration merge binding is unavailable")
    with dependencies.engine.connect() as db:
        repository = repository_factory(db)
        binding_row = repository.merge_request_binding_by_id(str(binding_id))
        work_item_binding_row = repository.merge_request_binding_by_work_item(
            str(inbox["work_item_id"])
        )
        observation_row = (
            None
            if binding_row is None
            else repository.latest_merge_request_observation(str(binding_id))
        )
        effect_row = (
            repository.effect_by_operation_work_item_fingerprint(
                _MERGE_OPERATION.value,
                str(inbox["work_item_id"]),
                inbox["payload_hash"],
            )
            if include_effect
            else None
        )
    if binding_row is None or work_item_binding_row is None or observation_row is None:
        raise SourceControlDependencyUnavailable("Integration merge facts are unavailable")
    try:
        binding = _binding_dto(binding_row)
        work_item_binding = _binding_dto(work_item_binding_row)
        observation = _observation_dto(observation_row)
        effect = None if effect_row is None else _effect_dto(effect_row)
    except (TypeError, ValueError):
        raise _EffectCollision from None
    if (
        binding != work_item_binding
        or binding.id != str(binding_id)
        or binding.work_item_id != str(inbox["work_item_id"])
        or binding.requirement_id != str(inbox["requirement_id"])
        or binding.repository_id != str(inbox["repository_id"])
        or observation.binding_id != binding.id
    ):
        raise SourceControlDependencyUnavailable("Integration merge facts are invalid")
    if effect is not None:
        payload = effect.payload
        if (
            effect.operation is not _MERGE_OPERATION
            or effect.work_item_id != str(inbox["work_item_id"])
            or effect.requirement_id != str(inbox["requirement_id"])
            or effect.repository_id != str(inbox["repository_id"])
            or effect.request_fingerprint != inbox["payload_hash"]
            or not isinstance(payload, MergeIntegrationMergeRequestEffectPayload)
            or payload.binding_id != binding.id
            or effect.subject_key != merge_effect_subject(binding.id, payload.requested_head_sha)
        ):
            raise _EffectCollision
    return _StoredMergeFacts(
        binding=binding,
        observation=observation,
        effect=effect,
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
        elif effect.state is EffectState.BLOCKED and effect.last_error_code is not None:
            effect = _record_effect_callback(
                callback_subject,
                effect,
                kind=(
                    "external_drift"
                    if effect.last_error_code == "EXTERNAL_MERGE_DRIFT"
                    else "blocked"
                ),
                binding_id=facts.binding.id,
                reason_code=effect.last_error_code,
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
    blocked_reason = (
        None
        if effect.state is EffectState.SUCCEEDED
        else (
            "RECONCILIATION_PENDING"
            if effect.state
            in {
                EffectState.IN_FLIGHT,
                EffectState.UNKNOWN,
                EffectState.RECONCILIATION,
            }
            else effect.last_error_code
        )
    )
    return ProcessIntegrationRequestResult(
        effect=effect,
        binding=facts.binding,
        observation=facts.observation,
        blocked_reason=blocked_reason,
    )


def _read_merge_admission(
    inbox: Any,
    *,
    dependencies: SourceControlDependencies,
) -> _MergeAdmission:
    requirement_delivery = dependencies.requirement_delivery
    requirement_binding = dependencies.requirement
    eligibility = dependencies.eligibility
    repository_factory = dependencies.delivery_repository_factory
    if (
        requirement_delivery is None
        or requirement_binding is None
        or eligibility is None
        or repository_factory is None
    ):
        raise SourceControlDependencyUnavailable("Integration merge admission unavailable")
    context = requirement_delivery.delivery_context(str(inbox["work_item_id"]))
    binding_context = requirement_binding.binding_context(str(inbox["work_item_id"]))
    if (
        context.requirement_id != str(inbox["requirement_id"])
        or context.requirement_revision != inbox["requirement_revision"]
        or context.work_item_id != str(inbox["work_item_id"])
        or context.work_item_revision != inbox["work_item_revision"]
        or context.repository_id != str(inbox["repository_id"])
        or context.request_actor_id != inbox["actor_id"]
        or context.requirement_state != "VERIFYING"
        or context.work_item_state != "VERIFYING"
        or context.integration_delivery_state != "MERGE_PENDING"
        or context.integration_merge_request_binding_id
        != str(inbox["integration_merge_request_binding_id"])
        or binding_context.requirement_id != context.requirement_id
        or binding_context.workspace_id != context.workspace_id
        or binding_context.work_item_id != context.work_item_id
        or binding_context.work_item_revision != context.work_item_revision
        or binding_context.repository_id != context.repository_id
        or binding_context.required_capabilities != context.required_capabilities
    ):
        raise SourceControlDependencyUnavailable("Integration merge context is invalid")
    blocked_reason: str | None = None
    if (
        context.human_owner_id != inbox["actor_id"]
        or binding_context.assignment_state != "ASSIGNED"
        or binding_context.human_owner_id != context.human_owner_id
        or binding_context.human_owner_id != inbox["actor_id"]
    ):
        blocked_reason = "OWNER_MISMATCH"
    elif context.repository_state != "BOUND":
        blocked_reason = "REPOSITORY_NOT_AUTHORIZED"
    required_capabilities = tuple(
        dict.fromkeys((*binding_context.required_capabilities, _MERGE_CAPABILITY))
    )
    merge_context = binding_context.model_copy(
        update={"required_capabilities": required_capabilities}
    )
    if blocked_reason is None and not eligibility.evaluate(merge_context).eligible:
        blocked_reason = "MERGE_ACTOR_INELIGIBLE"
    binding_id = context.integration_merge_request_binding_id
    if binding_id is None:
        raise SourceControlDependencyUnavailable("Integration merge binding is unavailable")
    with dependencies.engine.connect() as db:
        repository_row = dependencies.repository_factory(db).workspace_repository(
            context.repository_id
        )
        repository = repository_factory(db)
        branch_row = repository.branch_binding_by_work_item(context.work_item_id)
        binding_row = repository.merge_request_binding_by_id(binding_id)
        work_item_binding_row = repository.merge_request_binding_by_work_item(context.work_item_id)
        observation_row = (
            None if binding_row is None else repository.latest_merge_request_observation(binding_id)
        )
    if (
        repository_row is None
        or repository_row["status"] != "AUTHORIZED"
        or str(repository_row["workspace_id"]) != context.workspace_id
        or branch_row is None
        or binding_row is None
        or work_item_binding_row is None
        or observation_row is None
    ):
        raise SourceControlDependencyUnavailable("Integration merge facts are unavailable")
    binding = _binding_dto(binding_row)
    work_item_binding = _binding_dto(work_item_binding_row)
    latest_observation = _observation_dto(observation_row)
    if (
        str(branch_row["id"]) != binding.branch_binding_id
        or str(branch_row["requirement_id"]) != context.requirement_id
        or str(branch_row["workspace_id"]) != context.workspace_id
        or str(branch_row["repository_id"]) != context.repository_id
        or branch_row["branch_name"] != context.task_branch
        or branch_row["base_commit_sha"] != context.base_commit_sha
        or binding.id != binding_id
        or work_item_binding != binding
        or binding.requirement_id != context.requirement_id
        or binding.workspace_id != context.workspace_id
        or binding.work_item_id != context.work_item_id
        or binding.repository_id != context.repository_id
        or binding.external_project_id != str(repository_row["project_id"])
        or binding.source_branch != context.task_branch
        or binding.target_branch != _TARGET_BRANCH
        or latest_observation.binding_id != binding.id
        or latest_observation.state is not MergeRequestState.OPEN
    ):
        raise SourceControlDependencyUnavailable("Integration merge facts are invalid")
    return _MergeAdmission(
        context=context,
        binding_context=merge_context,
        repository_profile=_repository_profile(repository_row),
        branch_binding_id=str(branch_row["id"]),
        binding=binding,
        latest_observation=latest_observation,
        blocked_reason=blocked_reason,
    )


def _read_merge_provider_proof(
    admission: _MergeAdmission,
    *,
    dependencies: SourceControlDependencies,
) -> _MergeProviderProof:
    gitlab = dependencies.gitlab_merge_requests
    if gitlab is None:
        raise SourceControlDependencyUnavailable("Integration merge provider unavailable")
    profile = admission.repository_profile
    try:
        project = gitlab.get_project_delivery_profile(profile)
        merge_request = gitlab.get_merge_request(
            profile,
            iid=admission.binding.merge_request_iid,
        )
        source = gitlab.get_branch(profile, admission.binding.source_branch)
        target = gitlab.get_branch(profile, _TARGET_BRANCH)
    except GitLabProjectPolicyUnsupported:
        raise _MergePreflightBlocked("PROJECT_PROFILE_UNSUPPORTED") from None
    except GitLabTargetBranchNotProtected:
        raise _MergePreflightBlocked("TARGET_BRANCH_NOT_PROTECTED") from None
    except GitLabMergeRequestNotFound:
        raise _MergePreflightBlocked("MR_CLOSED") from None
    except GitLabBranchNotFound:
        raise _MergePreflightBlocked("BRANCH_BINDING_MISSING") from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _MergePreflightBlocked("REPOSITORY_NOT_AUTHORIZED") from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _MergePreflightTransient from None
    if (
        project.project_id != profile.project_id
        or project.project_path != profile.project_path
        or project.default_branch != profile.default_branch
        or project.merge_method != "merge"
    ):
        raise _MergePreflightBlocked("PROJECT_PROFILE_UNSUPPORTED")
    if (
        merge_request.project_id != admission.binding.external_project_id
        or merge_request.iid != admission.binding.merge_request_iid
        or merge_request.source_branch != admission.binding.source_branch
        or merge_request.target_branch != _TARGET_BRANCH
    ):
        raise _MergePreflightBlocked("MR_CONFLICT")
    if source.name != admission.binding.source_branch:
        raise _MergePreflightBlocked("BRANCH_BINDING_MISSING")
    if target.name != _TARGET_BRANCH:
        raise _MergePreflightBlocked("TARGET_BRANCH_NOT_FOUND")
    if merge_request.head_sha != source.commit_sha:
        raise _MergePreflightBlocked("HEAD_SHA_CHANGED")
    return _MergeProviderProof(
        project=project,
        source=source,
        target=target,
        merge_request=merge_request,
    )


def _provider_block_reason(proof: _MergeProviderProof) -> str | None:
    merge_request = proof.merge_request
    if merge_request.state == "merged":
        return "EXTERNAL_MERGE_DRIFT"
    if merge_request.state == "closed":
        return "MR_CLOSED"
    if merge_request.state == "locked":
        return "MR_CHECKS_BLOCKED"
    if merge_request.has_conflicts:
        return "MERGE_CONFLICT"
    if (
        merge_request.detailed_merge_status != "mergeable"
        or not merge_request.blocking_discussions_resolved
        or merge_request.head_pipeline_status != "success"
    ):
        return "MR_CHECKS_BLOCKED"
    return None


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
            dependencies=dependencies,
        )
    return _effect_dto(in_flight)


def _complete_planned_head_change(
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
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.PLANNED.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.BLOCKED.value,
                "last_error_code": "HEAD_SHA_CHANGED",
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
        if row is None or completed is None:
            raise RequirementCallbackUnavailable("Integration merge lease was lost")
        _append_audit(
            repository,
            action="source_control.integration_merge.blocked",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
        )
    blocked = _effect_dto(row)
    callback_subject = _OriginatingCallbackSubject(
        work_item_id=admission.context.work_item_id,
        work_item_revision=admission.context.work_item_revision,
    )
    blocked = _record_effect_callback(
        callback_subject,
        blocked,
        kind="blocked",
        binding_id=admission.binding.id,
        reason_code="HEAD_SHA_CHANGED",
        operation=_MERGE_OPERATION,
        dependencies=dependencies,
    )
    return ProcessIntegrationRequestResult(
        effect=blocked,
        binding=admission.binding,
        observation=admission.latest_observation,
        blocked_reason="HEAD_SHA_CHANGED",
    )


def _complete_merge_preflight_block(
    admission: _MergeAdmission,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    requirement = dependencies.requirement_delivery
    if repository_factory is None or requirement is None:
        raise SourceControlDependencyUnavailable("Integration merge callback unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        marked = repository_factory(db).record_preflight_outcome(
            message_id,
            expected_attempts=inbox_attempts,
            reason_code=reason_code,
            now=now,
        )
        if marked is None:
            raise RequirementCallbackUnavailable("Integration merge inbox lease was lost")
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        locked = repository.delivery_request(message_id, for_update=True)
        if (
            locked is None
            or locked["state"] != "PROCESSING"
            or locked["attempts"] != inbox_attempts
            or locked["last_error_code"] != reason_code
        ):
            raise RequirementCallbackUnavailable("Integration merge inbox lease was lost")
        try:
            requirement.record_blocked(
                IntegrationDeliveryBlockedResult(
                    work_item_id=admission.context.work_item_id,
                    binding_id=admission.binding.id,
                    reason_code=reason_code,
                    expected_revision=admission.context.work_item_revision,
                    idempotency_key=(
                        f"source-control:integration-merge-blocked:{message_id}:{reason_code}"
                    ),
                )
            )
        except Exception:
            return ProcessIntegrationRequestResult(
                effect=None,
                binding=admission.binding,
                observation=admission.latest_observation,
                blocked_reason=reason_code,
            )
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=dependencies.clock.now(),
        )
        if completed is None:
            raise RequirementCallbackUnavailable("Integration merge inbox lease was lost")
    return ProcessIntegrationRequestResult(
        effect=None,
        binding=admission.binding,
        observation=admission.latest_observation,
        blocked_reason=reason_code,
    )


def _replay_merge_preflight_callback(
    inbox: Any,
    facts: _StoredMergeFacts,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    requirement = dependencies.requirement_delivery
    if repository_factory is None or requirement is None:
        raise SourceControlDependencyUnavailable("Integration merge callback unavailable")
    with dependencies.engine.begin() as db:
        repository = repository_factory(db)
        locked = repository.delivery_request(message_id, for_update=True)
        if (
            locked is None
            or locked["state"] != "PROCESSING"
            or locked["attempts"] != inbox_attempts
            or locked["last_error_code"] != reason_code
        ):
            raise RequirementCallbackUnavailable("Integration merge inbox lease was lost")
        try:
            if reason_code == "EXTERNAL_MERGE_DRIFT":
                requirement.record_external_merge_drift(
                    ExternalMergeDriftResult(
                        work_item_id=str(inbox["work_item_id"]),
                        binding_id=facts.binding.id,
                        expected_revision=inbox["work_item_revision"],
                        idempotency_key=f"source-control:external-merge-drift:{message_id}",
                    )
                )
            else:
                requirement.record_blocked(
                    IntegrationDeliveryBlockedResult(
                        work_item_id=str(inbox["work_item_id"]),
                        binding_id=facts.binding.id,
                        reason_code=reason_code,
                        expected_revision=inbox["work_item_revision"],
                        idempotency_key=(
                            f"source-control:integration-merge-blocked:{message_id}:{reason_code}"
                        ),
                    )
                )
        except Exception:
            return ProcessIntegrationRequestResult(
                effect=None,
                binding=facts.binding,
                observation=facts.observation,
                blocked_reason=reason_code,
            )
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=dependencies.clock.now(),
        )
        if completed is None:
            raise RequirementCallbackUnavailable("Integration merge inbox lease was lost")
    return ProcessIntegrationRequestResult(
        effect=None,
        binding=facts.binding,
        observation=facts.observation,
        blocked_reason=reason_code,
    )


def _mark_merge_unknown(
    admission: _MergeAdmission,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    policy = dependencies.policy
    if repository_factory is None or policy is None:
        raise SourceControlDependencyUnavailable("Integration merge recovery unavailable")
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
        completed = repository.complete_delivery_request(
            message_id,
            expected_attempts=inbox_attempts,
            now=now,
        )
        if row is None or completed is None:
            raise RequirementCallbackUnavailable("Integration merge lease was lost")
    unknown = _effect_dto(row)
    unknown = _record_effect_callback(
        _OriginatingCallbackSubject(
            work_item_id=admission.context.work_item_id,
            work_item_revision=admission.context.work_item_revision,
        ),
        unknown,
        kind="pending",
        binding_id=admission.binding.id,
        operation=_MERGE_OPERATION,
        dependencies=dependencies,
    )
    return ProcessIntegrationRequestResult(
        effect=unknown,
        binding=admission.binding,
        observation=admission.latest_observation,
        blocked_reason="RECONCILIATION_PENDING",
    )


def _complete_merge_effect_block(
    admission: _MergeAdmission,
    effect: SourceControlEffectDto,
    *,
    message_id: str,
    inbox_attempts: int,
    reason_code: str,
    readback: GitLabMergeRequestSnapshot | None = None,
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
                binding_id=admission.binding.id,
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
                "last_error_code": reason_code,
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
            raise RequirementCallbackUnavailable("Integration merge lease was lost")
        _append_audit(
            repository,
            action="source_control.integration_merge.blocked",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
        )
    blocked = _effect_dto(row)
    blocked = _record_effect_callback(
        _OriginatingCallbackSubject(
            work_item_id=admission.context.work_item_id,
            work_item_revision=admission.context.work_item_revision,
        ),
        blocked,
        kind=("external_drift" if reason_code == "EXTERNAL_MERGE_DRIFT" else "blocked"),
        binding_id=admission.binding.id,
        reason_code=reason_code,
        operation=_MERGE_OPERATION,
        dependencies=dependencies,
    )
    observation = (
        admission.latest_observation
        if observation_row is None
        else _observation_dto(observation_row)
    )
    return ProcessIntegrationRequestResult(
        effect=blocked,
        binding=admission.binding,
        observation=observation,
        blocked_reason=reason_code,
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
            reason_code="EXTERNAL_MERGE_DRIFT",
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
            or locked["last_error_code"] != "EXTERNAL_MERGE_DRIFT"
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
                blocked_reason="EXTERNAL_MERGE_DRIFT",
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
        blocked_reason="EXTERNAL_MERGE_DRIFT",
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


def _process_integration_merge_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if (
        repository_factory is None
        or dependencies.requirement_delivery is None
        or dependencies.requirement is None
        or dependencies.eligibility is None
        or dependencies.gitlab_merge_requests is None
        or dependencies.policy is None
    ):
        raise SourceControlDependencyUnavailable("Integration merge dependency unavailable")
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
    if inbox["topic"] != _MERGE_TOPIC:
        raise SourceControlDependencyUnavailable("Delivery request operation is invalid")
    persisted_preflight_reason = inbox["last_error_code"]
    if persisted_preflight_reason in _MERGE_PREFLIGHT_OUTCOME_REASONS:
        preflight_facts = _read_stored_merge_facts(
            inbox,
            include_effect=False,
            dependencies=dependencies,
        )
        if claimed is None:
            if inbox["state"] != "PROCESSED":
                raise RequirementCallbackUnavailable("Delivery request is unavailable")
            return ProcessIntegrationRequestResult(
                effect=None,
                binding=preflight_facts.binding,
                observation=preflight_facts.observation,
                blocked_reason=persisted_preflight_reason,
            )
        return _replay_merge_preflight_callback(
            inbox,
            preflight_facts,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=persisted_preflight_reason,
            dependencies=dependencies,
        )
    try:
        stored_facts = _read_stored_merge_facts(inbox, dependencies=dependencies)
    except _EffectCollision:
        if claimed is None:
            raise RequirementCallbackUnavailable("Delivery request is unavailable") from None
        admission = _read_merge_admission(inbox, dependencies=dependencies)
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MR_CONFLICT",
            dependencies=dependencies,
        )
    if stored_facts.effect is not None and stored_facts.effect.state is not EffectState.PLANNED:
        if claimed is None and inbox["state"] != "PROCESSED":
            raise RequirementCallbackUnavailable("Delivery request is unavailable")
        return _replay_merge_effect(
            inbox,
            stored_facts,
            claimed_attempts=None if claimed is None else claimed["attempts"],
            dependencies=dependencies,
        )
    if claimed is None:
        if inbox["state"] == "PROCESSED":
            return ProcessIntegrationRequestResult(
                effect=None,
                binding=stored_facts.binding,
                observation=stored_facts.observation,
                blocked_reason=inbox["last_error_code"],
            )
        raise RequirementCallbackUnavailable("Delivery request is unavailable")

    admission = _read_merge_admission(inbox, dependencies=dependencies)
    if admission.blocked_reason is not None:
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=admission.blocked_reason,
            dependencies=dependencies,
        )
    try:
        proof = _read_merge_provider_proof(admission, dependencies=dependencies)
    except _MergePreflightBlocked as blocked:
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=blocked.reason_code,
            dependencies=dependencies,
        )
    except _MergePreflightTransient:
        with dependencies.engine.begin() as db:
            released = repository_factory(db).release_delivery_request(
                message_id,
                expected_attempts=claimed["attempts"],
                error_code="PROVIDER_UNAVAILABLE",
                retry_at=dependencies.clock.now() + timedelta(minutes=1),
                now=dependencies.clock.now(),
            )
            if released is None:
                raise RequirementCallbackUnavailable(
                    "Integration merge inbox lease was lost"
                ) from None
        raise RequirementCallbackUnavailable("Integration merge provider is unavailable") from None
    existing_effect = stored_facts.effect
    if existing_effect is not None:
        payload = _validated_merge_effect(
            existing_effect,
            admission=admission,
            request_fingerprint=inbox["payload_hash"],
        )
        if payload is None:
            raise _EffectCollision
        if existing_effect.state is not EffectState.PLANNED:
            raise RequirementCallbackUnavailable("Integration merge effect is not writable")
        if payload.requested_head_sha != proof.current_head_sha:
            return _complete_planned_head_change(
                admission,
                existing_effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                dependencies=dependencies,
            )
    provider_reason = _provider_block_reason(proof)
    if provider_reason is not None:
        if existing_effect is not None:
            return _complete_merge_effect_block(
                admission,
                existing_effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                reason_code=provider_reason,
                readback=(
                    proof.merge_request if provider_reason == "EXTERNAL_MERGE_DRIFT" else None
                ),
                dependencies=dependencies,
            )
        if provider_reason == "EXTERNAL_MERGE_DRIFT":
            return _commit_external_merge_drift(
                admission,
                proof.merge_request,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                dependencies=dependencies,
            )
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=provider_reason,
            dependencies=dependencies,
        )
    try:
        effect = _acquire_merge_effect(
            admission,
            requested_head_sha=proof.current_head_sha,
            request_fingerprint=inbox["payload_hash"],
            dependencies=dependencies,
        )
    except _EffectCollision:
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MR_CONFLICT",
            dependencies=dependencies,
        )
    acquired_payload = effect.payload
    if not isinstance(acquired_payload, MergeIntegrationMergeRequestEffectPayload):
        raise _EffectCollision
    gitlab = dependencies.gitlab_merge_requests
    assert gitlab is not None
    try:
        gitlab.merge_merge_request(
            admission.repository_profile,
            iid=admission.binding.merge_request_iid,
            expected_head_sha=acquired_payload.requested_head_sha,
        )
    except GitLabMergeRequestHeadChanged:
        return _complete_merge_effect_block(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="HEAD_SHA_CHANGED",
            dependencies=dependencies,
        )
    except GitLabMergeRequestBlocked:
        return _complete_merge_effect_block(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MERGE_CONFLICT",
            dependencies=dependencies,
        )
    except (GitLabResultUnknown, GitLabProviderUnavailable):
        return _mark_merge_unknown(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    try:
        readback = gitlab.get_merge_request(
            admission.repository_profile,
            iid=admission.binding.merge_request_iid,
        )
    except (
        GitLabMergeRequestNotFound,
        GitLabAccessDenied,
        GitLabProjectNotFound,
        GitLabProviderUnavailable,
        GitLabResultUnknown,
    ):
        return _mark_merge_unknown(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    merged_coordinates_valid = (
        readback.state == "merged"
        and readback.project_id == proof.merge_request.project_id
        and readback.iid == proof.merge_request.iid
        and readback.source_branch == admission.binding.source_branch
        and readback.target_branch == _TARGET_BRANCH
        and readback.head_sha == acquired_payload.requested_head_sha
        and readback.merge_commit_sha is not None
        and readback.merged_at is not None
    )
    if not merged_coordinates_valid:
        if readback.head_sha != acquired_payload.requested_head_sha:
            return _complete_merge_effect_block(
                admission,
                effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                reason_code="HEAD_SHA_CHANGED",
                dependencies=dependencies,
            )
        if readback.state == "closed":
            return _complete_merge_effect_block(
                admission,
                effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                reason_code="MR_CLOSED",
                dependencies=dependencies,
            )
        return _mark_merge_unknown(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    try:
        source_readback = gitlab.get_branch(
            admission.repository_profile,
            admission.binding.source_branch,
        )
    except GitLabBranchNotFound:
        return _complete_merge_effect_block(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="SOURCE_BRANCH_MISSING_AFTER_INTEGRATION",
            readback=readback,
            dependencies=dependencies,
        )
    except (
        GitLabAccessDenied,
        GitLabProjectNotFound,
        GitLabProviderUnavailable,
        GitLabResultUnknown,
    ):
        return _mark_merge_unknown(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    if (
        source_readback.name != admission.binding.source_branch
        or source_readback.commit_sha != acquired_payload.requested_head_sha
    ):
        return _complete_merge_effect_block(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="HEAD_SHA_CHANGED",
            readback=readback,
            dependencies=dependencies,
        )
    try:
        effect, observation = _commit_merge_success(
            admission,
            effect,
            readback,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    except Exception:
        return _resolve_merge_fact_commit(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    callback_subject = _OriginatingCallbackSubject(
        work_item_id=admission.context.work_item_id,
        work_item_revision=admission.context.work_item_revision,
    )
    effect = _record_effect_callback(
        callback_subject,
        effect,
        kind="merged",
        binding_id=admission.binding.id,
        operation=_MERGE_OPERATION,
        dependencies=dependencies,
    )
    return ProcessIntegrationRequestResult(
        effect=effect,
        binding=admission.binding,
        observation=observation,
        blocked_reason=None,
    )
