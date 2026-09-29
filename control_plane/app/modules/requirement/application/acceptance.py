import re
from typing import Any

from control_plane.app.modules.audit import AuditEnvelope, record
from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    requirement_dto,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.application.evidence import _validated_artifacts
from control_plane.app.modules.requirement.domain import (
    AcceptanceConfirmationResult,
    AcceptanceDecisionResult,
    AcceptanceStale,
    CurrentAcceptanceProof,
    DecisionOutcome,
    DecisionValidity,
    DeliveryDecisionDto,
    DeliveryGateAssignmentDto,
    DeliveryGateDto,
    DeliveryGateState,
    DeliveryGateType,
    EvidenceUnavailableOrStale,
    GateAlreadyDecided,
    GateNotFound,
    GateReviewerIneligible,
    GateReviewerMismatch,
    IntegrationBaselineSelectionDto,
    InvalidRequirementInput,
    RequirementDependencyUnavailable,
    RequirementError,
    RequirementNotFound,
    RequirementState,
    SelectIntegrationBaselineResult,
    SelectionStale,
    StaleRequirementRevision,
)
from control_plane.app.modules.requirement.ports import (
    DeliveryGatePolicySnapshot,
    IntegrationBaselineEvidenceSnapshot,
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
_SELECTION_OPERATION = "requirement_select_integration_baseline"
_SELECTION_TOPIC = "requirement.integration-baseline.selected"
_CONFIRM_OPERATION = "requirement_confirm_acceptance"
_DECIDE_OPERATION = "requirement_decide_acceptance"


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
            actor_type="HUMAN",
            action=f"{action}_denied",
            target_type=target_type,
            target_id=target_id,
            result="DENIED",
            reason=f"reasonCode={type(error).__name__.upper()}",
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.denial_audit,
    )


def _selection_dto(row: Any) -> IntegrationBaselineSelectionDto:
    return IntegrationBaselineSelectionDto(
        id=str(row["id"]),
        requirement_id=str(row["requirement_id"]),
        delivery_snapshot_id=str(row["delivery_snapshot_id"]),
        integration_baseline_id=str(row["integration_baseline_id"]),
        integration_baseline_hash=row["integration_baseline_hash"],
        evidence_requirement_version=row["evidence_requirement_version"],
        evidence_required_work_item_set_version=row["evidence_required_work_item_set_version"],
        evidence_required_work_item_set_hash=row["evidence_required_work_item_set_hash"],
        requirement_version_before=row["requirement_version_before"],
        requirement_version_after=row["requirement_version_after"],
        selected_by=row["selected_by"],
        selected_at=row["selected_at"],
        invalidated_at=row["invalidated_at"],
        invalidation_reason=row["invalidation_reason"],
    )


def _gate_dto(row: Any) -> DeliveryGateDto:
    return DeliveryGateDto(
        id=str(row["id"]),
        gate_type=DeliveryGateType(row["gate_type"]),
        requirement_id=str(row["requirement_id"]),
        work_item_id=None if row["work_item_id"] is None else str(row["work_item_id"]),
        selection_id=str(row["selection_id"]),
        requirement_version=row["requirement_version"],
        acceptance_criteria_version=row["acceptance_criteria_version"],
        acceptance_criteria_hash=row["acceptance_criteria_hash"],
        integration_baseline_id=str(row["integration_baseline_id"]),
        integration_baseline_hash=row["integration_baseline_hash"],
        formal_merge_request_binding_id=(
            None
            if row["formal_merge_request_binding_id"] is None
            else str(row["formal_merge_request_binding_id"])
        ),
        subject_head_sha=row["subject_head_sha"],
        policy_code=row["policy_code"],
        policy_version=row["policy_version"],
        policy_snapshot_hash=row["policy_snapshot_hash"],
        state=DeliveryGateState(row["state"]),
        revision=row["revision"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        invalidated_at=row["invalidated_at"],
        invalidation_reason=row["invalidation_reason"],
    )


def _assignment_dto(row: Any) -> DeliveryGateAssignmentDto:
    return DeliveryGateAssignmentDto(
        id=str(row["id"]),
        gate_id=str(row["gate_id"]),
        default_reviewer_id=row["default_reviewer_id"],
        current_reviewer_id=row["current_reviewer_id"],
        resolution_snapshot=dict(row["resolution_snapshot"]),
        revision=row["revision"],
        assigned_at=row["assigned_at"],
        superseded_at=row["superseded_at"],
    )


def _decision_dto(row: Any) -> DeliveryDecisionDto:
    return DeliveryDecisionDto(
        id=str(row["id"]),
        gate_id=str(row["gate_id"]),
        gate_assignment_id=str(row["gate_assignment_id"]),
        reviewer_id=row["reviewer_id"],
        outcome=DecisionOutcome(row["outcome"]),
        reason=row["reason"],
        subject_revision=row["subject_revision"],
        requirement_version=row["requirement_version"],
        acceptance_criteria_version=row["acceptance_criteria_version"],
        acceptance_criteria_hash=row["acceptance_criteria_hash"],
        integration_baseline_id=str(row["integration_baseline_id"]),
        integration_baseline_hash=row["integration_baseline_hash"],
        subject_head_sha=row["subject_head_sha"],
        eligibility_snapshot=dict(row["eligibility_snapshot"]),
        validity=DecisionValidity(row["validity"]),
        decided_at=row["decided_at"],
        invalidated_at=row["invalidated_at"],
        invalidation_reason=row["invalidation_reason"],
    )


def _read_evidence(
    integration_baseline_id: str,
    *,
    dependencies: RequirementDependencies,
) -> IntegrationBaselineEvidenceSnapshot:
    reader = dependencies.integration_evidence
    if reader is None:
        raise RequirementDependencyUnavailable("Integration Baseline Evidence is unavailable")
    try:
        return reader.get(integration_baseline_id)
    except RequirementError:
        raise
    except Exception as error:
        raise EvidenceUnavailableOrStale(
            "Integration Baseline Evidence lookup failed closed"
        ) from error


def _require_current_selected_evidence(
    selection: Any,
    *,
    dependencies: RequirementDependencies,
) -> IntegrationBaselineEvidenceSnapshot:
    evidence = _read_evidence(
        str(selection["integration_baseline_id"]),
        dependencies=dependencies,
    )
    if evidence.currentness_state == "UNAVAILABLE":
        raise EvidenceUnavailableOrStale(
            "Integration Baseline Evidence currentness proof is unavailable"
        )
    if evidence.currentness_state != "CURRENT":
        raise EvidenceUnavailableOrStale("Integration Baseline Evidence is stale")
    if (
        evidence.id != str(selection["integration_baseline_id"])
        or evidence.evidence_hash != selection["integration_baseline_hash"]
    ):
        raise AcceptanceStale("Selected Evidence identity is stale")
    return evidence


def _validate_evidence_artifacts(
    requirement_id: str,
    evidence: IntegrationBaselineEvidenceSnapshot,
    dependencies: RequirementDependencies,
) -> None:
    for item in evidence.work_items:
        if not item.artifact_references:
            raise EvidenceUnavailableOrStale("Evidence Artifact references are unavailable")
        _validated_artifacts(
            requirement_id=requirement_id,
            references=item.artifact_references,
            dependencies=dependencies,
        )


def _validate_selection_subject(
    repository: RequirementRepository,
    *,
    requirement: Any,
    delivery_snapshot_id: str,
    integration_baseline_id: str,
    expected_requirement_version: int,
    dependencies: RequirementDependencies,
) -> tuple[Any, IntegrationBaselineEvidenceSnapshot]:
    if RequirementState(requirement["state"]) is not RequirementState.VERIFYING:
        raise SelectionStale("Requirement must be VERIFYING")
    if requirement["requirement_version"] != expected_requirement_version:
        raise SelectionStale("Requirement Version is stale")
    snapshot = repository.delivery_snapshot_by_id(delivery_snapshot_id)
    if snapshot is None or str(snapshot["requirement_id"]) != str(requirement["id"]):
        raise SelectionStale("delivery snapshot is unavailable")
    if (
        snapshot["requirement_version"] != expected_requirement_version
        or snapshot["required_work_item_set_version"]
        != requirement["required_work_item_set_version"]
        or snapshot["required_work_item_set_hash"] != requirement["required_work_item_set_hash"]
    ):
        raise SelectionStale("delivery snapshot is stale")
    evidence = _read_evidence(integration_baseline_id, dependencies=dependencies)
    if evidence.currentness_state == "UNAVAILABLE":
        raise EvidenceUnavailableOrStale(
            "Integration Baseline Evidence currentness proof is unavailable"
        )
    if evidence.currentness_state != "CURRENT":
        raise EvidenceUnavailableOrStale("Integration Baseline Evidence is stale")
    snapshot_work_items = tuple(str(value) for value in snapshot["work_item_ids"])
    evidence_work_items = tuple(item.work_item_id for item in evidence.work_items)
    if (
        evidence.id != integration_baseline_id
        or evidence.requirement_id != str(requirement["id"])
        or evidence.delivery_snapshot_id != delivery_snapshot_id
        or evidence.delivery_snapshot_hash != snapshot["snapshot_hash"]
        or evidence.requirement_version != snapshot["requirement_version"]
        or evidence.required_work_item_set_version != snapshot["required_work_item_set_version"]
        or evidence.required_work_item_set_hash != snapshot["required_work_item_set_hash"]
        or evidence_work_items != snapshot_work_items
        or len(set(evidence_work_items)) != len(evidence_work_items)
    ):
        raise SelectionStale("Evidence does not match the delivery snapshot")
    current_work_items = repository.work_items(str(requirement["id"]))
    repositories = {str(item["id"]): str(item["repository_id"]) for item in current_work_items}
    if set(repositories) != set(evidence_work_items) or any(
        repositories.get(item.work_item_id) != item.repository_id for item in evidence.work_items
    ):
        raise SelectionStale("Evidence repository binding is stale")
    _validate_evidence_artifacts(str(requirement["id"]), evidence, dependencies)
    return snapshot, evidence


def select_integration_baseline(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    delivery_snapshot_id: str,
    integration_baseline_id: str,
    expected_revision: int,
    expected_requirement_version: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> SelectIntegrationBaselineResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "deliverySnapshotId": delivery_snapshot_id,
        "expectedRequirementVersion": expected_requirement_version,
        "expectedRevision": expected_revision,
        "integrationBaselineId": integration_baseline_id,
        "requirementId": requirement_id,
    }
    fingerprint = canonical_request_fingerprint(
        operation=_SELECTION_OPERATION,
        method="COMMAND",
        path="requirement.select-integration-baseline",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    try:
        requirement = repository.requirement_by_id(requirement_id, for_update=True)
        if requirement is None:
            raise RequirementNotFound(requirement_id)
        existing = repository.idempotency_by_scope(
            stable_actor,
            _SELECTION_OPERATION,
            idempotency_key,
        )
        if existing is None and requirement["revision"] != expected_revision:
            raise StaleRequirementRevision(requirement_id)

        def command() -> IdempotentResponse:
            snapshot, evidence = _validate_selection_subject(
                repository,
                requirement=requirement,
                delivery_snapshot_id=delivery_snapshot_id,
                integration_baseline_id=integration_baseline_id,
                expected_requirement_version=expected_requirement_version,
                dependencies=dependencies,
            )
            now = dependencies.clock.now()
            selection_id = str(dependencies.random.uuid4())
            selection = repository.insert_integration_baseline_selection(
                id=selection_id,
                requirement_id=requirement_id,
                delivery_snapshot_id=delivery_snapshot_id,
                delivery_snapshot_hash=snapshot["snapshot_hash"],
                integration_baseline_id=evidence.id,
                integration_baseline_hash=evidence.evidence_hash,
                evidence_requirement_version=evidence.requirement_version,
                evidence_required_work_item_set_version=(evidence.required_work_item_set_version),
                evidence_required_work_item_set_hash=evidence.required_work_item_set_hash,
                requirement_version_before=expected_requirement_version,
                requirement_version_after=expected_requirement_version + 1,
                selected_by=stable_actor,
                now=now,
            )
            updated = repository.apply_integration_baseline_selection(
                requirement_id,
                selection_id=selection_id,
                expected_revision=expected_revision,
                expected_requirement_version=expected_requirement_version,
                now=now,
            )
            if updated is None:
                raise SelectionStale("Requirement changed while selecting Evidence")
            repository.insert_outbox(
                id=str(dependencies.random.uuid4()),
                topic=_SELECTION_TOPIC,
                aggregate_type="REQUIREMENT",
                aggregate_id=requirement_id,
                aggregate_version=updated["revision"],
                payload={
                    "integrationBaselineHash": evidence.evidence_hash,
                    "integrationBaselineId": evidence.id,
                    "requirementId": requirement_id,
                    "requirementVersion": updated["requirement_version"],
                    "selectionId": selection_id,
                },
                now=now,
            )
            audit(
                repository,
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.integration_baseline.selected",
                target_type="INTEGRATION_BASELINE_SELECTION",
                target_id=selection_id,
                reason=(
                    f"integrationBaselineId={evidence.id}; "
                    f"requirementVersion={updated['requirement_version']}"
                ),
            )
            result = SelectIntegrationBaselineResult(
                requirement=requirement_dto(updated),
                selection=_selection_dto(selection),
                outbox_topic=_SELECTION_TOPIC,
            )
            return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_SELECTION_OPERATION,
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
            action="requirement.integration_baseline.select",
            target_type="REQUIREMENT",
            target_id=requirement_id,
            error=error,
        )
        raise
    return SelectIntegrationBaselineResult.model_validate(execution.response.body)


def _validated_delivery_policy(
    requirement: Any,
    dependencies: RequirementDependencies,
) -> DeliveryGatePolicySnapshot:
    policies = dependencies.delivery_gate_policies
    if policies is None:
        raise RequirementDependencyUnavailable("Acceptance policy is unavailable")
    try:
        policy = policies.requirement_acceptance(
            workspace_id=str(requirement["workspace_id"]),
            requirement_created_by=requirement["created_by"],
        )
    except Exception as error:
        raise RequirementDependencyUnavailable("Acceptance policy failed closed") from error
    if (
        policy.default_reviewer_id != requirement["created_by"]
        or not policy.policy_code.strip()
        or not _SHA256.fullmatch(policy.snapshot_hash)
    ):
        raise RequirementDependencyUnavailable("Acceptance policy is invalid")
    return policy


def confirm_requirement_acceptance(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    selection_id: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> AcceptanceConfirmationResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=_CONFIRM_OPERATION,
        method="COMMAND",
        path="requirement.confirm-acceptance",
        body={
            "expectedRevision": expected_revision,
            "requirementId": requirement_id,
            "selectionId": selection_id,
        },
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    try:
        requirement = repository.requirement_by_id(requirement_id, for_update=True)
        if requirement is None:
            raise RequirementNotFound(requirement_id)
        existing = repository.idempotency_by_scope(
            stable_actor,
            _CONFIRM_OPERATION,
            idempotency_key,
        )
        if existing is None and requirement["revision"] != expected_revision:
            raise StaleRequirementRevision(requirement_id)

        def command() -> IdempotentResponse:
            if (
                RequirementState(requirement["state"]) is not RequirementState.AWAITING_ACCEPTANCE
                or str(requirement["current_integration_baseline_selection_id"]) != selection_id
                or requirement["current_acceptance_gate_id"] is not None
            ):
                raise AcceptanceStale("Selection is not awaiting Acceptance")
            selection = repository.integration_baseline_selection_by_id(selection_id)
            if (
                selection is None
                or str(selection["requirement_id"]) != requirement_id
                or selection["invalidated_at"] is not None
                or selection["requirement_version_after"] != requirement["requirement_version"]
            ):
                raise AcceptanceStale("Selection is stale")
            _require_current_selected_evidence(selection, dependencies=dependencies)
            policy = _validated_delivery_policy(requirement, dependencies)
            now = dependencies.clock.now()
            gate_id = str(dependencies.random.uuid4())
            gate = repository.insert_delivery_gate(
                id=gate_id,
                gate_type=DeliveryGateType.REQUIREMENT_ACCEPTANCE.value,
                requirement_id=requirement_id,
                work_item_id=None,
                selection_id=selection_id,
                requirement_version=requirement["requirement_version"],
                acceptance_criteria_version=requirement["acceptance_criteria_version"],
                acceptance_criteria_hash=requirement["acceptance_criteria_hash"],
                integration_baseline_id=selection["integration_baseline_id"],
                integration_baseline_hash=selection["integration_baseline_hash"],
                formal_merge_request_binding_id=None,
                subject_head_sha=None,
                policy_code=policy.policy_code,
                policy_version=policy.version,
                policy_snapshot_hash=policy.snapshot_hash,
                now=now,
            )
            assignment = repository.insert_delivery_gate_assignment(
                id=str(dependencies.random.uuid4()),
                gate_id=gate_id,
                default_reviewer_id=policy.default_reviewer_id,
                current_reviewer_id=policy.default_reviewer_id,
                resolution_snapshot=policy.resolution_snapshot,
                now=now,
            )
            updated = repository.set_current_acceptance_gate(
                requirement_id,
                selection_id=selection_id,
                gate_id=gate_id,
                expected_revision=expected_revision,
                now=now,
            )
            if updated is None:
                raise AcceptanceStale("Requirement changed while opening Acceptance")
            audit(
                repository,
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.acceptance.opened",
                target_type="DELIVERY_GATE",
                target_id=gate_id,
                reason=(f"selectionId={selection_id}; reviewerId={policy.default_reviewer_id}"),
            )
            result = AcceptanceConfirmationResult(
                requirement=requirement_dto(updated),
                selection=_selection_dto(selection),
                gate=_gate_dto(gate),
                assignment=_assignment_dto(assignment),
            )
            return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_CONFIRM_OPERATION,
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
            action="requirement.acceptance.open",
            target_type="REQUIREMENT",
            target_id=requirement_id,
            error=error,
        )
        raise
    return AcceptanceConfirmationResult.model_validate(execution.response.body)


def decide_requirement_acceptance(
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
        raise InvalidRequirementInput("decision reason is required")
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=_DECIDE_OPERATION,
        method="COMMAND",
        path="requirement.decide-acceptance",
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
            _DECIDE_OPERATION,
            idempotency_key,
        )
        if existing is None and requirement["revision"] != expected_revision:
            raise StaleRequirementRevision(requirement_id)

        def command() -> IdempotentResponse:
            if (
                RequirementState(requirement["state"]) is not RequirementState.AWAITING_ACCEPTANCE
                or str(requirement["current_acceptance_gate_id"]) != gate_id
            ):
                raise AcceptanceStale("Acceptance Gate is not current")
            gate = repository.delivery_gate_by_id(gate_id, for_update=True)
            if gate is None or str(gate["requirement_id"]) != requirement_id:
                raise GateNotFound(gate_id)
            if gate["state"] != DeliveryGateState.OPEN.value:
                raise GateAlreadyDecided(gate_id)
            selection_id = str(requirement["current_integration_baseline_selection_id"])
            selection = repository.integration_baseline_selection_by_id(selection_id)
            if (
                selection is None
                or selection["invalidated_at"] is not None
                or str(gate["selection_id"]) != selection_id
                or gate["requirement_version"] != requirement["requirement_version"]
                or gate["acceptance_criteria_version"] != requirement["acceptance_criteria_version"]
                or gate["acceptance_criteria_hash"] != requirement["acceptance_criteria_hash"]
                or gate["integration_baseline_id"] != selection["integration_baseline_id"]
                or gate["integration_baseline_hash"] != selection["integration_baseline_hash"]
            ):
                raise AcceptanceStale("Acceptance subject is stale")
            _require_current_selected_evidence(selection, dependencies=dependencies)
            assignment = repository.current_delivery_gate_assignment(
                gate_id,
                for_update=True,
            )
            if assignment is None:
                raise AcceptanceStale("Acceptance assignment is unavailable")
            if assignment["current_reviewer_id"] != stable_actor:
                raise GateReviewerMismatch(stable_actor)
            guard = dependencies.delivery_reviewer_guard
            if guard is None:
                raise RequirementDependencyUnavailable("Acceptance eligibility unavailable")
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
                    "Acceptance eligibility failed closed"
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
                subject_head_sha=None,
                eligibility_snapshot=eligibility.model_dump(mode="json"),
                now=now,
            )
            decided_gate = repository.decide_delivery_gate(
                gate_id,
                expected_revision=gate["revision"],
                now=now,
            )
            if decided_gate is None:
                raise GateAlreadyDecided(gate_id)
            target_state = (
                RequirementState.AWAITING_MERGE
                if outcome is DecisionOutcome.APPROVED
                else RequirementState.IN_PROGRESS
            )
            updated = repository.apply_acceptance_decision(
                requirement_id,
                gate_id=gate_id,
                expected_revision=expected_revision,
                state=target_state.value,
                now=now,
            )
            if updated is None:
                raise AcceptanceStale("Requirement changed while deciding Acceptance")
            if outcome is not DecisionOutcome.APPROVED:
                invalidation_reason = f"ACCEPTANCE_{outcome.value}"
                repository.invalidate_current_delivery_evidence(
                    requirement_id,
                    reason=invalidation_reason,
                    now=now,
                )
                reopened = repository.reopen_integrated_work_items_for_rework(
                    requirement_id,
                    now=now,
                )
                if not reopened:
                    raise AcceptanceStale("Acceptance rework subjects are unavailable")
                refreshed_requirement = repository.requirement_by_id(
                    requirement_id,
                    for_update=True,
                )
                refreshed_selection = repository.integration_baseline_selection_by_id(selection_id)
                refreshed_gate = repository.delivery_gate_by_id(gate_id)
                refreshed_decision = repository.delivery_decision_by_gate(gate_id)
                if (
                    refreshed_requirement is None
                    or refreshed_selection is None
                    or refreshed_gate is None
                    or refreshed_decision is None
                ):
                    raise AcceptanceStale("Acceptance invalidation facts are unavailable")
                updated = refreshed_requirement
                selection = refreshed_selection
                decided_gate = refreshed_gate
                decision = refreshed_decision
            audit(
                repository,
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.acceptance.decided",
                target_type="DELIVERY_GATE",
                target_id=gate_id,
                reason=f"outcome={outcome.value}; selectionId={selection_id}",
            )
            result = AcceptanceDecisionResult(
                requirement=requirement_dto(updated),
                selection=_selection_dto(selection),
                gate=_gate_dto(decided_gate),
                assignment=_assignment_dto(assignment),
                decision=_decision_dto(decision),
            )
            return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_DECIDE_OPERATION,
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
            action="requirement.acceptance.decide",
            target_type="DELIVERY_GATE",
            target_id=gate_id,
            error=error,
        )
        raise
    return AcceptanceDecisionResult.model_validate(execution.response.body)


def get_current_acceptance_proof(
    repository: RequirementRepository,
    *,
    requirement_id: str,
) -> CurrentAcceptanceProof:
    requirement = repository.requirement_by_id(requirement_id)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    selection_id = requirement["current_integration_baseline_selection_id"]
    gate_id = requirement["current_acceptance_gate_id"]
    if selection_id is None or gate_id is None:
        return CurrentAcceptanceProof(
            requirement_id=requirement_id,
            current=False,
            requirement_version=requirement["requirement_version"],
        )
    selection = repository.integration_baseline_selection_by_id(str(selection_id))
    gate = repository.delivery_gate_by_id(str(gate_id))
    decision = repository.delivery_decision_by_gate(str(gate_id))
    current = bool(
        selection is not None
        and gate is not None
        and decision is not None
        and selection["invalidated_at"] is None
        and gate["state"] == DeliveryGateState.DECIDED.value
        and gate["invalidated_at"] is None
        and decision["validity"] == DecisionValidity.CURRENT.value
        and decision["outcome"] == DecisionOutcome.APPROVED.value
        and str(gate["selection_id"]) == str(selection_id)
        and gate["requirement_version"] == requirement["requirement_version"]
        and gate["acceptance_criteria_version"] == requirement["acceptance_criteria_version"]
        and gate["acceptance_criteria_hash"] == requirement["acceptance_criteria_hash"]
        and gate["integration_baseline_id"] == selection["integration_baseline_id"]
        and gate["integration_baseline_hash"] == selection["integration_baseline_hash"]
    )
    return CurrentAcceptanceProof(
        requirement_id=requirement_id,
        current=current,
        requirement_version=requirement["requirement_version"],
        selection_id=str(selection_id),
        gate_id=str(gate_id),
        decision_id=None if decision is None else str(decision["id"]),
        integration_baseline_id=(
            None if selection is None else str(selection["integration_baseline_id"])
        ),
        integration_baseline_hash=(
            None if selection is None else selection["integration_baseline_hash"]
        ),
        outcome=None if decision is None else DecisionOutcome(decision["outcome"]),
    )
