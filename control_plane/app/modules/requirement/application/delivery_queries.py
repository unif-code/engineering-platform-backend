import base64
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from control_plane.app.modules.requirement.application.acceptance import (
    _assignment_dto,
    _decision_dto,
    _gate_dto,
    _selection_dto,
    _validate_evidence_artifacts,
)
from control_plane.app.modules.requirement.application.common import (
    requirement_dto,
    work_item_dto,
)
from control_plane.app.modules.requirement.application.dependencies import RequirementDependencies
from control_plane.app.modules.requirement.domain import (
    CurrentDeliveryGateProjection,
    DeliveryHistoryFactType,
    DeliveryHistoryItem,
    DeliveryHistoryPage,
    EvidenceUnavailableOrStale,
    InvalidRequirementCursor,
    RequirementDeliveryProjection,
    RequirementDeliverySnapshot,
    RequirementDependencyUnavailable,
    RequirementNotFound,
    WorkItemFormalDeliveryProjection,
)
from control_plane.app.modules.requirement.domain.delivery_projection import DeliveryHistoryFact
from control_plane.app.modules.requirement.domain.evidence import canonical_delivery_snapshot_hash
from control_plane.app.modules.requirement.ports import (
    IntegrationBaselineEvidenceSnapshot,
    RequirementRepository,
)


def get_delivery_snapshot_evidence(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    snapshot_id: str,
    dependencies: RequirementDependencies,
) -> IntegrationBaselineEvidenceSnapshot:
    snapshot = repository.delivery_snapshot_by_id(snapshot_id)
    if snapshot is None or str(snapshot["requirement_id"]) != requirement_id:
        raise RequirementNotFound("delivery snapshot")
    expected_hash = canonical_delivery_snapshot_hash(
        requirement_id=requirement_id,
        requirement_version=snapshot["requirement_version"],
        required_work_item_set_version=snapshot["required_work_item_set_version"],
        required_work_item_set_hash=snapshot["required_work_item_set_hash"],
        work_item_ids=tuple(str(item) for item in snapshot["work_item_ids"]),
    )
    if expected_hash != snapshot["snapshot_hash"]:
        raise EvidenceUnavailableOrStale("stored delivery snapshot hash mismatch")
    reader = dependencies.integration_evidence
    if reader is None:
        raise RequirementDependencyUnavailable("Integration Baseline Evidence is unavailable")
    try:
        evidence = reader.get_by_snapshot(
            delivery_snapshot_id=snapshot_id, delivery_snapshot_hash=expected_hash
        )
    except Exception as error:
        raise EvidenceUnavailableOrStale(
            "Integration Baseline Evidence lookup failed closed"
        ) from error
    if (
        evidence.requirement_id != requirement_id
        or evidence.delivery_snapshot_id != snapshot_id
        or evidence.delivery_snapshot_hash != expected_hash
        or evidence.requirement_version != snapshot["requirement_version"]
        or evidence.required_work_item_set_version != snapshot["required_work_item_set_version"]
        or evidence.required_work_item_set_hash != snapshot["required_work_item_set_hash"]
        or tuple(item.work_item_id for item in evidence.work_items)
        != tuple(str(item) for item in snapshot["work_item_ids"])
    ):
        raise EvidenceUnavailableOrStale("Evidence does not match the delivery snapshot")
    current = repository.requirement_by_id(requirement_id)
    if current is None:
        raise RequirementNotFound(requirement_id)
    version_matches = current["requirement_version"] == snapshot["requirement_version"]
    selection_id = current["current_integration_baseline_selection_id"]
    if not version_matches and selection_id is not None:
        selection = repository.integration_baseline_selection_by_id(str(selection_id))
        version_matches = (
            selection is not None
            and selection["invalidated_at"] is None
            and str(selection["delivery_snapshot_id"]) == snapshot_id
            and str(selection["integration_baseline_id"]) == evidence.id
            and selection["integration_baseline_hash"] == evidence.evidence_hash
            and selection["requirement_version_before"] == snapshot["requirement_version"]
            and selection["requirement_version_after"] == current["requirement_version"]
        )
    input_changed = (
        not version_matches
        or current["required_work_item_set_version"] != snapshot["required_work_item_set_version"]
        or current["required_work_item_set_hash"] != snapshot["required_work_item_set_hash"]
        or {str(item["id"]) for item in repository.work_items(requirement_id)}
        != set(snapshot["work_item_ids"])
    )
    reasons = list(evidence.currentness_reasons)
    if input_changed:
        reasons.append("REQUIREMENT_INPUT_CHANGED")
    artifacts_unavailable = False
    try:
        _validate_evidence_artifacts(requirement_id, evidence, dependencies)
    except EvidenceUnavailableOrStale:
        artifacts_unavailable = True
        reasons.append("ARTIFACT_UNAVAILABLE_OR_STALE")
    return evidence.model_copy(
        update={
            "currentness_state": (
                "STALE"
                if input_changed or evidence.currentness_state == "STALE"
                else "UNAVAILABLE"
                if artifacts_unavailable or evidence.currentness_state == "UNAVAILABLE"
                else "CURRENT"
            ),
            "currentness_reasons": tuple(reasons),
        }
    )


def _snapshot_dto(value: Any) -> RequirementDeliverySnapshot:
    return RequirementDeliverySnapshot(
        id=str(value["id"]),
        requirement_id=str(value["requirement_id"]),
        requirement_version=value["requirement_version"],
        required_work_item_set_version=value["required_work_item_set_version"],
        required_work_item_set_hash=value["required_work_item_set_hash"],
        work_item_ids=tuple(str(item) for item in value["work_item_ids"]),
        snapshot_hash=value["snapshot_hash"],
        created_by=value["created_by"],
        created_at=value["created_at"],
    )


def _gate_projection(
    gate: object | None,
    assignment: object | None,
    decision: object | None,
) -> CurrentDeliveryGateProjection | None:
    if gate is None:
        return None
    return CurrentDeliveryGateProjection(
        gate=_gate_dto(gate),
        assignment=None if assignment is None else _assignment_dto(assignment),
        decision=None if decision is None else _decision_dto(decision),
    )


def get_requirement_delivery(
    repository: RequirementRepository,
    *,
    requirement_id: str,
) -> RequirementDeliveryProjection:
    requirement = repository.requirement_by_id(requirement_id)
    if requirement is None:
        raise RequirementNotFound(requirement_id)

    selection = None
    latest_snapshot = repository.latest_delivery_snapshot(requirement_id)
    snapshot = None if latest_snapshot is None else _snapshot_dto(latest_snapshot)
    selection_id = requirement["current_integration_baseline_selection_id"]
    if selection_id is not None:
        selection_row = repository.integration_baseline_selection_by_id(str(selection_id))
        if selection_row is not None:
            selection = _selection_dto(selection_row)
            snapshot_row = repository.delivery_snapshot_by_id(
                str(selection_row["delivery_snapshot_id"])
            )
            if snapshot_row is not None:
                snapshot = _snapshot_dto(snapshot_row)

    acceptance = None
    acceptance_gate_id = requirement["current_acceptance_gate_id"]
    if acceptance_gate_id is not None:
        acceptance_gate = repository.delivery_gate_by_id(str(acceptance_gate_id))
        if acceptance_gate is not None:
            acceptance = _gate_projection(
                acceptance_gate,
                repository.current_delivery_gate_assignment(str(acceptance_gate_id)),
                repository.delivery_decision_by_gate(str(acceptance_gate_id)),
            )

    formal_contexts = {
        str(row["work_item_id"]): row
        for row in repository.current_formal_delivery_projections(requirement_id)
    }
    work_items = []
    for item in repository.work_items(requirement_id):
        context = formal_contexts.get(str(item["id"]))
        formal_review = (
            None
            if context is None
            else _gate_projection(
                context["gate"],
                context["assignment"],
                context["decision"],
            )
        )
        work_items.append(
            WorkItemFormalDeliveryProjection(
                work_item=work_item_dto(item),
                current_formal_review=formal_review,
            )
        )

    return RequirementDeliveryProjection(
        requirement=requirement_dto(requirement),
        current_delivery_snapshot=snapshot,
        current_selection=selection,
        current_acceptance=acceptance,
        work_items=tuple(work_items),
    )


def _encode_history_cursor(occurred_at: datetime, fact_type: str, fact_id: str) -> str:
    payload = json.dumps(
        {
            "factId": fact_id,
            "factType": fact_type,
            "occurredAt": occurred_at.isoformat(),
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_history_cursor(
    cursor: str | None,
) -> tuple[datetime | None, DeliveryHistoryFactType | None, str | None]:
    if cursor is None:
        return None, None, None
    try:
        encoded = cursor.encode("ascii")
        padded = encoded + b"=" * (-len(encoded) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != {
            "factId",
            "factType",
            "occurredAt",
            "version",
        }:
            raise ValueError
        if payload["version"] != 1:
            raise ValueError
        occurred_at = datetime.fromisoformat(payload["occurredAt"])
        if occurred_at.tzinfo is None:
            raise ValueError
        fact_type = DeliveryHistoryFactType(payload["factType"])
        fact_id = str(UUID(payload["factId"]))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise InvalidRequirementCursor("invalid Requirement delivery history cursor") from None
    return occurred_at, fact_type, fact_id


def _history_fact(fact_type: DeliveryHistoryFactType, value: Any) -> DeliveryHistoryFact:
    if fact_type is DeliveryHistoryFactType.DELIVERY_SNAPSHOT:
        return _snapshot_dto(value)
    if fact_type is DeliveryHistoryFactType.INTEGRATION_BASELINE_SELECTION:
        return _selection_dto(value)
    if fact_type is DeliveryHistoryFactType.DELIVERY_GATE:
        return _gate_dto(value)
    if fact_type is DeliveryHistoryFactType.DELIVERY_GATE_ASSIGNMENT:
        return _assignment_dto(value)
    return _decision_dto(value)


def list_requirement_delivery_history(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    cursor: str | None,
    limit: int,
) -> DeliveryHistoryPage:
    if not 1 <= limit <= 100:
        raise InvalidRequirementCursor(
            "Requirement delivery history limit must be between 1 and 100"
        )
    requirement = repository.requirement_by_id(requirement_id)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    before_at, before_type, before_id = _decode_history_cursor(cursor)
    rows = repository.list_delivery_history(
        requirement_id,
        before_occurred_at=before_at,
        before_fact_type=None if before_type is None else before_type.value,
        before_fact_id=before_id,
        limit=limit + 1,
    )
    visible = rows[:limit]
    items = tuple(
        DeliveryHistoryItem(
            fact_type=DeliveryHistoryFactType(row["fact_type"]),
            occurred_at=row["occurred_at"],
            fact=_history_fact(DeliveryHistoryFactType(row["fact_type"]), row["fact"]),
        )
        for row in visible
    )
    next_cursor = None
    if len(rows) > limit:
        last = visible[-1]
        next_cursor = _encode_history_cursor(
            last["occurred_at"],
            last["fact_type"],
            str(last["fact_id"]),
        )
    return DeliveryHistoryPage(
        requirement_id=requirement_id,
        requirement_revision=requirement["revision"],
        items=items,
        next_cursor=next_cursor,
    )
