"""Ordinary pre-decision delegation; no administrator recovery override."""

import json
from typing import Any

from control_plane.app.modules.requirement.application.acceptance import (
    _assignment_dto,
    _audit_denial,
    _gate_dto,
    _require_current_selected_evidence,
)
from control_plane.app.modules.requirement.application.common import actor_id, audit
from control_plane.app.modules.requirement.application.dependencies import RequirementDependencies
from control_plane.app.modules.requirement.application.formal import _current_evidence_item
from control_plane.app.modules.requirement.domain import (
    AcceptanceStale,
    GateAlreadyDecided,
    GateNotFound,
    GateReviewerIneligible,
    GateReviewerMismatch,
    InvalidRequirementInput,
    RequirementDependencyUnavailable,
    RequirementError,
    RequirementNotFound,
)
from control_plane.app.modules.requirement.domain.acceptance import DeliveryGateReassignmentResult
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.modules.requirement.ports.runtime import frozen_gate_capabilities
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)

OPERATION = "requirement_reassign_delivery_gate"


def reassign_delivery_gate(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    gate_id: str,
    candidate_id: str,
    expected_gate_revision: int,
    reason: str,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> DeliveryGateReassignmentResult:
    stable_actor = actor_id(actor)
    if not candidate_id.strip() or not reason.strip():
        raise InvalidRequirementInput("Candidate and reassignment reason are required")
    material = dependencies.secret_manager.load()
    fingerprint = canonical_request_fingerprint(
        operation=OPERATION,
        method="POST",
        path=f"requirement/{requirement_id}/delivery-gates/{gate_id}/reassign",
        body={
            "candidateId": candidate_id,
            "reason": reason,
            "expectedGateRevision": expected_gate_revision,
        },
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    try:
        requirement = repository.requirement_by_id(requirement_id, for_update=True)
        if requirement is None:
            raise RequirementNotFound(requirement_id)

        def command() -> IdempotentResponse:
            gate = repository.delivery_gate_by_id(gate_id, for_update=True)
            if gate is None or str(gate["requirement_id"]) != requirement_id:
                raise GateNotFound(gate_id)
            if gate["state"] != "OPEN" or repository.delivery_decision_by_gate(gate_id) is not None:
                raise GateAlreadyDecided(gate_id)
            if gate["revision"] != expected_gate_revision:
                raise AcceptanceStale("Delivery Gate revision changed")
            selection = repository.integration_baseline_selection_by_id(str(gate["selection_id"]))
            if (
                selection is None
                or selection["invalidated_at"] is not None
                or str(requirement["current_integration_baseline_selection_id"])
                != str(gate["selection_id"])
                or gate["requirement_version"] != requirement["requirement_version"]
                or gate["acceptance_criteria_version"] != requirement["acceptance_criteria_version"]
                or gate["acceptance_criteria_hash"] != requirement["acceptance_criteria_hash"]
                or gate["integration_baseline_id"] != selection["integration_baseline_id"]
                or gate["integration_baseline_hash"] != selection["integration_baseline_hash"]
            ):
                raise AcceptanceStale("Delivery Gate subject changed")
            _require_current_selected_evidence(selection, dependencies=dependencies)
            if gate["gate_type"] == "REQUIREMENT_ACCEPTANCE":
                if (
                    requirement["state"] != "AWAITING_ACCEPTANCE"
                    or str(requirement["current_acceptance_gate_id"]) != gate_id
                ):
                    raise AcceptanceStale("Acceptance Gate is not current")
            elif gate["gate_type"] == "FORMAL_MR_REVIEW":
                work_item_id = str(gate["work_item_id"])
                repository.work_item_by_id(work_item_id, for_update=True)
                context = repository.formal_delivery_context(work_item_id)
                if (
                    context is None
                    or context["formal_delivery_state"] != "MR_OPEN"
                    or str(context["formal_merge_request_binding_id"])
                    != str(gate["formal_merge_request_binding_id"])
                    or _current_evidence_item(context, dependencies).task_commit_sha
                    != gate["subject_head_sha"]
                ):
                    raise AcceptanceStale("Formal Review Gate is not current")
            else:
                raise GateNotFound(gate_id)
            assignment = repository.current_delivery_gate_assignment(gate_id, for_update=True)
            if assignment is None or assignment["default_reviewer_id"] != stable_actor:
                raise GateReviewerMismatch(stable_actor)
            guard = dependencies.delivery_reviewer_guard
            if guard is None:
                raise RequirementDependencyUnavailable("Delivery qualification unavailable")
            try:
                capabilities = frozen_gate_capabilities(
                    dict(assignment["resolution_snapshot"]),
                    gate["gate_type"],
                    expected_version=gate["policy_version"],
                    expected_hash=gate["policy_snapshot_hash"],
                )
                actor_facts = guard.evaluate(
                    actor_id=stable_actor,
                    workspace_id=str(requirement["workspace_id"]),
                    required_capabilities=("requirement.delivery_gate.assign",),
                )
                candidate_facts = guard.evaluate(
                    actor_id=candidate_id,
                    workspace_id=str(requirement["workspace_id"]),
                    required_capabilities=capabilities,
                )
            except Exception as error:
                raise RequirementDependencyUnavailable(
                    "Delivery qualification failed closed"
                ) from error
            for subject, caps, facts in (
                (stable_actor, ("requirement.delivery_gate.assign",), actor_facts),
                (candidate_id, capabilities, candidate_facts),
            ):
                if (
                    not facts.eligible
                    or facts.actor_id != subject
                    or facts.workspace_id != str(requirement["workspace_id"])
                    or facts.required_capabilities != caps
                ):
                    raise GateReviewerIneligible(subject)
            now = dependencies.clock.now()
            updated_gate = repository.advance_delivery_gate_assignment(
                gate_id, expected_revision=expected_gate_revision
            )
            if updated_gate is None or not repository.supersede_delivery_gate_assignment(
                str(assignment["id"]), now=now
            ):
                raise AcceptanceStale("Concurrent delivery reassignment")
            resolution = {
                **dict(assignment["resolution_snapshot"]),
                "reassignment": {
                    "actorId": stable_actor,
                    "reason": reason,
                    "previousAssignmentId": str(assignment["id"]),
                    "actorQualification": actor_facts.model_dump(mode="json"),
                    "candidateQualification": candidate_facts.model_dump(mode="json"),
                },
            }
            assigned = repository.insert_delivery_gate_assignment(
                id=str(dependencies.random.uuid4()),
                gate_id=gate_id,
                default_reviewer_id=assignment["default_reviewer_id"],
                current_reviewer_id=candidate_id,
                resolution_snapshot=resolution,
                revision=assignment["revision"] + 1,
                now=now,
            )
            audit(
                repository,
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.delivery_gate.reassigned",
                target_type="DELIVERY_GATE",
                target_id=gate_id,
                reason=json.dumps(
                    {
                        "reason": reason,
                        "candidateId": candidate_id,
                        "previousAssignmentId": str(assignment["id"]),
                        "assignmentId": str(assigned["id"]),
                        "policyVersion": gate["policy_version"],
                        "policyHash": gate["policy_snapshot_hash"],
                        "actorQualificationHash": actor_facts.snapshot_hash,
                        "candidateQualificationHash": candidate_facts.snapshot_hash,
                    },
                    sort_keys=True,
                ),
            )
            result = DeliveryGateReassignmentResult(
                gate=_gate_dto(updated_gate), assignment=_assignment_dto(assigned)
            )
            return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=OPERATION,
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
            action="requirement.delivery_gate.reassign",
            target_type="DELIVERY_GATE",
            target_id=gate_id,
            error=error,
        )
        raise
    return DeliveryGateReassignmentResult.model_validate(execution.response.body)
