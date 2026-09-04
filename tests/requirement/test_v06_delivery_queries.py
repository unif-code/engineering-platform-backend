from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    DecisionOutcome,
    DeliveryGatePolicySnapshot,
    FormalDeliveryBlockedReason,
    RequirementDependencies,
    confirm_requirement_acceptance,
    decide_formal_review,
    decide_requirement_acceptance,
    record_formal_delivery_blocked,
    record_formal_mr_ready,
    request_formal_merge,
    request_formal_merge_request,
    select_integration_baseline,
)
from control_plane.app.modules.requirement.api import (
    RequirementHttpRuntime,
    create_requirement_v06_delivery_router,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_commands import NOW, WORKSPACE_ID, Actor
from tests.requirement.test_v06_acceptance_commands import _selection_fixture
from tests.requirement.test_v06_formal_delivery_commands import _approved_requirement


@dataclass(slots=True)
class _ReadGuard:
    calls: list[tuple[str, str | None]]

    def __call__(self, principal: object, capability: str, workspace_id: str | None) -> None:
        del principal
        self.calls.append((capability, workspace_id))
        if (capability, workspace_id) != ("requirement.read", WORKSPACE_ID):
            raise HTTPException(status_code=403, detail="Forbidden")


def _client(
    database: IsolatedRequirementDatabase,
    dependencies: RequirementDependencies,
) -> tuple[TestClient, _ReadGuard]:
    guard = _ReadGuard([])
    app = FastAPI()
    app.include_router(
        create_requirement_v06_delivery_router(
            lambda: RequirementHttpRuntime(
                engine=database.runtime,
                dependencies=dependencies,
            ),
            lambda: Actor("employee-1"),
            guard,
        )
    )
    return TestClient(app, raise_server_exceptions=False), guard


def test_current_delivery_projection_exposes_latest_requested_snapshot_before_selection(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, _evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-read-requested-snapshot",
    )

    client, guard = _client(isolated_requirement_database, dependencies)
    response = client.get(f"/api/v1/requirements/{requested.requirement.id}/delivery")

    assert response.status_code == 200, response.text
    assert response.headers["etag"] == f'"v{requested.requirement.revision}"'
    payload = response.json()
    assert payload["currentDeliverySnapshot"]["id"] == requested.snapshot.id
    assert payload["currentSelection"] is None
    assert payload["currentAcceptance"] is None
    assert guard.calls == [("requirement.read", WORKSPACE_ID)]


def test_current_delivery_projection_recovers_acceptance_and_formal_review(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    approved, evidence, dependencies = _approved_requirement(isolated_requirement_database)
    work_item_id = evidence.work_items[0].work_item_id
    binding_id = "96000000-0000-0000-0000-000000000691"
    with isolated_requirement_database.runtime.begin() as db:
        requested = request_formal_merge_request(
            db,
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=approved.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-read-formal-create",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        ready = record_formal_mr_ready(
            db,
            work_item_id=work_item_id,
            binding_id=binding_id,
            head_sha=evidence.work_items[0].task_commit_sha,
            expected_revision=requested.work_item.revision,
            assignment=DeliveryGatePolicySnapshot(
                version=4,
                default_reviewer_id="employee-1",
                policy_code="FORMAL_REVIEW_WORK_ITEM_OWNER",
                snapshot_hash="sha256:" + "a" * 64,
                resolution_snapshot={"rule": "WORK_ITEM_OWNER"},
            ),
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-read-formal-ready",
            correlation_id="v06-read-formal-ready",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        reviewed = decide_formal_review(
            db,
            requirement_id=approved.requirement.id,
            gate_id=ready.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="The exact formal diff is approved.",
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-read-formal-review",
            dependencies=dependencies,
        )

    client, guard = _client(isolated_requirement_database, dependencies)
    response = client.get(f"/api/v1/requirements/{approved.requirement.id}/delivery")

    assert response.status_code == 200, response.text
    assert response.headers["etag"] == f'"v{reviewed.requirement.revision}"'
    payload = response.json()
    assert payload["requirement"]["id"] == approved.requirement.id
    assert payload["currentDeliverySnapshot"]["id"] == approved.selection.delivery_snapshot_id
    assert payload["currentSelection"]["id"] == approved.selection.id
    assert payload["currentAcceptance"]["gate"]["id"] == approved.gate.id
    assert payload["currentAcceptance"]["assignment"]["id"] == approved.assignment.id
    assert payload["currentAcceptance"]["decision"]["outcome"] == "APPROVED"
    assert len(payload["workItems"]) == 1
    formal = payload["workItems"][0]
    assert formal["workItem"]["formalDeliveryState"] == "MR_OPEN"
    assert formal["currentFormalReview"]["gate"]["id"] == ready.gate.id

    with isolated_requirement_database.runtime.begin() as db:
        merge_requested = request_formal_merge(
            db,
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=reviewed.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-read-formal-merge",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        blocked = record_formal_delivery_blocked(
            db,
            work_item_id=work_item_id,
            binding_id=binding_id,
            reason_code=FormalDeliveryBlockedReason.MR_CHECKS_BLOCKED,
            expected_revision=merge_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key="v06-read-formal-blocked",
            correlation_id="v06-read-formal-blocked",
            dependencies=dependencies,
        )

    blocked_response = client.get(f"/api/v1/requirements/{approved.requirement.id}/delivery")
    assert blocked_response.status_code == 200, blocked_response.text
    blocked_work_item = blocked_response.json()["workItems"][0]["workItem"]
    assert blocked_work_item["formalDeliveryState"] == "BLOCKED"
    assert blocked_work_item["formalBlockedReasonCode"] == "MR_CHECKS_BLOCKED"

    with isolated_requirement_database.runtime.begin() as db:
        retried = request_formal_merge(
            db,
            requirement_id=approved.requirement.id,
            work_item_id=work_item_id,
            expected_revision=blocked.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-read-formal-merge-retry",
            dependencies=dependencies,
        )
    assert retried.work_item.formal_delivery_state.value == "MERGE_PENDING"
    assert retried.work_item.formal_blocked_reason_code is None
    assert formal["currentFormalReview"]["assignment"]["id"] == ready.assignment.id
    assert formal["currentFormalReview"]["decision"]["outcome"] == "APPROVED"
    assert guard.calls == [("requirement.read", WORKSPACE_ID)] * 2


def test_delivery_history_pages_every_invalidated_and_superseded_fact(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-read-history",
    )
    with isolated_requirement_database.runtime.begin() as db:
        selected = select_integration_baseline(
            db,
            requirement_id=requested.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-read-history-select",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        opened = confirm_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-read-history-open",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        decided = decide_requirement_acceptance(
            db,
            requirement_id=selected.requirement.id,
            gate_id=opened.gate.id,
            outcome=DecisionOutcome.CHANGES_REQUESTED,
            reason="The accepted evidence needs correction.",
            expected_revision=opened.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="v06-read-history-decide",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.delivery_gate_assignment SET superseded_at=:now "
                "WHERE id=:assignment_id"
            ),
            {"assignment_id": opened.assignment.id, "now": NOW},
        )

    client, guard = _client(isolated_requirement_database, dependencies)
    base = f"/api/v1/requirements/{requested.requirement.id}/delivery/history"
    first = client.get(base, params={"limit": 2})
    assert first.status_code == 200, first.text
    assert first.headers["etag"] == f'"v{decided.requirement.revision}"'
    assert first.json()["nextCursor"] is not None
    assert opened.assignment.id not in first.json()["nextCursor"]

    second = client.get(
        base,
        params={"limit": 2, "cursor": first.json()["nextCursor"]},
    )
    assert second.status_code == 200, second.text
    third = client.get(
        base,
        params={"limit": 2, "cursor": second.json()["nextCursor"]},
    )
    assert third.status_code == 200, third.text
    assert third.json()["nextCursor"] is None

    items = first.json()["items"] + second.json()["items"] + third.json()["items"]
    assert [item["factType"] for item in items] == [
        "INTEGRATION_BASELINE_SELECTION",
        "DELIVERY_SNAPSHOT",
        "DELIVERY_GATE_ASSIGNMENT",
        "DELIVERY_GATE",
        "DELIVERY_DECISION",
    ]
    assert len({item["fact"]["id"] for item in items}) == 5
    by_type = {item["factType"]: item["fact"] for item in items}
    assert by_type["INTEGRATION_BASELINE_SELECTION"]["invalidatedAt"] is not None
    assert by_type["DELIVERY_GATE"]["invalidatedAt"] is not None
    assert by_type["DELIVERY_DECISION"]["invalidatedAt"] is not None
    assert by_type["DELIVERY_GATE_ASSIGNMENT"]["supersededAt"] is not None
    assert guard.calls == [("requirement.read", WORKSPACE_ID)] * 3

    malformed = client.get(base, params={"cursor": "not-an-opaque-cursor"})
    assert malformed.status_code == 422
    assert malformed.json()["title"] == "Invalid Requirement cursor"


def test_delivery_read_endpoints_are_explicit_v06_routes_only() -> None:
    explicit = FastAPI()
    explicit.include_router(
        create_requirement_v06_delivery_router(
            lambda: None,  # type: ignore[arg-type,return-value]
            lambda: Actor("employee-1"),
            lambda *_: None,
        )
    )
    paths = explicit.openapi()["paths"]
    assert paths["/api/v1/requirements/{requirementId}/delivery"]["get"]["operationId"] == (
        "requirements_get_delivery"
    )
    history = paths["/api/v1/requirements/{requirementId}/delivery/history"]["get"]
    assert history["operationId"] == "requirements_list_delivery_history"
    assert {parameter["name"] for parameter in history["parameters"]} == {
        "requirementId",
        "cursor",
        "limit",
    }

    from control_plane.app.bootstrap.app import create_app

    default_paths = create_app().openapi()["paths"]
    assert "/api/v1/requirements/{requirementId}/delivery" not in default_paths
    assert "/api/v1/requirements/{requirementId}/delivery/history" not in default_paths
