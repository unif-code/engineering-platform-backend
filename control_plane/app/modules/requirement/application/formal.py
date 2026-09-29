import hashlib
import json
import re
from typing import Any

from control_plane.app.modules.audit import AuditEnvelope, record
from control_plane.app.modules.requirement.application.acceptance import (
    _assignment_dto,
    _decision_dto,
    _gate_dto,
    _selection_dto,
    _validate_evidence_artifacts,
)
from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    requirement_dto,
    validated_correlation_id,
    work_item_dto,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    AcceptanceDecisionResult,
    AcceptanceStale,
    DecisionOutcome,
    DeliveryGateState,
    DeliveryGateType,
    EvidenceUnavailableOrStale,
    FormalDeliveryAdmission,
    FormalDeliveryBlocked,
    FormalDeliveryBlockedReason,
    FormalDeliveryBlockedResult,
    FormalDeliveryCommandResult,
    FormalDeliveryConflict,
    FormalDeliveryRequestKind,
    FormalDeliveryRequestMessage,
    FormalDeliveryState,
    FormalMergedResult,
    FormalMrReadyResult,
    FormalReviewStale,
    GateNotFound,
    GateReviewerIneligible,
    GateReviewerMismatch,
    IntegrationDeliveryState,
    InvalidRequirementInput,
    RequirementDependencyUnavailable,
    RequirementError,
    RequirementNotFound,
    RequirementState,
    StaleRequirementRevision,
    StaleWorkItemRevision,
    WorkItemNotFound,
    WorkItemState,
)
from control_plane.app.modules.requirement.ports import (
    DeliveryGatePolicySnapshot,
    IntegrationBaselineEvidenceWorkItem,
    RequirementRepository,
)
from control_plane.app.modules.requirement.ports.runtime import frozen_gate_capabilities
from control_plane.app.shared.api.request_id import current_request_id
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_CREATE_TOPIC = "requirement.formal-merge-request.requested"
_MERGE_TOPIC = "requirement.formal-merge.requested"
_CREATE_OPERATION = "requirement_request_formal_merge_request"
_MERGE_OPERATION = "requirement_request_formal_merge"
_READY_OPERATION = "requirement_record_formal_mr_ready"
_MERGED_OPERATION = "requirement_record_formal_merged"
_BLOCKED_OPERATION = "requirement_record_formal_delivery_blocked"
_REVIEW_OPERATION = "requirement_decide_formal_review"


def _ready_formal_invalidation_reason(
    repository: RequirementRepository,
    requirement_id: str,
) -> str | None:
    reason = repository.formal_invalidation_block_reason(requirement_id)
    if reason is None or repository.has_pending_formal_delivery(requirement_id):
        return None
    return reason


def _apply_ready_formal_invalidation(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    reason: str,
    now: Any,
) -> None:
    reopened = repository.reopen_formal_invalidation_blocks_for_rework(
        requirement_id,
        now=now,
    )
    if not reopened:
        raise FormalDeliveryConflict("Formal Delivery invalidation facts are unavailable")
    repository.invalidate_current_delivery_evidence(
        requirement_id,
        reason=f"FORMAL_DELIVERY_{reason}",
        now=now,
    )


def _audit_denial(
    *,
    dependencies: RequirementDependencies,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    error: Exception,
) -> None:
    record(
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor,
            actor_type="HUMAN" if not actor.startswith("SYSTEM:") else "SYSTEM",
            action=f"{action}_denied",
            target_type=target_type,
            target_id=target_id,
            result="DENIED",
            reason=f"reasonCode={type(error).__name__.upper()}",
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.denial_audit,
    )


def _current_evidence_item(
    context: Any,
    dependencies: RequirementDependencies,
) -> IntegrationBaselineEvidenceWorkItem:
    if (
        RequirementState(context["requirement_state"])
        not in {RequirementState.AWAITING_MERGE, RequirementState.COMPLETED}
        or context["acceptance_decision_id"] is None
        or context["acceptance_outcome"] != DecisionOutcome.APPROVED.value
        or context["acceptance_validity"] != "CURRENT"
        or context["integration_baseline_id"] is None
        or context["integration_baseline_hash"] is None
    ):
        raise AcceptanceStale("Requirement Acceptance is not current")
    reader = dependencies.integration_evidence
    if reader is None:
        raise RequirementDependencyUnavailable("Integration Baseline Evidence unavailable")
    try:
        evidence = reader.get(str(context["integration_baseline_id"]))
    except Exception as error:
        raise EvidenceUnavailableOrStale(
            "Integration Baseline Evidence lookup failed closed"
        ) from error
    if evidence.currentness_state == "UNAVAILABLE":
        raise EvidenceUnavailableOrStale(
            "Integration Baseline Evidence currentness proof is unavailable"
        )
    if evidence.currentness_state != "CURRENT":
        raise EvidenceUnavailableOrStale("Integration Baseline Evidence is stale")
    if (
        evidence.id != str(context["integration_baseline_id"])
        or evidence.evidence_hash != context["integration_baseline_hash"]
    ):
        raise AcceptanceStale("Accepted Evidence hash is stale")
    _validate_evidence_artifacts(str(context["requirement_id"]), evidence, dependencies)
    matches = tuple(
        item for item in evidence.work_items if item.work_item_id == str(context["work_item_id"])
    )
    if len(matches) != 1:
        raise FormalDeliveryConflict("Accepted Evidence does not cover the WorkItem")
    return matches[0]


def get_formal_delivery_admission(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    dependencies: RequirementDependencies,
) -> FormalDeliveryAdmission:
    context = repository.formal_delivery_context(work_item_id)
    if context is None:
        raise WorkItemNotFound(work_item_id)
    evidence_item = _current_evidence_item(context, dependencies)
    if (
        context["task_branch"] is None
        or context["human_owner_id"] is None
        or IntegrationDeliveryState(context["integration_delivery_state"])
        is not IntegrationDeliveryState.INTEGRATED
    ):
        raise FormalDeliveryConflict("WorkItem is not ready for Formal Delivery")
    formal_review_decision_id = (
        context["formal_review_decision_id"]
        if context["formal_review_validity"] == "CURRENT"
        else None
    )
    if FormalDeliveryState(context["formal_delivery_state"]) is FormalDeliveryState.MERGE_PENDING:
        if (
            formal_review_decision_id is None
            or context["formal_review_outcome"] != DecisionOutcome.APPROVED.value
            or context["formal_review_validity"] != "CURRENT"
            or context["formal_gate_head_sha"] != evidence_item.task_commit_sha
        ):
            raise FormalDeliveryConflict("Formal Review is not current")
    return FormalDeliveryAdmission(
        requirement_id=str(context["requirement_id"]),
        requirement_revision=context["requirement_revision"],
        requirement_version=context["requirement_version"],
        workspace_id=str(context["workspace_id"]),
        work_item_id=str(context["work_item_id"]),
        work_item_revision=context["work_item_revision"],
        repository_id=str(context["repository_id"]),
        task_branch=context["task_branch"],
        requested_head_sha=evidence_item.task_commit_sha,
        human_owner_id=context["human_owner_id"],
        acceptance_decision_id=str(context["acceptance_decision_id"]),
        formal_merge_request_binding_id=(
            None
            if context["formal_merge_request_binding_id"] is None
            else str(context["formal_merge_request_binding_id"])
        ),
        formal_review_decision_id=(
            None if formal_review_decision_id is None else str(formal_review_decision_id)
        ),
    )


def _request_formal_delivery(
    repository: RequirementRepository,
    *,
    kind: FormalDeliveryRequestKind,
    requirement_id: str,
    work_item_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> FormalDeliveryCommandResult:
    stable_actor = actor_id(actor)
    operation, topic, path = (
        (_CREATE_OPERATION, _CREATE_TOPIC, "requirement.request-formal-merge-request")
        if kind is FormalDeliveryRequestKind.CREATE_MR
        else (_MERGE_OPERATION, _MERGE_TOPIC, "requirement.request-formal-merge")
    )
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=operation,
        method="COMMAND",
        path=path,
        body={
            "expectedRevision": expected_revision,
            "requirementId": requirement_id,
            "workItemId": work_item_id,
        },
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    try:
        requirement = repository.requirement_by_id(requirement_id, for_update=True)
        if requirement is None:
            raise RequirementNotFound(requirement_id)
        existing = repository.idempotency_by_scope(stable_actor, operation, idempotency_key)
        if existing is None and requirement["revision"] != expected_revision:
            raise StaleRequirementRevision(requirement_id)

        def command() -> IdempotentResponse:
            work_item = repository.work_item_by_id(work_item_id, for_update=True)
            if work_item is None or str(work_item["requirement_id"]) != requirement_id:
                raise WorkItemNotFound(work_item_id)
            context = repository.formal_delivery_context(work_item_id)
            if context is None:
                raise WorkItemNotFound(work_item_id)
            if repository.formal_invalidation_block_reason(requirement_id) is not None:
                raise FormalDeliveryBlocked("Formal Delivery evidence invalidation is pending")
            evidence_item = _current_evidence_item(context, dependencies)
            if (
                kind is FormalDeliveryRequestKind.CREATE_MR
                and stable_actor != work_item["human_owner_id"]
            ):
                raise FormalDeliveryBlocked("Only the current WorkItem owner may request delivery")
            formal_state = FormalDeliveryState(work_item["formal_delivery_state"])
            binding_id = (
                None
                if work_item["formal_merge_request_binding_id"] is None
                else str(work_item["formal_merge_request_binding_id"])
            )
            initial_create = (
                kind is FormalDeliveryRequestKind.CREATE_MR
                and formal_state is FormalDeliveryState.NOT_STARTED
                and binding_id is None
            )
            refreshed_review = (
                kind is FormalDeliveryRequestKind.CREATE_MR
                and formal_state is FormalDeliveryState.MR_OPEN
                and binding_id is not None
                and context["formal_gate_state"] == DeliveryGateState.INVALIDATED.value
                and context["formal_review_validity"] == "INVALIDATED"
                and context["formal_gate_selection_id"] is not None
                and context["current_integration_baseline_selection_id"] is not None
                and str(context["formal_gate_selection_id"])
                != str(context["current_integration_baseline_selection_id"])
            )
            merge_request = (
                kind is FormalDeliveryRequestKind.MERGE_MR
                and formal_state in {FormalDeliveryState.MR_OPEN, FormalDeliveryState.BLOCKED}
                and binding_id is not None
            )
            retried_create = (
                kind is FormalDeliveryRequestKind.CREATE_MR
                and formal_state is FormalDeliveryState.BLOCKED
                and (
                    binding_id is None
                    or (
                        context["formal_gate_state"] == DeliveryGateState.INVALIDATED.value
                        and context["formal_review_validity"] == "INVALIDATED"
                        and context["formal_gate_selection_id"] is not None
                        and context["current_integration_baseline_selection_id"] is not None
                        and str(context["formal_gate_selection_id"])
                        != str(context["current_integration_baseline_selection_id"])
                    )
                )
            )
            if (
                not (initial_create or refreshed_review or retried_create or merge_request)
                or IntegrationDeliveryState(work_item["integration_delivery_state"])
                is not IntegrationDeliveryState.INTEGRATED
            ):
                raise FormalDeliveryBlocked("Formal Delivery state is stale")
            if kind is FormalDeliveryRequestKind.MERGE_MR:
                if (
                    context["formal_review_decision_id"] is None
                    or context["formal_review_outcome"] != DecisionOutcome.APPROVED.value
                    or context["formal_review_validity"] != "CURRENT"
                    or context["formal_gate_head_sha"] != evidence_item.task_commit_sha
                ):
                    raise FormalDeliveryBlocked("Formal Review is not approved for this head")
            now = dependencies.clock.now()
            target_formal_state = (
                FormalDeliveryState.MR_PENDING
                if kind is FormalDeliveryRequestKind.CREATE_MR
                else FormalDeliveryState.MERGE_PENDING
            )
            updated_work_item = repository.update_work_item_formal_delivery(
                work_item_id,
                expected_revision=work_item["revision"],
                state=WorkItemState.AWAITING_MERGE.value,
                formal_state=target_formal_state.value,
                binding_id=binding_id,
                blocked_reason=None,
                now=now,
            )
            if updated_work_item is None:
                raise StaleWorkItemRevision(work_item_id)
            updated_requirement = repository.touch_requirement(
                requirement_id,
                expected_revision=expected_revision,
                now=now,
            )
            if updated_requirement is None:
                raise StaleRequirementRevision(requirement_id)
            message_id = str(dependencies.random.uuid4())
            repository.insert_outbox(
                id=message_id,
                topic=topic,
                aggregate_type="REQUIREMENT",
                aggregate_id=requirement_id,
                aggregate_version=updated_requirement["revision"],
                payload={
                    "acceptanceDecisionId": str(context["acceptance_decision_id"]),
                    "actorId": stable_actor,
                    "formalMergeRequestBindingId": (
                        binding_id
                        if kind is FormalDeliveryRequestKind.CREATE_MR
                        else str(work_item["formal_merge_request_binding_id"])
                    ),
                    "formalReviewDecisionId": (
                        None
                        if kind is FormalDeliveryRequestKind.CREATE_MR
                        else str(context["formal_review_decision_id"])
                    ),
                    "kind": kind.value,
                    "repositoryId": str(work_item["repository_id"]),
                    "requestedHeadSha": evidence_item.task_commit_sha,
                    "requirementId": requirement_id,
                    "requirementRevision": updated_requirement["revision"],
                    "workItemId": work_item_id,
                    "workItemRevision": updated_work_item["revision"],
                },
                now=now,
            )
            audit(
                repository,
                dependencies=dependencies,
                actor=stable_actor,
                action=(
                    "requirement.formal_mr.requested"
                    if kind is FormalDeliveryRequestKind.CREATE_MR
                    else "requirement.formal_merge.requested"
                ),
                target_type="WORK_ITEM",
                target_id=work_item_id,
                reason=f"headSha={evidence_item.task_commit_sha}",
            )
            result = FormalDeliveryCommandResult(
                requirement=requirement_dto(updated_requirement),
                work_item=work_item_dto(updated_work_item),
                outbox_topic=topic,
            )
            return IdempotentResponse(status_code=202, body=result.model_dump(mode="json"))

        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=operation,
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except (RequirementError, IdempotencyConflict) as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action=(
                "requirement.formal_mr.request"
                if kind is FormalDeliveryRequestKind.CREATE_MR
                else "requirement.formal_merge.request"
            ),
            target_type="WORK_ITEM",
            target_id=work_item_id,
            error=error,
        )
        raise
    return FormalDeliveryCommandResult.model_validate(execution.response.body)


def request_formal_merge_request(
    repository: RequirementRepository,
    **kwargs: Any,
) -> FormalDeliveryCommandResult:
    return _request_formal_delivery(
        repository,
        kind=FormalDeliveryRequestKind.CREATE_MR,
        **kwargs,
    )


def request_formal_merge(
    repository: RequirementRepository,
    **kwargs: Any,
) -> FormalDeliveryCommandResult:
    return _request_formal_delivery(
        repository,
        kind=FormalDeliveryRequestKind.MERGE_MR,
        **kwargs,
    )


def record_formal_delivery_blocked(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    binding_id: str | None,
    reason_code: FormalDeliveryBlockedReason,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> FormalDeliveryBlockedResult:
    if not isinstance(reason_code, FormalDeliveryBlockedReason):
        raise InvalidRequirementInput("Formal Delivery blocked reason is invalid")
    stable_actor = actor_id(actor)
    stable_correlation = validated_correlation_id(correlation_id)
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=_BLOCKED_OPERATION,
        method="COMMAND",
        path="requirement.record-formal-delivery-blocked",
        body={
            "bindingId": binding_id,
            "expectedRevision": expected_revision,
            "reasonCode": reason_code.value,
            "workItemId": work_item_id,
        },
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        work_item = repository.work_item_by_id(work_item_id, for_update=True)
        if work_item is None:
            raise WorkItemNotFound(work_item_id)
        if work_item["revision"] != expected_revision:
            raise StaleWorkItemRevision(work_item_id)
        requirement_id = str(work_item["requirement_id"])
        requirement = repository.requirement_by_id(requirement_id, for_update=True)
        if requirement is None:
            raise RequirementNotFound(requirement_id)
        formal_state = FormalDeliveryState(work_item["formal_delivery_state"])
        current_binding = (
            None
            if work_item["formal_merge_request_binding_id"] is None
            else str(work_item["formal_merge_request_binding_id"])
        )
        if (
            formal_state not in {FormalDeliveryState.MR_PENDING, FormalDeliveryState.MERGE_PENDING}
            or (current_binding is not None and binding_id != current_binding)
            or (
                formal_state is FormalDeliveryState.MERGE_PENDING
                and (current_binding is None or binding_id is None)
            )
        ):
            raise FormalDeliveryConflict("Formal Delivery blocked callback is stale")
        stable_binding = current_binding or binding_id
        now = dependencies.clock.now()
        updated_work_item = repository.update_work_item_formal_delivery(
            work_item_id,
            expected_revision=expected_revision,
            state=WorkItemState.AWAITING_MERGE.value,
            formal_state=FormalDeliveryState.BLOCKED.value,
            binding_id=stable_binding,
            blocked_reason=reason_code.value,
            now=now,
        )
        if updated_work_item is None:
            raise StaleWorkItemRevision(work_item_id)
        ready_invalidation_reason = _ready_formal_invalidation_reason(
            repository,
            requirement_id,
        )
        updated_requirement = repository.update_requirement_state(
            requirement_id,
            expected_revision=requirement["revision"],
            state=(
                RequirementState.IN_PROGRESS.value
                if ready_invalidation_reason is not None
                else RequirementState.AWAITING_MERGE.value
            ),
            now=now,
        )
        if updated_requirement is None:
            raise StaleRequirementRevision(requirement_id)
        if ready_invalidation_reason is not None:
            _apply_ready_formal_invalidation(
                repository,
                requirement_id=requirement_id,
                reason=ready_invalidation_reason,
                now=now,
            )
            refreshed_work_item = repository.work_item_by_id(work_item_id, for_update=True)
            if refreshed_work_item is None:
                raise WorkItemNotFound(work_item_id)
            updated_work_item = refreshed_work_item
            refreshed_requirement = repository.requirement_by_id(
                requirement_id,
                for_update=True,
            )
            if refreshed_requirement is None:
                raise RequirementNotFound(requirement_id)
            updated_requirement = refreshed_requirement
        audit(
            repository,
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.formal_delivery.blocked",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            reason=f"reasonCode={reason_code.value}",
            correlation_id=stable_correlation,
        )
        result = FormalDeliveryBlockedResult(
            requirement=requirement_dto(updated_requirement),
            work_item=work_item_dto(updated_work_item),
            reason_code=reason_code,
        )
        return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_BLOCKED_OPERATION,
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except (RequirementError, IdempotencyConflict) as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.formal_delivery.blocked",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            error=error,
        )
        raise
    return FormalDeliveryBlockedResult.model_validate(execution.response.body)


def _validated_routing_policy(policy: DeliveryGatePolicySnapshot) -> None:
    if (
        not policy.default_reviewer_id.strip()
        or not policy.policy_code.strip()
        or not _SHA256.fullmatch(policy.snapshot_hash)
    ):
        raise RequirementDependencyUnavailable("Formal Review routing is invalid")


def record_formal_mr_ready(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    binding_id: str,
    head_sha: str,
    expected_revision: int,
    assignment: DeliveryGatePolicySnapshot,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> FormalMrReadyResult:
    stable_actor = actor_id(actor)
    stable_correlation = validated_correlation_id(correlation_id)
    _validated_routing_policy(assignment)
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=_READY_OPERATION,
        method="COMMAND",
        path="requirement.record-formal-mr-ready",
        body={
            "assignment": assignment.model_dump(mode="json"),
            "bindingId": binding_id,
            "expectedRevision": expected_revision,
            "headSha": head_sha,
            "workItemId": work_item_id,
        },
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        work_item = repository.work_item_by_id(work_item_id, for_update=True)
        if work_item is None:
            raise WorkItemNotFound(work_item_id)
        if work_item["revision"] != expected_revision:
            raise StaleWorkItemRevision(work_item_id)
        requirement_id = str(work_item["requirement_id"])
        requirement = repository.requirement_by_id(requirement_id, for_update=True)
        if requirement is None:
            raise RequirementNotFound(requirement_id)
        context = repository.formal_delivery_context(work_item_id)
        if context is None:
            raise WorkItemNotFound(work_item_id)
        evidence_item = _current_evidence_item(context, dependencies)
        if (
            FormalDeliveryState(work_item["formal_delivery_state"])
            is not FormalDeliveryState.MR_PENDING
            or (
                work_item["formal_merge_request_binding_id"] is not None
                and str(work_item["formal_merge_request_binding_id"]) != binding_id
            )
            or evidence_item.task_commit_sha != head_sha
            or not _COMMIT_SHA.fullmatch(head_sha)
        ):
            raise FormalDeliveryConflict("Formal MR callback is stale")
        selection_id = str(requirement["current_integration_baseline_selection_id"])
        selection = repository.integration_baseline_selection_by_id(selection_id)
        if selection is None or selection["invalidated_at"] is not None:
            raise AcceptanceStale("Acceptance Selection is stale")
        now = dependencies.clock.now()
        gate_id = str(dependencies.random.uuid4())
        gate = repository.insert_delivery_gate(
            id=gate_id,
            gate_type=DeliveryGateType.FORMAL_MR_REVIEW.value,
            requirement_id=requirement_id,
            work_item_id=work_item_id,
            selection_id=selection_id,
            requirement_version=requirement["requirement_version"],
            acceptance_criteria_version=requirement["acceptance_criteria_version"],
            acceptance_criteria_hash=requirement["acceptance_criteria_hash"],
            integration_baseline_id=selection["integration_baseline_id"],
            integration_baseline_hash=selection["integration_baseline_hash"],
            formal_merge_request_binding_id=binding_id,
            subject_head_sha=head_sha,
            policy_code=assignment.policy_code,
            policy_version=assignment.version,
            policy_snapshot_hash=assignment.snapshot_hash,
            now=now,
        )
        gate_assignment = repository.insert_delivery_gate_assignment(
            id=str(dependencies.random.uuid4()),
            gate_id=gate_id,
            default_reviewer_id=assignment.default_reviewer_id,
            current_reviewer_id=assignment.default_reviewer_id,
            resolution_snapshot=assignment.resolution_snapshot,
            now=now,
        )
        updated_work_item = repository.update_work_item_formal_delivery(
            work_item_id,
            expected_revision=expected_revision,
            state=WorkItemState.AWAITING_MERGE.value,
            formal_state=FormalDeliveryState.MR_OPEN.value,
            binding_id=binding_id,
            blocked_reason=None,
            now=now,
        )
        if updated_work_item is None:
            raise StaleWorkItemRevision(work_item_id)
        ready_invalidation_reason = _ready_formal_invalidation_reason(
            repository,
            requirement_id,
        )
        updated_requirement = (
            repository.update_requirement_state(
                requirement_id,
                expected_revision=requirement["revision"],
                state=RequirementState.IN_PROGRESS.value,
                now=now,
            )
            if ready_invalidation_reason is not None
            else repository.touch_requirement(
                requirement_id,
                expected_revision=requirement["revision"],
                now=now,
            )
        )
        if updated_requirement is None:
            raise StaleRequirementRevision(requirement_id)
        if ready_invalidation_reason is not None:
            _apply_ready_formal_invalidation(
                repository,
                requirement_id=requirement_id,
                reason=ready_invalidation_reason,
                now=now,
            )
            refreshed_work_item = repository.work_item_by_id(work_item_id, for_update=True)
            refreshed_requirement = repository.requirement_by_id(
                requirement_id,
                for_update=True,
            )
            refreshed_gate = repository.delivery_gate_by_id(gate_id)
            if (
                refreshed_work_item is None
                or refreshed_requirement is None
                or refreshed_gate is None
            ):
                raise FormalDeliveryConflict(
                    "Formal Delivery invalidation projection is unavailable"
                )
            updated_work_item = refreshed_work_item
            updated_requirement = refreshed_requirement
            gate = refreshed_gate
        audit(
            repository,
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.formal_mr.ready",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            reason=f"bindingId={binding_id}; headSha={head_sha}",
            correlation_id=stable_correlation,
        )
        result = FormalMrReadyResult(
            requirement=requirement_dto(updated_requirement),
            work_item=work_item_dto(updated_work_item),
            gate=_gate_dto(gate),
            assignment=_assignment_dto(gate_assignment),
        )
        return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_READY_OPERATION,
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except (RequirementError, IdempotencyConflict) as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.formal_mr.ready",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            error=error,
        )
        raise
    return FormalMrReadyResult.model_validate(execution.response.body)


def decide_formal_review(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    gate_id: str,
    outcome: DecisionOutcome,
    reason: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> AcceptanceDecisionResult:
    stable_actor = actor_id(actor)
    stable_reason = reason.strip()
    if not stable_reason:
        raise InvalidRequirementInput("Formal Review reason is required")
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=_REVIEW_OPERATION,
        method="COMMAND",
        path="requirement.decide-formal-review",
        body={
            "expectedRevision": expected_revision,
            "gateId": gate_id,
            "outcome": outcome.value,
            "reason": stable_reason,
            "requirementId": requirement_id,
        },
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    try:
        requirement = repository.requirement_by_id(requirement_id, for_update=True)
        if requirement is None:
            raise RequirementNotFound(requirement_id)
        existing = repository.idempotency_by_scope(
            stable_actor,
            _REVIEW_OPERATION,
            idempotency_key,
        )
        if existing is None and requirement["revision"] != expected_revision:
            raise StaleRequirementRevision(requirement_id)

        def command() -> IdempotentResponse:
            gate = repository.delivery_gate_by_id(gate_id, for_update=True)
            if (
                gate is None
                or str(gate["requirement_id"]) != requirement_id
                or gate["gate_type"] != DeliveryGateType.FORMAL_MR_REVIEW.value
            ):
                raise GateNotFound(gate_id)
            if gate["state"] != DeliveryGateState.OPEN.value:
                raise FormalReviewStale("Formal Review Gate is not open")
            work_item_id = str(gate["work_item_id"])
            work_item = repository.work_item_by_id(work_item_id, for_update=True)
            if work_item is None:
                raise WorkItemNotFound(work_item_id)
            context = repository.formal_delivery_context(work_item_id)
            if context is None:
                raise WorkItemNotFound(work_item_id)
            evidence_item = _current_evidence_item(context, dependencies)
            if (
                context["formal_delivery_state"] != FormalDeliveryState.MR_OPEN.value
                or str(context["formal_merge_request_binding_id"])
                != str(gate["formal_merge_request_binding_id"])
                or evidence_item.task_commit_sha != gate["subject_head_sha"]
                or gate["requirement_version"] != requirement["requirement_version"]
                or gate["acceptance_criteria_hash"] != requirement["acceptance_criteria_hash"]
            ):
                raise FormalReviewStale("Formal Review subject is stale")
            selection = repository.integration_baseline_selection_by_id(str(gate["selection_id"]))
            if selection is None or selection["invalidated_at"] is not None:
                raise AcceptanceStale("Acceptance Selection is stale")
            assignment = repository.current_delivery_gate_assignment(
                gate_id,
                for_update=True,
            )
            if assignment is None:
                raise FormalReviewStale("Formal Review assignment is unavailable")
            if assignment["current_reviewer_id"] != stable_actor:
                raise GateReviewerMismatch(stable_actor)
            guard = dependencies.delivery_reviewer_guard
            if guard is None:
                raise RequirementDependencyUnavailable("Formal Review eligibility unavailable")
            try:
                capabilities = frozen_gate_capabilities(
                    dict(assignment["resolution_snapshot"]),
                    gate["gate_type"],
                    expected_version=gate["policy_version"],
                    expected_hash=gate["policy_snapshot_hash"],
                )
                eligibility = guard.evaluate(
                    actor_id=stable_actor,
                    workspace_id=str(requirement["workspace_id"]),
                    required_capabilities=capabilities,
                )
            except Exception as error:
                raise RequirementDependencyUnavailable(
                    "Formal Review eligibility failed closed"
                ) from error
            if (
                not eligibility.eligible
                or eligibility.workspace_id != str(requirement["workspace_id"])
                or eligibility.actor_id != stable_actor
                or eligibility.required_capabilities != capabilities
                or not _SHA256.fullmatch(eligibility.snapshot_hash)
            ):
                raise GateReviewerIneligible(stable_actor)
            now = dependencies.clock.now()
            decision = repository.insert_delivery_decision(
                id=str(dependencies.random.uuid4()),
                gate_id=gate_id,
                gate_assignment_id=str(assignment["id"]),
                reviewer_id=stable_actor,
                outcome=outcome.value,
                reason=stable_reason,
                subject_revision=gate["revision"],
                requirement_version=requirement["requirement_version"],
                acceptance_criteria_version=requirement["acceptance_criteria_version"],
                acceptance_criteria_hash=requirement["acceptance_criteria_hash"],
                integration_baseline_id=selection["integration_baseline_id"],
                integration_baseline_hash=selection["integration_baseline_hash"],
                subject_head_sha=gate["subject_head_sha"],
                eligibility_snapshot=eligibility.model_dump(mode="json"),
                now=now,
            )
            decided_gate = repository.decide_delivery_gate(
                gate_id,
                expected_revision=gate["revision"],
                now=now,
            )
            if decided_gate is None:
                raise FormalReviewStale("Formal Review Gate changed while deciding")
            if outcome is DecisionOutcome.APPROVED:
                updated_requirement = repository.touch_requirement(
                    requirement_id,
                    expected_revision=expected_revision,
                    now=now,
                )
                if updated_requirement is None:
                    raise StaleRequirementRevision(requirement_id)
            else:
                updated_work_item = repository.reopen_work_item_for_rework(
                    work_item_id,
                    expected_revision=work_item["revision"],
                    formal_state=FormalDeliveryState.MR_OPEN.value,
                    formal_binding_id=str(gate["formal_merge_request_binding_id"]),
                    now=now,
                )
                if updated_work_item is None:
                    raise StaleWorkItemRevision(work_item_id)
                updated_requirement = repository.update_requirement_state(
                    requirement_id,
                    expected_revision=expected_revision,
                    state=RequirementState.IN_PROGRESS.value,
                    now=now,
                )
                if updated_requirement is None:
                    raise StaleRequirementRevision(requirement_id)
                repository.invalidate_current_delivery_evidence(
                    requirement_id,
                    reason=f"FORMAL_REVIEW_{outcome.value}",
                    now=now,
                )
                refreshed_requirement = repository.requirement_by_id(
                    requirement_id,
                    for_update=True,
                )
                refreshed_selection = repository.integration_baseline_selection_by_id(
                    str(gate["selection_id"])
                )
                refreshed_gate = repository.delivery_gate_by_id(gate_id)
                refreshed_decision = repository.delivery_decision_by_gate(gate_id)
                if (
                    refreshed_requirement is None
                    or refreshed_selection is None
                    or refreshed_gate is None
                    or refreshed_decision is None
                ):
                    raise FormalDeliveryConflict("Formal Review invalidation facts are unavailable")
                updated_requirement = refreshed_requirement
                selection = refreshed_selection
                decided_gate = refreshed_gate
                decision = refreshed_decision
            audit(
                repository,
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.formal_review.decided",
                target_type="DELIVERY_GATE",
                target_id=gate_id,
                reason=f"outcome={outcome.value}; headSha={gate['subject_head_sha']}",
            )
            result = AcceptanceDecisionResult(
                requirement=requirement_dto(updated_requirement),
                selection=_selection_dto(selection),
                gate=_gate_dto(decided_gate),
                assignment=_assignment_dto(assignment),
                decision=_decision_dto(decision),
            )
            return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_REVIEW_OPERATION,
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except (RequirementError, IdempotencyConflict) as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.formal_review.decide",
            target_type="DELIVERY_GATE",
            target_id=gate_id,
            error=error,
        )
        raise
    return AcceptanceDecisionResult.model_validate(execution.response.body)


def record_formal_merged(
    repository: RequirementRepository,
    *,
    work_item_id: str,
    binding_id: str,
    head_sha: str,
    merge_commit_sha: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    correlation_id: str,
    dependencies: RequirementDependencies,
) -> FormalMergedResult:
    stable_actor = actor_id(actor)
    stable_correlation = validated_correlation_id(correlation_id)
    if not _COMMIT_SHA.fullmatch(head_sha) or not _COMMIT_SHA.fullmatch(merge_commit_sha):
        raise InvalidRequirementInput("Formal merge commits are invalid")
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=_MERGED_OPERATION,
        method="COMMAND",
        path="requirement.record-formal-merged",
        body={
            "bindingId": binding_id,
            "expectedRevision": expected_revision,
            "headSha": head_sha,
            "mergeCommitSha": merge_commit_sha,
            "workItemId": work_item_id,
        },
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        work_item = repository.work_item_by_id(work_item_id, for_update=True)
        if work_item is None:
            raise WorkItemNotFound(work_item_id)
        if work_item["revision"] != expected_revision:
            raise StaleWorkItemRevision(work_item_id)
        requirement_id = str(work_item["requirement_id"])
        requirement = repository.requirement_by_id(requirement_id, for_update=True)
        if requirement is None:
            raise RequirementNotFound(requirement_id)
        context = repository.formal_delivery_context(work_item_id)
        if context is None:
            raise WorkItemNotFound(work_item_id)
        evidence_item = _current_evidence_item(context, dependencies)
        if (
            FormalDeliveryState(work_item["formal_delivery_state"])
            is not FormalDeliveryState.MERGE_PENDING
            or str(work_item["formal_merge_request_binding_id"]) != binding_id
            or evidence_item.task_commit_sha != head_sha
            or context["formal_review_outcome"] != DecisionOutcome.APPROVED.value
            or context["formal_review_validity"] != "CURRENT"
            or context["formal_gate_head_sha"] != head_sha
        ):
            raise FormalDeliveryConflict("Formal merge callback is stale")
        now = dependencies.clock.now()
        updated_work_item = repository.update_work_item_formal_delivery(
            work_item_id,
            expected_revision=expected_revision,
            state=WorkItemState.COMPLETED.value,
            formal_state=FormalDeliveryState.MERGED.value,
            binding_id=binding_id,
            blocked_reason=None,
            now=now,
        )
        if updated_work_item is None:
            raise StaleWorkItemRevision(work_item_id)
        all_merged = all(
            state == FormalDeliveryState.MERGED.value
            for state in repository.required_formal_delivery_states(requirement_id)
        )
        ready_invalidation_reason = _ready_formal_invalidation_reason(
            repository,
            requirement_id,
        )
        updated_requirement = repository.update_requirement_state(
            requirement_id,
            expected_revision=requirement["revision"],
            state=(
                RequirementState.IN_PROGRESS.value
                if ready_invalidation_reason is not None
                else (
                    RequirementState.COMPLETED.value
                    if all_merged
                    else RequirementState.AWAITING_MERGE.value
                )
            ),
            now=now,
        )
        if updated_requirement is None:
            raise StaleRequirementRevision(requirement_id)
        if ready_invalidation_reason is not None:
            _apply_ready_formal_invalidation(
                repository,
                requirement_id=requirement_id,
                reason=ready_invalidation_reason,
                now=now,
            )
            refreshed_work_item = repository.work_item_by_id(work_item_id, for_update=True)
            refreshed_requirement = repository.requirement_by_id(
                requirement_id,
                for_update=True,
            )
            if refreshed_work_item is None or refreshed_requirement is None:
                raise FormalDeliveryConflict(
                    "Formal Delivery invalidation projection is unavailable"
                )
            updated_work_item = refreshed_work_item
            updated_requirement = refreshed_requirement
        audit(
            repository,
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.formal_delivery.merged",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            reason=f"bindingId={binding_id}; mergeCommitSha={merge_commit_sha}",
            correlation_id=stable_correlation,
        )
        result = FormalMergedResult(
            requirement=requirement_dto(updated_requirement),
            work_item=work_item_dto(updated_work_item),
        )
        return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_MERGED_OPERATION,
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except (RequirementError, IdempotencyConflict) as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.formal_delivery.merged",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            error=error,
        )
        raise
    return FormalMergedResult.model_validate(execution.response.body)


def _formal_payload_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _formal_message(row: Any) -> FormalDeliveryRequestMessage:
    payload = row["payload"]
    if not isinstance(payload, dict):
        raise FormalDeliveryConflict("Formal Delivery message is invalid")
    expected_fields = {
        "acceptanceDecisionId",
        "actorId",
        "formalMergeRequestBindingId",
        "formalReviewDecisionId",
        "kind",
        "repositoryId",
        "requestedHeadSha",
        "requirementId",
        "requirementRevision",
        "workItemId",
        "workItemRevision",
    }
    try:
        kind = FormalDeliveryRequestKind(payload["kind"])
        message = FormalDeliveryRequestMessage(
            message_id=str(row["id"]),
            payload_hash=_formal_payload_hash(payload),
            requirement_id=payload["requirementId"],
            requirement_revision=payload["requirementRevision"],
            work_item_id=payload["workItemId"],
            work_item_revision=payload["workItemRevision"],
            repository_id=payload["repositoryId"],
            actor_id=payload["actorId"],
            acceptance_decision_id=payload["acceptanceDecisionId"],
            formal_merge_request_binding_id=payload["formalMergeRequestBindingId"],
            formal_review_decision_id=payload["formalReviewDecisionId"],
            requested_head_sha=payload["requestedHeadSha"],
            kind=kind,
            attempts=row["attempts"],
        )
    except (KeyError, TypeError, ValueError):
        raise FormalDeliveryConflict("Formal Delivery message is invalid") from None
    if (
        set(payload) != expected_fields
        or payload["requirementId"] != str(row["aggregate_id"])
        or payload["requirementRevision"] != row["aggregate_version"]
        or not _COMMIT_SHA.fullmatch(message.requested_head_sha)
        or (
            kind is FormalDeliveryRequestKind.CREATE_MR
            and (message.formal_review_decision_id is not None or row["topic"] != _CREATE_TOPIC)
        )
        or (
            kind is FormalDeliveryRequestKind.MERGE_MR
            and (
                message.formal_merge_request_binding_id is None
                or message.formal_review_decision_id is None
                or row["topic"] != _MERGE_TOPIC
            )
        )
    ):
        raise FormalDeliveryConflict("Formal Delivery message is invalid")
    return message


def claim_formal_delivery_requests(
    repository: RequirementRepository,
    *,
    limit: int,
    available_before: Any,
    lease_until: Any,
) -> tuple[FormalDeliveryRequestMessage, ...]:
    if not 1 <= limit <= 100 or lease_until <= available_before:
        raise InvalidRequirementInput("Formal Delivery lease is invalid")
    return tuple(
        _formal_message(row)
        for row in repository.claim_formal_delivery_requests(
            limit=limit,
            available_before=available_before,
            lease_until=lease_until,
        )
    )


def acknowledge_formal_delivery_request(
    repository: RequirementRepository,
    *,
    message_id: str,
    dependencies: RequirementDependencies,
) -> None:
    row = repository.outbox_by_id(message_id, for_update=True)
    if row is None or row["topic"] not in {_CREATE_TOPIC, _MERGE_TOPIC}:
        raise FormalDeliveryConflict("Formal Delivery request is unavailable")
    _formal_message(row)
    if (
        row["state"] != "PUBLISHED"
        and repository.publish_outbox(
            message_id,
            now=dependencies.clock.now(),
        )
        is None
    ):
        raise FormalDeliveryConflict("Formal Delivery request is unavailable")


def release_formal_delivery_request(
    repository: RequirementRepository,
    *,
    message_id: str,
    error_code: str,
    available_at: Any,
) -> None:
    if error_code not in {
        "FORMAL_DELIVERY_CONFLICT",
        "FORMAL_DELIVERY_INVALID",
        "SOURCE_CONTROL_UNAVAILABLE",
    }:
        raise InvalidRequirementInput("Formal Delivery release code is invalid")
    row = repository.outbox_by_id(message_id, for_update=True)
    if row is None or row["topic"] not in {_CREATE_TOPIC, _MERGE_TOPIC}:
        raise FormalDeliveryConflict("Formal Delivery request is unavailable")
    _formal_message(row)
    if (
        row["state"] != "PUBLISHED"
        and repository.release_outbox(
            message_id,
            error_code=error_code,
            available_at=available_at,
        )
        is None
    ):
        raise FormalDeliveryConflict("Formal Delivery request is unavailable")
