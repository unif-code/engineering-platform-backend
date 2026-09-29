from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application._batch_claim import (
    InboxClaimLost,
    InboxProcessingFailed,
)
from control_plane.app.modules.source_control.application._eligibility import (
    actor_eligibility_context,
)
from control_plane.app.modules.source_control.application._integration_common import (
    binding_dto,
    effect_dto,
    observation_digest,
    observation_dto,
    repository_profile,
    snapshot_state,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    CreateFormalMergeRequestEffectPayload,
    EffectOperation,
    EffectState,
    FormalDeliveryConflict,
    FormalDeliveryRequestEnvelope,
    FormalDeliveryRequestKind,
    FormalReviewAssignmentDto,
    FormalReviewRoutingSnapshot,
    MergeFormalMergeRequestEffectPayload,
    MergeRequestCreationOrigin,
    MergeRequestKind,
    ProcessFormalDeliveryResult,
    RequirementCallbackState,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import (
    FormalDeliveryAdmission,
    FormalDeliveryBlockedCallback,
    FormalMergedCallback,
    FormalMrReadyCallback,
    FormalReconciliationPendingCallback,
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabFormalMergeRequestPort,
    GitLabMergeRequestBlocked,
    GitLabMergeRequestHeadChanged,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectNotFound,
    GitLabProjectPolicyUnsupported,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
    GitLabTargetBranchNotProtected,
    SourceControlFormalRepository,
    SourceControlFormalRepositoryFactory,
)

_CREATE = EffectOperation.CREATE_FORMAL_MR
_MERGE = EffectOperation.MERGE_FORMAL_MR
_FORMAL_REQUEST_CAPABILITY = "formal_merge_request.request"
_FORMAL_MERGE_CAPABILITY = "merge_request.merge"
_CREATE_TOPIC = "requirement.formal-merge-request.requested"
_MERGE_TOPIC = "requirement.formal-merge.requested"


class _FormalPreflightBlocked(Exception):
    def __init__(self, reason_code: SourceControlReason) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


_DETERMINISTIC_PROVIDER_ERRORS = (
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabMergeRequestBlocked,
    GitLabMergeRequestHeadChanged,
    GitLabMergeRequestNotFound,
    GitLabProjectNotFound,
    GitLabProjectPolicyUnsupported,
    GitLabTargetBranchNotProtected,
)


def _provider_block_reason(error: Exception) -> SourceControlReason:
    if isinstance(error, GitLabBranchNotFound):
        return SourceControlReason.SOURCE_BRANCH_MISSING_AFTER_INTEGRATION
    if isinstance(error, GitLabMergeRequestHeadChanged):
        return SourceControlReason.HEAD_SHA_CHANGED
    if isinstance(error, GitLabMergeRequestBlocked):
        return SourceControlReason.MR_CHECKS_BLOCKED
    if isinstance(error, GitLabMergeRequestNotFound):
        return SourceControlReason.MR_CLOSED
    if isinstance(error, GitLabTargetBranchNotProtected):
        return SourceControlReason.TARGET_BRANCH_NOT_PROTECTED
    if isinstance(error, GitLabProjectPolicyUnsupported):
        return SourceControlReason.PROJECT_PROFILE_UNSUPPORTED
    if isinstance(error, (GitLabAccessDenied, GitLabProjectNotFound)):
        return SourceControlReason.REPOSITORY_NOT_AUTHORIZED
    raise TypeError("Formal provider error is not deterministic")


def _merge_readback_block_reason(
    snapshot: GitLabMergeRequestSnapshot,
) -> SourceControlReason | None:
    if snapshot.state in {"closed", "locked"}:
        return SourceControlReason.MR_CLOSED
    if snapshot.has_conflicts:
        return SourceControlReason.MERGE_CONFLICT
    if (
        not snapshot.blocking_discussions_resolved
        or snapshot.detailed_merge_status != "mergeable"
        or snapshot.head_pipeline_status != "success"
    ):
        return SourceControlReason.MR_CHECKS_BLOCKED
    return None


def _audit(
    repository: SourceControlFormalRepository,
    *,
    action: str,
    target_type: str,
    target_id: str,
    dependencies: SourceControlDependencies,
    result: str = "SUCCESS",
    reason: str | None = None,
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
            result=result,
            reason=reason,
            correlation_id=f"source-control:formal:{target_id}",
        ),
    )


def _kind_from_topic(topic: str) -> FormalDeliveryRequestKind:
    if topic == _CREATE_TOPIC:
        return FormalDeliveryRequestKind.CREATE_MR
    if topic == _MERGE_TOPIC:
        return FormalDeliveryRequestKind.MERGE_MR
    raise FormalDeliveryConflict("Formal Delivery request topic is invalid")


def _request_matches(row: Any, envelope: FormalDeliveryRequestEnvelope) -> bool:
    return (
        row["topic"] == envelope.topic
        and row["payload_hash"] == envelope.payload_hash
        and str(row["requirement_id"]) == envelope.requirement_id
        and row["requirement_revision"] == envelope.requirement_revision
        and str(row["work_item_id"]) == envelope.work_item_id
        and row["work_item_revision"] == envelope.work_item_revision
        and str(row["repository_id"]) == envelope.repository_id
        and row["actor_id"] == envelope.actor_id
        and str(row["acceptance_decision_id"]) == envelope.acceptance_decision_id
        and (
            None
            if row["formal_merge_request_binding_id"] is None
            else str(row["formal_merge_request_binding_id"])
        )
        == envelope.formal_merge_request_binding_id
        and (
            None
            if row["formal_review_decision_id"] is None
            else str(row["formal_review_decision_id"])
        )
        == envelope.formal_review_decision_id
        and row["requested_head_sha"] == envelope.requested_head_sha
        and _kind_from_topic(row["topic"]) is envelope.kind
    )


def accept_formal_delivery_request(
    repository: SourceControlFormalRepository,
    envelope: FormalDeliveryRequestEnvelope,
    *,
    dependencies: SourceControlDependencies,
) -> bool:
    existing = repository.formal_request(envelope.message_id, for_update=True)
    if existing is not None:
        if not _request_matches(existing, envelope):
            raise FormalDeliveryConflict("Formal Delivery message replay conflicts")
        return True
    inserted = repository.insert_formal_request(
        message_id=envelope.message_id,
        topic=envelope.topic,
        payload_hash=envelope.payload_hash,
        requirement_id=envelope.requirement_id,
        requirement_revision=envelope.requirement_revision,
        work_item_id=envelope.work_item_id,
        work_item_revision=envelope.work_item_revision,
        repository_id=envelope.repository_id,
        actor_id=envelope.actor_id,
        acceptance_decision_id=envelope.acceptance_decision_id,
        formal_merge_request_binding_id=envelope.formal_merge_request_binding_id,
        formal_review_decision_id=envelope.formal_review_decision_id,
        requested_head_sha=envelope.requested_head_sha,
        now=dependencies.clock.now(),
    )
    if inserted is None:
        existing = repository.formal_request(envelope.message_id, for_update=True)
        if existing is None or not _request_matches(existing, envelope):
            raise FormalDeliveryConflict("Formal Delivery message replay conflicts")
        return True
    _audit(
        repository,
        action="source_control.formal_delivery.request_accepted",
        target_type="formal_delivery_request_inbox",
        target_id=envelope.message_id,
        dependencies=dependencies,
        reason=f"kind={envelope.kind.value}; headSha={envelope.requested_head_sha}",
    )
    return True


def _formal_repository(
    dependencies: SourceControlDependencies,
) -> SourceControlFormalRepositoryFactory:
    factory = dependencies.formal_repository_factory
    if factory is None:
        raise SourceControlDependencyUnavailable("Formal Delivery repository unavailable")
    return factory


def _claim_request(
    message_id: str, dependencies: SourceControlDependencies
) -> tuple[Any, Any | None]:
    factory = _formal_repository(dependencies)
    with dependencies.engine.begin() as db:
        repository = factory(db)
        now = dependencies.clock.now()
        claimed = repository.claim_formal_request(
            message_id,
            now=now,
            lease_until=now + timedelta(minutes=2),
        )
        request = claimed or repository.formal_request(message_id)
    if request is None:
        raise FormalDeliveryConflict("Formal Delivery request is unavailable")
    return request, claimed


def _read_admission(
    request: Any,
    *,
    dependencies: SourceControlDependencies,
) -> tuple[FormalDeliveryAdmission, GitLabRepositoryProfile, Any]:
    requirement = dependencies.requirement_formal_delivery
    factory = _formal_repository(dependencies)
    if requirement is None:
        raise SourceControlDependencyUnavailable("Requirement Formal Delivery unavailable")
    admission = requirement.delivery_admission(str(request["work_item_id"]))
    kind = _kind_from_topic(request["topic"])
    # Requirement revision is the HTTP command CAS, not a durable delivery
    # coordinate: sibling requests/callbacks advance it without changing this
    # accepted delivery. The owner revalidates Acceptance/Selection currentness;
    # keep this WorkItem and its immutable delivery coordinates exact below.
    if (
        admission.requirement_id != str(request["requirement_id"])
        or admission.work_item_id != str(request["work_item_id"])
        or admission.work_item_revision != request["work_item_revision"]
        or admission.repository_id != str(request["repository_id"])
        or (
            kind is FormalDeliveryRequestKind.CREATE_MR
            and admission.human_owner_id != request["actor_id"]
        )
        or admission.acceptance_decision_id != str(request["acceptance_decision_id"])
        or admission.requested_head_sha != request["requested_head_sha"]
        or (
            kind is FormalDeliveryRequestKind.CREATE_MR
            and (
                admission.formal_merge_request_binding_id
                != (
                    None
                    if request["formal_merge_request_binding_id"] is None
                    else str(request["formal_merge_request_binding_id"])
                )
                or admission.formal_review_decision_id is not None
            )
        )
        or (
            kind is FormalDeliveryRequestKind.MERGE_MR
            and (
                admission.formal_merge_request_binding_id
                != str(request["formal_merge_request_binding_id"])
                or admission.formal_review_decision_id != str(request["formal_review_decision_id"])
            )
        )
    ):
        raise FormalDeliveryConflict("Requirement Formal Delivery admission is stale")
    with dependencies.engine.connect() as db:
        repository = factory(db)
        repository_row = repository.repository_by_id(admission.repository_id)
        branch_row = repository.branch_binding_by_work_item(admission.work_item_id)
    if (
        repository_row is None
        or repository_row["status"] != "AUTHORIZED"
        or str(repository_row["workspace_id"]) != admission.workspace_id
        or repository_row["default_branch"] != "main"
    ):
        raise FormalDeliveryConflict("Repository is not authorized for Formal Delivery")
    if (
        branch_row is None
        or str(branch_row["requirement_id"]) != admission.requirement_id
        or str(branch_row["workspace_id"]) != admission.workspace_id
        or str(branch_row["repository_id"]) != admission.repository_id
        or branch_row["branch_name"] != admission.task_branch
    ):
        raise FormalDeliveryConflict("Task branch binding is stale")
    return admission, repository_profile(repository_row), branch_row


def _formal_actor_block_reason(
    admission: FormalDeliveryAdmission,
    *,
    actor_id: str,
    merging: bool,
    dependencies: SourceControlDependencies,
) -> SourceControlReason | None:
    denied = (
        SourceControlReason.MERGE_ACTOR_INELIGIBLE
        if merging
        else SourceControlReason.OWNER_INELIGIBLE
    )
    requirement = dependencies.requirement
    eligibility = dependencies.eligibility
    if requirement is None or eligibility is None:
        raise SourceControlDependencyUnavailable("Formal merge actor eligibility unavailable")
    try:
        context = requirement.binding_context(admission.work_item_id)
    except Exception as error:
        raise SourceControlDependencyUnavailable(
            "Formal merge actor context unavailable"
        ) from error
    if (
        context.requirement_id != admission.requirement_id
        or context.workspace_id != admission.workspace_id
        or context.work_item_id != admission.work_item_id
        or context.work_item_revision != admission.work_item_revision
        or context.repository_id != admission.repository_id
    ):
        return denied
    if context.assignment_state != "ASSIGNED" or context.human_owner_id != admission.human_owner_id:
        return denied
    try:
        owner = eligibility.evaluate(
            actor_eligibility_context(
                context,
                actor_id=admission.human_owner_id,
                required_capabilities=context.required_capabilities,
            )
        )
        operator = eligibility.evaluate(
            actor_eligibility_context(
                context,
                actor_id=actor_id,
                required_capabilities=(_FORMAL_MERGE_CAPABILITY,)
                if merging
                else (_FORMAL_REQUEST_CAPABILITY,),
            )
        )
    except Exception as error:
        raise SourceControlDependencyUnavailable(
            "Formal merge actor eligibility unavailable"
        ) from error
    if not owner.eligible or not operator.eligible:
        return denied
    return None


def _formal_merge_actor_block_reason(
    admission: FormalDeliveryAdmission, *, actor_id: str, dependencies: SourceControlDependencies
) -> SourceControlReason | None:
    return _formal_actor_block_reason(
        admission, actor_id=actor_id, merging=True, dependencies=dependencies
    )


def _validate_project_and_head(
    admission: FormalDeliveryAdmission,
    profile: GitLabRepositoryProfile,
    *,
    dependencies: SourceControlDependencies,
) -> GitLabFormalMergeRequestPort:
    gitlab = _validate_project(profile, dependencies=dependencies)
    try:
        source = gitlab.get_branch(profile, admission.task_branch)
    except GitLabBranchNotFound as error:
        raise _FormalPreflightBlocked(
            SourceControlReason.SOURCE_BRANCH_MISSING_AFTER_INTEGRATION
        ) from error
    except (GitLabAccessDenied, GitLabProjectNotFound) as error:
        raise _FormalPreflightBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from error
    except (GitLabProviderUnavailable, GitLabResultUnknown) as error:
        raise SourceControlDependencyUnavailable(
            "Formal Delivery provider preflight is unavailable"
        ) from error
    if source.name != admission.task_branch or source.commit_sha != admission.requested_head_sha:
        raise _FormalPreflightBlocked(SourceControlReason.HEAD_SHA_CHANGED)
    return gitlab


def _validate_project(
    profile: GitLabRepositoryProfile,
    *,
    dependencies: SourceControlDependencies,
) -> GitLabFormalMergeRequestPort:
    gitlab = dependencies.gitlab_formal_merge_requests
    if gitlab is None:
        raise SourceControlDependencyUnavailable("GitLab Formal Delivery unavailable")
    try:
        project = gitlab.get_project_delivery_profile(profile)
    except GitLabBranchNotFound as error:
        raise _FormalPreflightBlocked(SourceControlReason.TARGET_BRANCH_NOT_FOUND) from error
    except GitLabTargetBranchNotProtected as error:
        raise _FormalPreflightBlocked(SourceControlReason.TARGET_BRANCH_NOT_PROTECTED) from error
    except GitLabProjectPolicyUnsupported as error:
        raise _FormalPreflightBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED) from error
    except (GitLabAccessDenied, GitLabProjectNotFound) as error:
        raise _FormalPreflightBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from error
    except (GitLabProviderUnavailable, GitLabResultUnknown) as error:
        raise SourceControlDependencyUnavailable(
            "Formal Delivery provider preflight is unavailable"
        ) from error
    if (
        project.project_id != profile.project_id
        or project.project_path != profile.project_path
        or project.default_branch != "main"
        or project.merge_method != "merge"
    ):
        raise _FormalPreflightBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED)
    return gitlab


def _payload_for(
    request: Any,
    branch_row: Any,
) -> CreateFormalMergeRequestEffectPayload | MergeFormalMergeRequestEffectPayload:
    kind = _kind_from_topic(request["topic"])
    if kind is FormalDeliveryRequestKind.CREATE_MR:
        return CreateFormalMergeRequestEffectPayload(
            acceptanceDecisionId=str(request["acceptance_decision_id"]),
            branchBindingId=str(branch_row["id"]),
            headSha=request["requested_head_sha"],
        )
    return MergeFormalMergeRequestEffectPayload(
        acceptanceDecisionId=str(request["acceptance_decision_id"]),
        bindingId=str(request["formal_merge_request_binding_id"]),
        requestedHeadSha=request["requested_head_sha"],
        reviewDecisionId=str(request["formal_review_decision_id"]),
    )


def _operation_subject(request: Any) -> tuple[EffectOperation, str]:
    if _kind_from_topic(request["topic"]) is FormalDeliveryRequestKind.CREATE_MR:
        return (
            _CREATE,
            (
                f"formal-work-item:{request['work_item_id']}:{request['requested_head_sha']}:"
                f"{request['payload_hash']}"
            ),
        )
    return (
        _MERGE,
        (
            f"formal-mr:{request['formal_merge_request_binding_id']}:"
            f"{request['requested_head_sha']}:{request['payload_hash']}"
        ),
    )


def _effect_matches(
    effect: SourceControlEffectDto,
    *,
    operation: EffectOperation,
    subject: str,
    request: Any,
    payload: object,
) -> bool:
    return (
        effect.operation is operation
        and effect.subject_key == subject
        and effect.work_item_id == str(request["work_item_id"])
        and effect.requirement_id == str(request["requirement_id"])
        and effect.repository_id == str(request["repository_id"])
        and effect.request_fingerprint == request["payload_hash"]
        and effect.payload == payload
    )


def _acquire_effect(
    request: Any,
    payload: object,
    *,
    dependencies: SourceControlDependencies,
) -> tuple[SourceControlEffectDto, bool]:
    factory = _formal_repository(dependencies)
    operation, subject = _operation_subject(request)
    try:
        with dependencies.engine.begin() as db:
            repository = factory(db)
            row = repository.effect_by_operation_subject(
                operation.value,
                subject,
                for_update=True,
            )
            if row is None:
                row = repository.insert_effect(
                    id=str(dependencies.random.uuid4()),
                    effect_key=f"source-control:{operation.value.lower()}:{subject}",
                    operation=operation.value,
                    subject_key=subject,
                    payload=payload,
                    work_item_id=str(request["work_item_id"]),
                    requirement_id=str(request["requirement_id"]),
                    repository_id=str(request["repository_id"]),
                    request_fingerprint=request["payload_hash"],
                    attempts=0,
                    next_reconcile_at=None,
                    state=EffectState.PLANNED.value,
                    requirement_callback_state=RequirementCallbackState.PENDING.value,
                    now=dependencies.clock.now(),
                )
                _audit(
                    repository,
                    action="source_control.formal_delivery.planned",
                    target_type="source_control_effect",
                    target_id=str(row["id"]),
                    dependencies=dependencies,
                    reason=f"operation={operation.value}",
                )
    except IntegrityError:
        with dependencies.engine.connect() as db:
            row = factory(db).effect_by_operation_subject(operation.value, subject)
        if row is None:
            raise
    try:
        effect = effect_dto(row)
    except (TypeError, ValueError) as error:
        raise FormalDeliveryConflict("Formal Delivery Effect is invalid") from error
    if not _effect_matches(
        effect,
        operation=operation,
        subject=subject,
        request=request,
        payload=payload,
    ):
        raise FormalDeliveryConflict("Formal Delivery Effect conflicts")
    if effect.state is not EffectState.PLANNED:
        return effect, False
    with dependencies.engine.begin() as db:
        repository = factory(db)
        row = repository.transition_effect(
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
        if row is None:
            raise RequirementCallbackUnavailable("Formal Delivery Effect lease was lost")
    return effect_dto(row), True


def _routing(
    admission: FormalDeliveryAdmission,
    *,
    dependencies: SourceControlDependencies,
) -> FormalReviewRoutingSnapshot:
    resolver = dependencies.formal_review_routing
    if resolver is None:
        raise SourceControlDependencyUnavailable("Formal Review routing unavailable")
    try:
        routing = resolver.resolve(
            workspace_id=admission.workspace_id,
            repository_id=admission.repository_id,
            work_item_id=admission.work_item_id,
            human_owner_id=admission.human_owner_id,
        )
    except Exception as error:
        raise SourceControlDependencyUnavailable("Formal Review routing failed closed") from error
    if (
        not routing.default_reviewer_id.strip()
        or not routing.policy_code.strip()
        or not routing.policy_snapshot_hash.startswith("sha256:")
        or len(routing.policy_snapshot_hash) != 71
    ):
        raise SourceControlDependencyUnavailable("Formal Review routing is invalid")
    return routing


def _assignment_dto(row: Any) -> FormalReviewAssignmentDto:
    return FormalReviewAssignmentDto(
        id=str(row["id"]),
        binding_id=str(row["binding_id"]),
        acceptance_decision_id=str(row["acceptance_decision_id"]),
        requirement_id=str(row["requirement_id"]),
        work_item_id=str(row["work_item_id"]),
        subject_head_sha=row["subject_head_sha"],
        default_reviewer_id=row["default_reviewer_id"],
        current_reviewer_id=row["current_reviewer_id"],
        policy_code=row["policy_code"],
        policy_version=row["policy_version"],
        policy_snapshot_hash=row["policy_snapshot_hash"],
        resolution_snapshot=dict(row["resolution_snapshot"]),
        revision=row["revision"],
        assigned_at=row["assigned_at"],
        superseded_at=row["superseded_at"],
    )


def _effect_acceptance_decision_id(effect: SourceControlEffectDto) -> str:
    payload = effect.payload
    if isinstance(
        payload,
        (CreateFormalMergeRequestEffectPayload, MergeFormalMergeRequestEffectPayload),
    ):
        return payload.acceptance_decision_id
    raise FormalDeliveryConflict("Formal Delivery Effect payload is invalid")


def _assignment_for_effect(
    repository: SourceControlFormalRepository,
    effect: SourceControlEffectDto,
    binding_id: str,
) -> Any:
    return repository.formal_review_assignment_by_acceptance(
        binding_id,
        _effect_acceptance_decision_id(effect),
    )


def _assignment_matches_cycle(
    assignment: Any,
    admission: FormalDeliveryAdmission,
    snapshot: GitLabMergeRequestSnapshot,
    routing: FormalReviewRoutingSnapshot,
) -> bool:
    return (
        str(assignment["acceptance_decision_id"]) == admission.acceptance_decision_id
        and assignment["subject_head_sha"] == snapshot.head_sha
        and assignment["default_reviewer_id"] == routing.default_reviewer_id
        and assignment["current_reviewer_id"] == routing.default_reviewer_id
        and assignment["policy_code"] == routing.policy_code
        and assignment["policy_version"] == routing.policy_version
        and assignment["policy_snapshot_hash"] == routing.policy_snapshot_hash
        and dict(assignment["resolution_snapshot"]) == routing.resolution_snapshot
    )


def _record_effect_callback(
    effect: SourceControlEffectDto,
    *,
    deliver: Callable[[], None],
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    if effect.callback_state is RequirementCallbackState.ACKED:
        return effect
    factory = _formal_repository(dependencies)
    with dependencies.engine.begin() as db:
        repository = factory(db)
        current = repository.effect_by_id(effect.id, for_update=True)
        if current is None:
            raise RequirementCallbackUnavailable("Formal Delivery callback lease was lost")
        locked = effect_dto(current)
        if (
            locked.id != effect.id
            or locked.state is not effect.state
            or locked.attempts != effect.attempts
        ):
            raise RequirementCallbackUnavailable("Formal Delivery callback lease was lost")
        if locked.callback_state is RequirementCallbackState.ACKED:
            return locked
        try:
            deliver()
        except Exception:
            state = RequirementCallbackState.FAILED
        else:
            state = RequirementCallbackState.ACKED
        row = repository.transition_effect(
            locked.id,
            expected_state=locked.state.value,
            expected_attempts=locked.attempts,
            values={
                "requirement_callback_state": state.value,
                "updated_at": dependencies.clock.now(),
            },
        )
        if row is not None:
            _audit(
                repository,
                action="source_control.formal_delivery.callback_"
                + ("acked" if state is RequirementCallbackState.ACKED else "failed"),
                target_type="source_control_effect",
                target_id=effect.id,
                dependencies=dependencies,
                result=("SUCCESS" if state is RequirementCallbackState.ACKED else "FAILURE"),
            )
    if row is None:
        raise RequirementCallbackUnavailable("Formal Delivery callback lease was lost")
    return effect_dto(row)


def _accepted_callback_revision(
    effect: SourceControlEffectDto, dependencies: SourceControlDependencies
) -> int:
    with dependencies.engine.connect() as db:
        request = _formal_repository(dependencies)(db).formal_request_for_effect(
            operation=effect.operation.value,
            work_item_id=effect.work_item_id,
            requirement_id=effect.requirement_id,
            repository_id=effect.repository_id,
            request_fingerprint=effect.request_fingerprint,
        )
    if request is None:
        raise SourceControlDependencyUnavailable("Formal callback request is unavailable")
    return int(request["work_item_revision"])


def _callback_pending(
    effect: SourceControlEffectDto,
    request: Any,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    def deliver() -> None:
        requirement = dependencies.requirement_formal_delivery
        if requirement is None:
            raise SourceControlDependencyUnavailable(
                "Requirement Formal pending callback unavailable"
            )
        binding = request["formal_merge_request_binding_id"]
        requirement.record_reconciliation_pending(
            FormalReconciliationPendingCallback(
                work_item_id=effect.work_item_id,
                binding_id=None if binding is None else str(binding),
                expected_revision=request["work_item_revision"],
                correlation_id=f"source-control:effect:{effect.id}",
                idempotency_key=f"source-control:formal-pending:{effect.id}",
            )
        )

    return _record_effect_callback(effect, deliver=deliver, dependencies=dependencies)


def _callback_ready(
    effect: SourceControlEffectDto,
    binding: Any,
    assignment: Any,
    *,
    expected_revision: int,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    if effect.callback_state is RequirementCallbackState.ACKED:
        return effect
    routing = _assignment_dto(assignment)

    def deliver() -> None:
        requirement = dependencies.requirement_formal_delivery
        if requirement is None:
            raise SourceControlDependencyUnavailable("Requirement Formal Delivery unavailable")
        requirement.record_mr_ready(
            FormalMrReadyCallback(
                work_item_id=effect.work_item_id,
                binding_id=str(binding["id"]),
                head_sha=routing.subject_head_sha,
                expected_revision=expected_revision,
                assignment=FormalReviewRoutingSnapshot(
                    default_reviewer_id=routing.default_reviewer_id,
                    policy_code=routing.policy_code,
                    policy_version=routing.policy_version,
                    policy_snapshot_hash=routing.policy_snapshot_hash,
                    resolution_snapshot=routing.resolution_snapshot,
                ),
                correlation_id=f"source-control:effect:{effect.id}",
                idempotency_key=f"source-control:formal-ready:{effect.id}",
            )
        )

    return _record_effect_callback(
        effect,
        deliver=deliver,
        dependencies=dependencies,
    )


def _callback_merged(
    effect: SourceControlEffectDto,
    binding: Any,
    observation: Any,
    *,
    expected_revision: int,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    if effect.callback_state is RequirementCallbackState.ACKED:
        return effect
    if observation["merge_commit_sha"] is None:
        raise SourceControlDependencyUnavailable("Requirement Formal merge callback unavailable")

    def deliver() -> None:
        requirement = dependencies.requirement_formal_delivery
        if requirement is None:
            raise SourceControlDependencyUnavailable(
                "Requirement Formal merge callback unavailable"
            )
        requirement.record_merged(
            FormalMergedCallback(
                work_item_id=effect.work_item_id,
                binding_id=str(binding["id"]),
                head_sha=observation["head_sha"],
                merge_commit_sha=observation["merge_commit_sha"],
                expected_revision=expected_revision,
                correlation_id=f"source-control:effect:{effect.id}",
                idempotency_key=f"source-control:formal-merged:{effect.id}",
            )
        )

    return _record_effect_callback(
        effect,
        deliver=deliver,
        dependencies=dependencies,
    )


def _callback_blocked(
    effect: SourceControlEffectDto,
    request: Any,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    if effect.callback_state is RequirementCallbackState.ACKED:
        return effect
    if effect.last_error_code is None:
        raise SourceControlDependencyUnavailable("Formal blocked callback reason is unavailable")
    try:
        reason_code = SourceControlReason(effect.last_error_code)
    except (TypeError, ValueError):
        raise SourceControlDependencyUnavailable(
            "Formal blocked callback reason is unavailable"
        ) from None
    binding_id = (
        None
        if request["formal_merge_request_binding_id"] is None
        else str(request["formal_merge_request_binding_id"])
    )

    def deliver() -> None:
        requirement = dependencies.requirement_formal_delivery
        if requirement is None:
            raise SourceControlDependencyUnavailable(
                "Requirement Formal blocked callback unavailable"
            )
        requirement.record_blocked(
            FormalDeliveryBlockedCallback(
                work_item_id=effect.work_item_id,
                binding_id=binding_id,
                reason_code=reason_code,
                expected_revision=request["work_item_revision"],
                correlation_id=f"source-control:effect:{effect.id}",
                idempotency_key=f"source-control:formal-blocked:{effect.id}",
            )
        )

    return _record_effect_callback(
        effect,
        deliver=deliver,
        dependencies=dependencies,
    )


def replay_pending_formal_callbacks(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
    excluded_effect_ids: frozenset[str] = frozenset(),
) -> tuple[SourceControlEffectDto, ...]:
    if limit < 1:
        raise ValueError("Formal callback replay limit must be positive")
    factory = _formal_repository(dependencies)
    with dependencies.engine.connect() as db:
        repository = factory(db)
        rows = repository.pending_callback_effects(
            limit=limit + len(excluded_effect_ids),
        )
    replayed: list[SourceControlEffectDto] = []
    for row in rows:
        effect = effect_dto(row)
        if effect.id in excluded_effect_ids:
            continue
        with dependencies.engine.connect() as db:
            repository = factory(db)
            request = repository.formal_request_for_effect(
                operation=effect.operation.value,
                work_item_id=effect.work_item_id,
                requirement_id=effect.requirement_id,
                repository_id=effect.repository_id,
                request_fingerprint=effect.request_fingerprint,
            )
            if effect.operation is _CREATE:
                binding = repository.formal_binding_by_work_item(effect.work_item_id)
            else:
                payload = effect.payload
                binding = (
                    None
                    if not isinstance(payload, MergeFormalMergeRequestEffectPayload)
                    else repository.merge_request_binding_by_id(payload.binding_id)
                )
            assignment = (
                None
                if binding is None
                else _assignment_for_effect(repository, effect, str(binding["id"]))
            )
            observation = (
                None
                if binding is None
                else repository.latest_merge_request_observation(str(binding["id"]))
            )
        if request is not None and effect.state is EffectState.UNKNOWN:
            replayed.append(_callback_pending(effect, request, dependencies=dependencies))
        elif request is not None and effect.state is EffectState.BLOCKED:
            replayed.append(
                _callback_blocked(
                    effect,
                    request,
                    dependencies=dependencies,
                )
            )
        elif request is None or binding is None:

            def unavailable() -> None:
                raise SourceControlDependencyUnavailable(
                    "Formal callback replay context unavailable"
                )

            replayed.append(
                _record_effect_callback(
                    effect,
                    deliver=unavailable,
                    dependencies=dependencies,
                )
            )
        elif effect.operation is _CREATE and assignment is not None:
            replayed.append(
                _callback_ready(
                    effect,
                    binding,
                    assignment,
                    expected_revision=request["work_item_revision"],
                    dependencies=dependencies,
                )
            )
        elif effect.operation is _MERGE and observation is not None:
            replayed.append(
                _callback_merged(
                    effect,
                    binding,
                    observation,
                    expected_revision=request["work_item_revision"],
                    dependencies=dependencies,
                )
            )
        else:

            def incomplete() -> None:
                raise SourceControlDependencyUnavailable("Formal callback replay facts unavailable")

            replayed.append(
                _record_effect_callback(
                    effect,
                    deliver=incomplete,
                    dependencies=dependencies,
                )
            )
        if len(replayed) >= limit:
            break
    return tuple(replayed)


def _complete_request(
    repository: SourceControlFormalRepository,
    *,
    message_id: str | None,
    expected_attempts: int | None,
    dependencies: SourceControlDependencies,
) -> None:
    if message_id is None:
        return
    if (
        expected_attempts is None
        or repository.complete_formal_request(
            message_id,
            expected_attempts=expected_attempts,
            now=dependencies.clock.now(),
        )
        is None
    ):
        raise RequirementCallbackUnavailable("Formal Delivery inbox lease was lost")


def _complete_effect_block(
    request: Any,
    effect: SourceControlEffectDto,
    *,
    reason_code: SourceControlReason,
    expected_state: EffectState,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    factory = _formal_repository(dependencies)
    now = dependencies.clock.now()
    binding_id = (
        None
        if request["formal_merge_request_binding_id"] is None
        else str(request["formal_merge_request_binding_id"])
    )
    with dependencies.engine.begin() as db:
        repository = factory(db)
        final = repository.transition_effect(
            effect.id,
            expected_state=expected_state.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.BLOCKED.value,
                "requirement_callback_state": RequirementCallbackState.PENDING.value,
                "last_error_code": reason_code.value,
                "next_reconcile_at": None,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if final is None:
            raise RequirementCallbackUnavailable("Formal Delivery Effect lease was lost")
        if request["state"] == "PROCESSING":
            completed = repository.complete_formal_request_blocked(
                str(request["message_id"]),
                expected_attempts=request["attempts"],
                reason_code=reason_code.value,
                now=now,
            )
            if completed is None:
                raise RequirementCallbackUnavailable("Formal Delivery inbox lease was lost")
        binding = None if binding_id is None else repository.merge_request_binding_by_id(binding_id)
        observation = (
            None
            if binding is None
            else repository.latest_merge_request_observation(str(binding["id"]))
        )
        assignment = (
            None
            if binding is None
            else _assignment_for_effect(repository, effect, str(binding["id"]))
        )
        _audit(
            repository,
            action="source_control.formal_delivery.effect_blocked",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
            result="FAILURE",
            reason=reason_code.value,
        )
    final_effect = _callback_blocked(
        effect_dto(final),
        request,
        dependencies=dependencies,
    )
    return ProcessFormalDeliveryResult(
        effect=final_effect,
        binding=None if binding is None else binding_dto(binding),
        observation=None if observation is None else observation_dto(observation),
        assignment=None if assignment is None else _assignment_dto(assignment),
        blocked_reason=reason_code.value,
    )


def _commit_create(
    admission: FormalDeliveryAdmission,
    effect: SourceControlEffectDto,
    snapshot: GitLabMergeRequestSnapshot,
    routing: FormalReviewRoutingSnapshot,
    *,
    branch_row: Any,
    creation_origin: MergeRequestCreationOrigin,
    message_id: str | None,
    inbox_attempts: int | None,
    expected_state: EffectState,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    factory = _formal_repository(dependencies)
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = factory(db)
        binding = repository.formal_binding_by_work_item(admission.work_item_id)
        created_binding = binding is None
        if binding is None:
            if admission.formal_merge_request_binding_id is not None:
                raise FormalDeliveryConflict("Formal MR Binding is unavailable")
            binding = repository.insert_merge_request_binding(
                id=str(dependencies.random.uuid4()),
                kind=MergeRequestKind.FORMAL.value,
                work_item_id=admission.work_item_id,
                requirement_id=admission.requirement_id,
                workspace_id=admission.workspace_id,
                repository_id=admission.repository_id,
                branch_binding_id=str(branch_row["id"]),
                external_project_id=snapshot.project_id,
                merge_request_iid=snapshot.iid,
                source_branch=snapshot.source_branch,
                target_branch="main",
                create_effect_id=effect.id,
                head_sha=snapshot.head_sha,
                creation_origin=creation_origin.value,
                now=now,
            )
        if (
            binding["kind"] != MergeRequestKind.FORMAL.value
            or str(binding["work_item_id"]) != admission.work_item_id
            or str(binding["requirement_id"]) != admission.requirement_id
            or str(binding["workspace_id"]) != admission.workspace_id
            or str(binding["repository_id"]) != admission.repository_id
            or str(binding["branch_binding_id"]) != str(branch_row["id"])
            or binding["external_project_id"] != snapshot.project_id
            or binding["source_branch"] != admission.task_branch
            or binding["target_branch"] != "main"
            or binding["merge_request_iid"] != snapshot.iid
            or (
                created_binding
                and (
                    str(binding["create_effect_id"]) != effect.id
                    or binding["head_sha"] != admission.requested_head_sha
                )
            )
            or (
                not created_binding
                and admission.formal_merge_request_binding_id != str(binding["id"])
            )
        ):
            raise FormalDeliveryConflict("Formal MR Binding conflicts")
        observation = repository.append_merge_request_observation(
            id=str(dependencies.random.uuid4()),
            binding_id=str(binding["id"]),
            head_sha=snapshot.head_sha,
            state=snapshot_state(snapshot).value,
            merge_commit_sha=snapshot.merge_commit_sha,
            external_merge_user_id=snapshot.merge_user_id,
            merged_at=snapshot.merged_at,
            observation_digest=observation_digest(snapshot),
            observed_at=now,
        )
        if observation is None:
            observation = repository.latest_merge_request_observation(str(binding["id"]))
        assignment = repository.current_formal_review_assignment(
            str(binding["id"]),
            for_update=True,
        )
        assignment_revision = 1
        if (
            assignment is not None
            and str(assignment["acceptance_decision_id"]) == admission.acceptance_decision_id
            and not _assignment_matches_cycle(assignment, admission, snapshot, routing)
        ):
            raise FormalDeliveryConflict("Formal Review acceptance cycle conflicts")
        if assignment is not None and not _assignment_matches_cycle(
            assignment,
            admission,
            snapshot,
            routing,
        ):
            superseded = repository.supersede_formal_review_assignment(
                str(assignment["id"]),
                expected_revision=assignment["revision"],
                now=now,
            )
            if superseded is None:
                raise RequirementCallbackUnavailable("Formal Review assignment lease was lost")
            assignment_revision = assignment["revision"] + 1
            assignment = None
        if assignment is None:
            assignment = repository.insert_formal_review_assignment(
                id=str(dependencies.random.uuid4()),
                binding_id=str(binding["id"]),
                acceptance_decision_id=admission.acceptance_decision_id,
                requirement_id=admission.requirement_id,
                work_item_id=admission.work_item_id,
                subject_head_sha=snapshot.head_sha,
                default_reviewer_id=routing.default_reviewer_id,
                current_reviewer_id=routing.default_reviewer_id,
                policy_code=routing.policy_code,
                policy_version=routing.policy_version,
                policy_snapshot_hash=routing.policy_snapshot_hash,
                resolution_snapshot=routing.resolution_snapshot,
                revision=assignment_revision,
                now=now,
            )
        final = repository.transition_effect(
            effect.id,
            expected_state=expected_state.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.SUCCEEDED.value,
                "requirement_callback_state": RequirementCallbackState.PENDING.value,
                "last_error_code": None,
                "next_reconcile_at": None,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if final is None or observation is None:
            raise RequirementCallbackUnavailable("Formal MR fact commit was lost")
        _complete_request(
            repository,
            message_id=message_id,
            expected_attempts=inbox_attempts,
            dependencies=dependencies,
        )
        _audit(
            repository,
            action="source_control.formal_mr.succeeded",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
            reason=f"bindingId={binding['id']}; headSha={snapshot.head_sha}",
        )
    final_effect = _callback_ready(
        effect_dto(final),
        binding,
        assignment,
        expected_revision=_accepted_callback_revision(effect, dependencies),
        dependencies=dependencies,
    )
    return ProcessFormalDeliveryResult(
        effect=final_effect,
        binding=binding_dto(binding),
        observation=observation_dto(observation),
        assignment=_assignment_dto(assignment),
    )


def _commit_merge(
    admission: FormalDeliveryAdmission,
    effect: SourceControlEffectDto,
    binding: Any,
    snapshot: GitLabMergeRequestSnapshot,
    *,
    message_id: str | None,
    inbox_attempts: int | None,
    expected_state: EffectState,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    factory = _formal_repository(dependencies)
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = factory(db)
        observation = repository.append_merge_request_observation(
            id=str(dependencies.random.uuid4()),
            binding_id=str(binding["id"]),
            head_sha=snapshot.head_sha,
            state=snapshot_state(snapshot).value,
            merge_commit_sha=snapshot.merge_commit_sha,
            external_merge_user_id=snapshot.merge_user_id,
            merged_at=snapshot.merged_at,
            observation_digest=observation_digest(snapshot),
            observed_at=now,
        )
        if observation is None:
            observation = repository.latest_merge_request_observation(str(binding["id"]))
        final = repository.transition_effect(
            effect.id,
            expected_state=expected_state.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.SUCCEEDED.value,
                "requirement_callback_state": RequirementCallbackState.PENDING.value,
                "last_error_code": None,
                "next_reconcile_at": None,
                "completed_at": now,
                "updated_at": now,
            },
        )
        if final is None or observation is None:
            raise RequirementCallbackUnavailable("Formal merge fact commit was lost")
        _complete_request(
            repository,
            message_id=message_id,
            expected_attempts=inbox_attempts,
            dependencies=dependencies,
        )
        _audit(
            repository,
            action="source_control.formal_merge.succeeded",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
            reason=f"bindingId={binding['id']}; mergeCommitSha={snapshot.merge_commit_sha}",
        )
    final_effect = _callback_merged(
        effect_dto(final),
        binding,
        observation,
        expected_revision=_accepted_callback_revision(effect, dependencies),
        dependencies=dependencies,
    )
    return ProcessFormalDeliveryResult(
        effect=final_effect,
        binding=binding_dto(binding),
        observation=observation_dto(observation),
    )


def _mark_unknown(
    effect: SourceControlEffectDto,
    request: Any,
    *,
    binding: Any | None,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    factory = _formal_repository(dependencies)
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.IN_FLIGHT.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.UNKNOWN.value,
                "last_error_code": "EXTERNAL_RESULT_UNKNOWN",
                "next_reconcile_at": now + timedelta(minutes=2),
                "updated_at": now,
            },
        )
        if row is None:
            raise RequirementCallbackUnavailable("Formal Delivery Effect lease was lost")
        _complete_request(
            repository,
            message_id=str(request["message_id"]),
            expected_attempts=request["attempts"],
            dependencies=dependencies,
        )
        _audit(
            repository,
            action="source_control.formal_delivery.unknown",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
            result="UNKNOWN",
            reason="EXTERNAL_RESULT_UNKNOWN",
        )
        observation = (
            None
            if binding is None
            else repository.latest_merge_request_observation(str(binding["id"]))
        )
    pending = _callback_pending(effect_dto(row), request, dependencies=dependencies)
    return ProcessFormalDeliveryResult(
        effect=pending,
        binding=None if binding is None else binding_dto(binding),
        observation=None if observation is None else observation_dto(observation),
    )


def _provider_create(
    admission: FormalDeliveryAdmission,
    profile: GitLabRepositoryProfile,
    effect: SourceControlEffectDto,
    *,
    actor_id: str,
    dependencies: SourceControlDependencies,
) -> tuple[GitLabMergeRequestSnapshot, MergeRequestCreationOrigin]:
    gitlab = _validate_project_and_head(admission, profile, dependencies=dependencies)
    try:
        candidates = gitlab.list_merge_requests(
            profile,
            source_branch=admission.task_branch,
            target_branch="main",
            state="all",
        )
        if len(candidates) > 1:
            raise _FormalPreflightBlocked(SourceControlReason.MR_CONFLICT)
        denied = _formal_actor_block_reason(
            admission, actor_id=actor_id, merging=False, dependencies=dependencies
        )
        if denied is not None:
            raise _FormalPreflightBlocked(denied)
        if candidates:
            snapshot = candidates[0]
            origin = MergeRequestCreationOrigin.EXTERNAL_ADOPTED
        else:
            locator = gitlab.create_formal_merge_request(
                profile,
                source_branch=admission.task_branch,
                expected_head_sha=admission.requested_head_sha,
                title=f"formal: deliver {admission.work_item_id}",
                description=(
                    f"Requirement: {admission.requirement_id}\n"
                    f"Work-Item: {admission.work_item_id}\n"
                    f"Source-Control-Effect: {effect.id}"
                ),
            )
            snapshot = gitlab.get_merge_request(profile, iid=locator.iid)
            origin = MergeRequestCreationOrigin.PLATFORM_CREATED
        source = gitlab.get_branch(profile, admission.task_branch)
    except (GitLabProviderUnavailable, GitLabResultUnknown) as error:
        raise GitLabResultUnknown("Formal MR result is unknown") from error
    except _DETERMINISTIC_PROVIDER_ERRORS as error:
        raise _FormalPreflightBlocked(_provider_block_reason(error)) from error
    if snapshot.head_sha != admission.requested_head_sha or (
        source.commit_sha != admission.requested_head_sha
    ):
        raise _FormalPreflightBlocked(SourceControlReason.HEAD_SHA_CHANGED)
    if snapshot.state != "opened":
        raise _FormalPreflightBlocked(SourceControlReason.MR_CLOSED)
    if (
        snapshot.project_id != profile.project_id
        or snapshot.source_branch != admission.task_branch
        or snapshot.target_branch != "main"
    ):
        raise _FormalPreflightBlocked(SourceControlReason.MR_CONFLICT)
    return snapshot, origin


def _provider_merge(
    admission: FormalDeliveryAdmission,
    profile: GitLabRepositoryProfile,
    binding: Any,
    *,
    actor_id: str,
    dependencies: SourceControlDependencies,
) -> GitLabMergeRequestSnapshot:
    gitlab = _validate_project_and_head(admission, profile, dependencies=dependencies)
    try:
        before = gitlab.get_merge_request(profile, iid=binding["merge_request_iid"])
    except (GitLabProviderUnavailable, GitLabResultUnknown) as error:
        raise GitLabResultUnknown("Formal merge preflight is unknown") from error
    except _DETERMINISTIC_PROVIDER_ERRORS as error:
        raise _FormalPreflightBlocked(_provider_block_reason(error)) from error
    if before.head_sha != admission.requested_head_sha:
        raise _FormalPreflightBlocked(SourceControlReason.HEAD_SHA_CHANGED)
    if before.state != "opened":
        raise _FormalPreflightBlocked(SourceControlReason.MR_CLOSED)
    if (
        before.project_id != binding["external_project_id"]
        or before.source_branch != admission.task_branch
        or before.target_branch != "main"
    ):
        raise _FormalPreflightBlocked(SourceControlReason.MR_CONFLICT)
    if before.has_conflicts:
        raise _FormalPreflightBlocked(SourceControlReason.MERGE_CONFLICT)
    if (
        not before.blocking_discussions_resolved
        or before.detailed_merge_status != "mergeable"
        or before.head_pipeline_status != "success"
    ):
        raise _FormalPreflightBlocked(SourceControlReason.MR_CHECKS_BLOCKED)
    denied = _formal_merge_actor_block_reason(
        admission, actor_id=actor_id, dependencies=dependencies
    )
    if denied is not None:
        raise _FormalPreflightBlocked(denied)
    try:
        snapshot = gitlab.merge_formal_merge_request(
            profile,
            iid=binding["merge_request_iid"],
            expected_head_sha=admission.requested_head_sha,
        )
    except (GitLabProviderUnavailable, GitLabResultUnknown) as error:
        raise GitLabResultUnknown("Formal merge result is unknown") from error
    except _DETERMINISTIC_PROVIDER_ERRORS as error:
        raise _FormalPreflightBlocked(_provider_block_reason(error)) from error
    if snapshot.head_sha != admission.requested_head_sha:
        raise _FormalPreflightBlocked(SourceControlReason.HEAD_SHA_CHANGED)
    if (
        snapshot.project_id != binding["external_project_id"]
        or snapshot.iid != binding["merge_request_iid"]
        or snapshot.source_branch != admission.task_branch
        or snapshot.target_branch != "main"
    ):
        raise _FormalPreflightBlocked(SourceControlReason.MR_CONFLICT)
    readback_block = None if snapshot.state == "merged" else _merge_readback_block_reason(snapshot)
    if readback_block is not None:
        raise _FormalPreflightBlocked(readback_block)
    if (
        snapshot.state != "merged"
        or snapshot.merge_commit_sha is None
        or snapshot.merged_at is None
    ):
        raise GitLabResultUnknown("Formal merge readback is inconclusive")
    return snapshot


def _replay(
    request: Any,
    *,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    factory = _formal_repository(dependencies)
    operation, subject = _operation_subject(request)
    with dependencies.engine.connect() as db:
        repository = factory(db)
        row = repository.effect_by_operation_subject(operation.value, subject)
        if row is None:
            try:
                blocked_reason = SourceControlReason(request["last_error_code"])
            except (TypeError, ValueError):
                raise FormalDeliveryConflict(
                    "Processed Formal Delivery Effect is unavailable"
                ) from None
            binding = (
                None
                if request["formal_merge_request_binding_id"] is None
                else repository.merge_request_binding_by_id(
                    str(request["formal_merge_request_binding_id"])
                )
            )
            observation = (
                None
                if binding is None
                else repository.latest_merge_request_observation(str(binding["id"]))
            )
            assignment = (
                None
                if binding is None
                else repository.formal_review_assignment_by_acceptance(
                    str(binding["id"]),
                    str(request["acceptance_decision_id"]),
                )
            )
            return ProcessFormalDeliveryResult(
                effect=None,
                binding=None if binding is None else binding_dto(binding),
                observation=(None if observation is None else observation_dto(observation)),
                assignment=(None if assignment is None else _assignment_dto(assignment)),
                blocked_reason=blocked_reason.value,
            )
        effect = effect_dto(row)
        if effect.state is EffectState.UNKNOWN:
            effect = _callback_pending(effect, request, dependencies=dependencies)
        binding = (
            repository.formal_binding_by_work_item(str(request["work_item_id"]))
            if operation is _CREATE
            else repository.merge_request_binding_by_id(
                str(request["formal_merge_request_binding_id"])
            )
        )
        if binding is None:
            if effect.state is EffectState.BLOCKED:
                effect = _callback_blocked(
                    effect,
                    request,
                    dependencies=dependencies,
                )
            return ProcessFormalDeliveryResult(
                effect=effect,
                binding=None,
                observation=None,
                blocked_reason=(
                    effect.last_error_code if effect.state is EffectState.BLOCKED else None
                ),
            )
        observation = repository.latest_merge_request_observation(str(binding["id"]))
        assignment = _assignment_for_effect(repository, effect, str(binding["id"]))
    if effect.state is EffectState.SUCCEEDED and observation is not None:
        if operation is _CREATE and assignment is not None:
            effect = _callback_ready(
                effect,
                binding,
                assignment,
                expected_revision=request["work_item_revision"],
                dependencies=dependencies,
            )
        elif operation is _MERGE:
            effect = _callback_merged(
                effect,
                binding,
                observation,
                expected_revision=request["work_item_revision"],
                dependencies=dependencies,
            )
    elif effect.state is EffectState.BLOCKED:
        effect = _callback_blocked(
            effect,
            request,
            dependencies=dependencies,
        )
    return ProcessFormalDeliveryResult(
        effect=effect,
        binding=binding_dto(binding),
        observation=None if observation is None else observation_dto(observation),
        assignment=None if assignment is None else _assignment_dto(assignment),
        blocked_reason=(effect.last_error_code if effect.state is EffectState.BLOCKED else None),
    )


def process_formal_delivery_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    request, claimed = _claim_request(message_id, dependencies)
    return _process_formal_delivery_request(
        message_id=message_id,
        dependencies=dependencies,
        request=request,
        claimed=claimed,
    )


def process_formal_delivery_candidate(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    request, claimed = _claim_request(message_id, dependencies)
    if claimed is None:
        raise InboxClaimLost(message_id)
    try:
        return _process_formal_delivery_request(
            message_id=message_id,
            dependencies=dependencies,
            request=request,
            claimed=claimed,
        )
    except Exception as error:
        raise InboxProcessingFailed(error, expected_attempts=claimed["attempts"]) from error


def _process_formal_delivery_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
    request: Any,
    claimed: Any | None,
) -> ProcessFormalDeliveryResult:
    if claimed is None:
        if request["state"] != "PROCESSED":
            raise FormalDeliveryConflict("Formal Delivery request is unavailable")
        return _replay(request, dependencies=dependencies)
    admission, profile, branch_row = _read_admission(request, dependencies=dependencies)
    payload = _payload_for(request, branch_row)
    kind = _kind_from_topic(request["topic"])
    routing: FormalReviewRoutingSnapshot | None = None
    binding: Any | None = None
    preflight_block_reason: SourceControlReason | None = None
    if kind is FormalDeliveryRequestKind.CREATE_MR:
        try:
            preflight_block_reason = _formal_actor_block_reason(
                admission,
                actor_id=str(request["actor_id"]),
                merging=False,
                dependencies=dependencies,
            )
            _validate_project_and_head(admission, profile, dependencies=dependencies)
        except _FormalPreflightBlocked as blocked:
            preflight_block_reason = blocked.reason_code
        if preflight_block_reason is None:
            routing = _routing(admission, dependencies=dependencies)
    else:
        factory = _formal_repository(dependencies)
        with dependencies.engine.connect() as db:
            repository = factory(db)
            binding = repository.merge_request_binding_by_id(
                str(request["formal_merge_request_binding_id"])
            )
            assignment = (
                None
                if binding is None
                else repository.current_formal_review_assignment(str(binding["id"]))
            )
            observation = (
                None
                if binding is None
                else repository.latest_merge_request_observation(str(binding["id"]))
            )
        if binding is None or binding["kind"] != MergeRequestKind.FORMAL.value:
            preflight_block_reason = SourceControlReason.MR_CONFLICT
        elif (
            assignment is None
            or str(assignment["acceptance_decision_id"]) != admission.acceptance_decision_id
            or assignment["subject_head_sha"] != admission.requested_head_sha
            or observation is None
            or observation["head_sha"] != admission.requested_head_sha
        ):
            preflight_block_reason = SourceControlReason.HEAD_SHA_CHANGED
        elif observation["state"] != "OPEN":
            preflight_block_reason = SourceControlReason.MR_CLOSED
        if preflight_block_reason is None:
            try:
                _validate_project_and_head(admission, profile, dependencies=dependencies)
            except _FormalPreflightBlocked as blocked:
                preflight_block_reason = blocked.reason_code
    effect, owns_effect = _acquire_effect(request, payload, dependencies=dependencies)
    if not owns_effect:
        if effect.state in {EffectState.SUCCEEDED, EffectState.BLOCKED}:
            factory = _formal_repository(dependencies)
            with dependencies.engine.begin() as db:
                repository = factory(db)
                if effect.state is EffectState.SUCCEEDED:
                    _complete_request(
                        repository,
                        message_id=message_id,
                        expected_attempts=claimed["attempts"],
                        dependencies=dependencies,
                    )
                else:
                    if effect.last_error_code is None:
                        raise SourceControlDependencyUnavailable(
                            "Formal Delivery blocked reason is unavailable"
                        )
                    completed = repository.complete_formal_request_blocked(
                        message_id,
                        expected_attempts=claimed["attempts"],
                        reason_code=effect.last_error_code,
                        now=dependencies.clock.now(),
                    )
                    if completed is None:
                        raise RequirementCallbackUnavailable("Formal Delivery inbox lease was lost")
                request = repository.formal_request(message_id)
            if request is None:
                raise FormalDeliveryConflict("Formal Delivery request is unavailable")
            return _replay(request, dependencies=dependencies)
        raise FormalDeliveryConflict("Formal Delivery Effect is already in progress")
    if preflight_block_reason is not None:
        return _complete_effect_block(
            claimed,
            effect,
            reason_code=preflight_block_reason,
            expected_state=EffectState.IN_FLIGHT,
            dependencies=dependencies,
        )
    if effect.operation is _CREATE:
        if routing is None:
            raise SourceControlDependencyUnavailable("Formal Review routing unavailable")
        try:
            snapshot, origin = _provider_create(
                admission,
                profile,
                effect,
                actor_id=str(request["actor_id"]),
                dependencies=dependencies,
            )
        except _FormalPreflightBlocked as blocked:
            return _complete_effect_block(
                claimed,
                effect,
                reason_code=blocked.reason_code,
                expected_state=EffectState.IN_FLIGHT,
                dependencies=dependencies,
            )
        except GitLabResultUnknown:
            return _mark_unknown(
                effect,
                claimed,
                binding=None,
                dependencies=dependencies,
            )
        return _commit_create(
            admission,
            effect,
            snapshot,
            routing,
            branch_row=branch_row,
            creation_origin=origin,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            expected_state=EffectState.IN_FLIGHT,
            dependencies=dependencies,
        )
    if binding is None:
        raise FormalDeliveryConflict("Formal MR Binding is stale")
    actor_block_reason = _formal_merge_actor_block_reason(
        admission,
        actor_id=str(request["actor_id"]),
        dependencies=dependencies,
    )
    if actor_block_reason is not None:
        return _complete_effect_block(
            claimed,
            effect,
            reason_code=actor_block_reason,
            expected_state=EffectState.IN_FLIGHT,
            dependencies=dependencies,
        )
    try:
        snapshot = _provider_merge(
            admission,
            profile,
            binding,
            actor_id=str(request["actor_id"]),
            dependencies=dependencies,
        )
    except _FormalPreflightBlocked as blocked:
        return _complete_effect_block(
            claimed,
            effect,
            reason_code=blocked.reason_code,
            expected_state=EffectState.IN_FLIGHT,
            dependencies=dependencies,
        )
    except GitLabResultUnknown:
        return _mark_unknown(
            effect,
            claimed,
            binding=binding,
            dependencies=dependencies,
        )
    return _commit_merge(
        admission,
        effect,
        binding,
        snapshot,
        message_id=message_id,
        inbox_attempts=claimed["attempts"],
        expected_state=EffectState.IN_FLIGHT,
        dependencies=dependencies,
    )


def _return_reconciliation_unknown(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    factory = _formal_repository(dependencies)
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        repository = factory(db)
        row = repository.transition_effect(
            effect.id,
            expected_state=EffectState.RECONCILIATION.value,
            expected_attempts=effect.attempts,
            values={
                "state": EffectState.UNKNOWN.value,
                "last_error_code": "EXTERNAL_RESULT_UNKNOWN",
                "next_reconcile_at": now + timedelta(minutes=2),
                "updated_at": now,
            },
        )
        if row is None:
            raise RequirementCallbackUnavailable("Formal reconciliation lease was lost")
        _audit(
            repository,
            action="source_control.formal_reconciliation.unknown",
            target_type="source_control_effect",
            target_id=effect.id,
            dependencies=dependencies,
            result="UNKNOWN",
            reason="EXTERNAL_RESULT_UNKNOWN",
        )
    return effect_dto(row)


def _validate_reconciliation_coordinates(
    effect: SourceControlEffectDto,
    admission: FormalDeliveryAdmission,
    branch_row: Any,
) -> None:
    if (
        effect.requirement_id != admission.requirement_id
        or effect.work_item_id != admission.work_item_id
        or effect.repository_id != admission.repository_id
    ):
        raise FormalDeliveryConflict("Formal reconciliation coordinates are stale")
    if effect.operation is _CREATE:
        payload = effect.payload
        if (
            not isinstance(payload, CreateFormalMergeRequestEffectPayload)
            or str(branch_row["id"]) != payload.branch_binding_id
            or admission.acceptance_decision_id != payload.acceptance_decision_id
            or admission.requested_head_sha != payload.head_sha
            or admission.formal_review_decision_id is not None
        ):
            raise FormalDeliveryConflict("Formal create reconciliation coordinates are stale")
        return
    payload = effect.payload
    if (
        not isinstance(payload, MergeFormalMergeRequestEffectPayload)
        or admission.acceptance_decision_id != payload.acceptance_decision_id
        or admission.requested_head_sha != payload.requested_head_sha
        or admission.formal_merge_request_binding_id != payload.binding_id
        or admission.formal_review_decision_id != payload.review_decision_id
    ):
        raise FormalDeliveryConflict("Formal merge reconciliation coordinates are stale")


def _complete_reconciliation_block(
    effect: SourceControlEffectDto,
    *,
    reason_code: SourceControlReason,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    factory = _formal_repository(dependencies)
    with dependencies.engine.connect() as db:
        request = factory(db).formal_request_for_effect(
            operation=effect.operation.value,
            work_item_id=effect.work_item_id,
            requirement_id=effect.requirement_id,
            repository_id=effect.repository_id,
            request_fingerprint=effect.request_fingerprint,
        )
    if request is None:
        _return_reconciliation_unknown(effect, dependencies=dependencies)
        raise SourceControlDependencyUnavailable(
            "Formal reconciliation request context unavailable"
        )
    return _complete_effect_block(
        request,
        effect,
        reason_code=reason_code,
        expected_state=EffectState.RECONCILIATION,
        dependencies=dependencies,
    )


def _reconcile_claimed_formal_effect(
    reconciling: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    factory = _formal_repository(dependencies)
    requirement = dependencies.requirement_formal_delivery
    try:
        with dependencies.engine.connect() as db:
            repository = factory(db)
            branch_row = repository.branch_binding_by_work_item(reconciling.work_item_id)
        if requirement is None or branch_row is None:
            raise SourceControlDependencyUnavailable("Formal reconciliation context unavailable")
        admission = requirement.delivery_admission(reconciling.work_item_id)
        _validate_reconciliation_coordinates(reconciling, admission, branch_row)
        with dependencies.engine.connect() as db:
            repository_row = factory(db).repository_by_id(admission.repository_id)
        if repository_row is None:
            raise FormalDeliveryConflict("Formal reconciliation repository is unavailable")
        profile = repository_profile(repository_row)
        if reconciling.operation is _CREATE:
            with dependencies.engine.connect() as db:
                request = factory(db).formal_request_for_effect(
                    operation=reconciling.operation.value,
                    work_item_id=reconciling.work_item_id,
                    requirement_id=reconciling.requirement_id,
                    repository_id=reconciling.repository_id,
                    request_fingerprint=reconciling.request_fingerprint,
                )
            if request is None or str(request["actor_id"]) != admission.human_owner_id:
                raise _FormalPreflightBlocked(SourceControlReason.OWNER_INELIGIBLE)
            denied = _formal_actor_block_reason(
                admission,
                actor_id=str(request["actor_id"]),
                merging=False,
                dependencies=dependencies,
            )
            if denied is not None:
                raise _FormalPreflightBlocked(denied)
            gitlab = _validate_project_and_head(
                admission,
                profile,
                dependencies=dependencies,
            )
            candidates = gitlab.list_merge_requests(
                profile,
                source_branch=admission.task_branch,
                target_branch="main",
                state="all",
            )
            if len(candidates) > 1:
                raise _FormalPreflightBlocked(SourceControlReason.MR_CONFLICT)
            if not candidates:
                raise GitLabResultUnknown("Formal MR reconciliation is inconclusive")
            snapshot = gitlab.get_merge_request(profile, iid=candidates[0].iid)
            if snapshot.head_sha != admission.requested_head_sha:
                raise _FormalPreflightBlocked(SourceControlReason.HEAD_SHA_CHANGED)
            if snapshot.state != "opened":
                raise _FormalPreflightBlocked(SourceControlReason.MR_CLOSED)
            if snapshot.source_branch != admission.task_branch or snapshot.target_branch != "main":
                raise _FormalPreflightBlocked(SourceControlReason.MR_CONFLICT)
            return _commit_create(
                admission,
                reconciling,
                snapshot,
                _routing(admission, dependencies=dependencies),
                branch_row=branch_row,
                creation_origin=MergeRequestCreationOrigin.EXTERNAL_ADOPTED,
                message_id=None,
                inbox_attempts=None,
                expected_state=EffectState.RECONCILIATION,
                dependencies=dependencies,
            )
        payload = reconciling.payload
        if not isinstance(payload, MergeFormalMergeRequestEffectPayload):
            raise FormalDeliveryConflict("Formal merge Effect payload is invalid")
        with dependencies.engine.connect() as db:
            binding = factory(db).merge_request_binding_by_id(payload.binding_id)
        if binding is None:
            raise FormalDeliveryConflict("Formal merge reconciliation Binding is unavailable")
        gitlab = _validate_project(profile, dependencies=dependencies)
        snapshot = gitlab.get_merge_request(profile, iid=binding["merge_request_iid"])
        if snapshot.state != "merged":
            with dependencies.engine.connect() as db:
                request = factory(db).formal_request_for_effect(
                    operation=reconciling.operation.value,
                    work_item_id=reconciling.work_item_id,
                    requirement_id=reconciling.requirement_id,
                    repository_id=reconciling.repository_id,
                    request_fingerprint=reconciling.request_fingerprint,
                )
            if request is None:
                raise SourceControlDependencyUnavailable(
                    "Formal reconciliation request context unavailable"
                )
            actor_block_reason = _formal_merge_actor_block_reason(
                admission,
                actor_id=str(request["actor_id"]),
                dependencies=dependencies,
            )
            if actor_block_reason is not None:
                raise _FormalPreflightBlocked(actor_block_reason)
            snapshot = _provider_merge(
                admission,
                profile,
                binding,
                actor_id=str(request["actor_id"]),
                dependencies=dependencies,
            )
        if snapshot.head_sha != payload.requested_head_sha:
            raise _FormalPreflightBlocked(SourceControlReason.HEAD_SHA_CHANGED)
        if (
            snapshot.project_id != binding["external_project_id"]
            or snapshot.iid != binding["merge_request_iid"]
            or snapshot.source_branch != admission.task_branch
            or snapshot.target_branch != "main"
        ):
            raise _FormalPreflightBlocked(SourceControlReason.MR_CONFLICT)
        if (
            snapshot.state != "merged"
            or snapshot.merge_commit_sha is None
            or snapshot.merged_at is None
        ):
            raise GitLabResultUnknown("Formal merge reconciliation is inconclusive")
        return _commit_merge(
            admission,
            reconciling,
            binding,
            snapshot,
            message_id=None,
            inbox_attempts=None,
            expected_state=EffectState.RECONCILIATION,
            dependencies=dependencies,
        )
    except _FormalPreflightBlocked as blocked:
        return _complete_reconciliation_block(
            reconciling,
            reason_code=blocked.reason_code,
            dependencies=dependencies,
        )
    except _DETERMINISTIC_PROVIDER_ERRORS as error:
        return _complete_reconciliation_block(
            reconciling,
            reason_code=_provider_block_reason(error),
            dependencies=dependencies,
        )
    except (GitLabProviderUnavailable, GitLabResultUnknown) as error:
        _return_reconciliation_unknown(reconciling, dependencies=dependencies)
        raise SourceControlDependencyUnavailable(
            "Formal MR reconciliation remains unknown"
        ) from error
    except Exception:
        _return_reconciliation_unknown(reconciling, dependencies=dependencies)
        raise


def reconcile_formal_delivery_effect(
    *,
    effect_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessFormalDeliveryResult:
    factory = _formal_repository(dependencies)
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        claimed = factory(db).claim_effect(
            effect_id,
            now=now,
            lease_until=now + timedelta(minutes=2),
        )
    if claimed is None:
        raise FormalDeliveryConflict("Formal Delivery Effect is not due for reconciliation")
    return _reconcile_claimed_formal_effect(
        effect_dto(claimed),
        dependencies=dependencies,
    )


def reconcile_due_formal_effects(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> tuple[SourceControlEffectDto, ...]:
    if limit < 1:
        raise ValueError("Formal reconciliation limit must be positive")
    factory = _formal_repository(dependencies)

    def reconcile_effect_lane(lane_limit: int) -> list[SourceControlEffectDto]:
        if lane_limit < 1:
            return []
        now = dependencies.clock.now()
        with dependencies.engine.begin() as db:
            rows = factory(db).claim_effects(
                limit=lane_limit,
                now=now,
                lease_until=now + timedelta(minutes=2),
            )
        lane: list[SourceControlEffectDto] = []
        for row in rows:
            claimed = effect_dto(row)
            try:
                result = _reconcile_claimed_formal_effect(
                    claimed,
                    dependencies=dependencies,
                )
            except Exception:
                with dependencies.engine.connect() as db:
                    current = factory(db).effect_by_id(claimed.id)
                if current is None:
                    raise
                lane.append(effect_dto(current))
            else:
                if result.effect is None:
                    raise RequirementCallbackUnavailable(
                        "Formal reconciliation Effect is unavailable"
                    )
                lane.append(result.effect)
        return lane

    callback_quota = max(1, limit // 2)
    effect_quota = limit - callback_quota
    effects = reconcile_effect_lane(effect_quota)
    effects.extend(
        replay_pending_formal_callbacks(
            limit=callback_quota,
            excluded_effect_ids=frozenset(effect.id for effect in effects),
            dependencies=dependencies,
        )
    )
    remaining = limit - len(effects)
    if remaining > 0:
        effects.extend(reconcile_effect_lane(remaining))
        remaining = limit - len(effects)
    if remaining > 0:
        effects.extend(
            replay_pending_formal_callbacks(
                limit=remaining,
                excluded_effect_ids=frozenset(effect.id for effect in effects),
                dependencies=dependencies,
            )
        )
    return tuple(effects)
