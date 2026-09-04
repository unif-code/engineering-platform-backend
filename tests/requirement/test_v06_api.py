from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import control_plane.app.modules.requirement as requirement
from control_plane.app import __version__
from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.requirement import (
    DecisionOutcome,
)
from control_plane.app.modules.requirement.api import (
    RequirementHttpRuntime,
    create_requirement_v06_delivery_router,
    v06_routes,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_api import SAME_ORIGIN, CapabilityGuard, PrincipalHolder
from tests.requirement.test_commands import Actor
from tests.requirement.test_v06_acceptance_commands import (
    StaticDeliveryReviewerGuard,
    _selection_fixture,
)

REQUIREMENT_ID = "10000000-0000-0000-0000-000000000690"
WORK_ITEM_ID = "20000000-0000-0000-0000-000000000690"
SUBJECT_ID = "30000000-0000-0000-0000-000000000690"


def test_default_http_reassigns_with_assign_only_grant_and_exact_gate_etag(
    isolated_requirement_database: IsolatedRequirementDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.app.bootstrap import app as bootstrap

    database = isolated_requirement_database
    requested, evidence, dependencies = _selection_fixture(
        database, key_suffix="default-reassign-http"
    )
    granted = {"employee-1": set(), "candidate": {"requirement.acceptance.decide"}}

    def evaluate(
        *, actor_id: str, workspace_id: str, required_capabilities: tuple[str, ...]
    ) -> Any:
        return StaticDeliveryReviewerGuard(
            set(required_capabilities) <= granted[actor_id]
        ).evaluate(
            actor_id=actor_id,
            workspace_id=workspace_id,
            required_capabilities=required_capabilities,
        )

    dependencies = replace(dependencies, delivery_reviewer_guard=SimpleNamespace(evaluate=evaluate))
    with database.runtime.begin() as db:
        selected = requirement.select_integration_baseline(
            db,
            requirement_id=requested.requirement.id,
            delivery_snapshot_id=requested.snapshot.id,
            integration_baseline_id=evidence.id,
            expected_revision=requested.requirement.revision,
            expected_requirement_version=requested.requirement.requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="http-reassign-select",
            dependencies=dependencies,
        )
        confirmation = requirement.confirm_requirement_acceptance(
            db,
            requirement_id=requested.requirement.id,
            selection_id=selected.selection.id,
            expected_revision=selected.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="http-reassign-confirm",
            dependencies=dependencies,
        )
    guards = []

    def guard(principal: Any, capability: str, workspace_id: str | None) -> None:
        guards.append((capability, workspace_id))
        if capability not in {"requirement.read", *granted[principal.account_id]}:
            raise HTTPException(403, "Forbidden")

    monkeypatch.setattr(bootstrap, "current_principal", lambda _: lambda: Actor("employee-1"))
    monkeypatch.setattr(bootstrap, "authorization_capability_guard", guard)
    client = TestClient(
        bootstrap.create_app(
            requirement_runtime_provider=lambda: RequirementHttpRuntime(
                database.runtime, dependencies
            )
        )
    )
    base = f"/api/v1/requirements/{requested.requirement.id}"
    path = f"{base}/delivery-gates/{confirmation.gate.id}:reassign"
    body = {"candidateId": "candidate", "reason": "Delegate acceptance"}
    headers = {
        **SAME_ORIGIN,
        "Idempotency-Key": "http-reassign",
        "If-Match": f'"v{confirmation.gate.revision}"',
    }
    assert client.post(path, json=body, headers=headers).status_code == 403
    granted["employee-1"].add("requirement.delivery_gate.assign")
    assert (
        client.post(path, json=body, headers={**headers, "If-Match": 'W/"v1"'}).status_code == 422
    )
    assert (
        client.post(
            path, json=body, headers={k: v for k, v in headers.items() if k != "If-Match"}
        ).status_code
        == 422
    )
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["etag"] == f'"v{confirmation.gate.revision + 1}"'
    assert response.json()["assignment"]["currentReviewerId"] == "candidate"
    replay = client.post(path, json=body, headers=headers)
    assert replay.json() == response.json()
    assert replay.headers["etag"] == response.headers["etag"]
    assert client.post(path, json={**body, "reason": "changed"}, headers=headers).status_code == 409
    assert (
        client.post(
            path, json=body, headers={**headers, "Idempotency-Key": "stale-http-reassign"}
        ).status_code
        == 409
    )
    assert ("requirement.delivery_gate.assign", requested.requirement.workspace_id) in guards
    assert all(capability != "requirement.acceptance.decide" for capability, _ in guards)
    for state in ("AWAITING_ACCEPTANCE", "AWAITING_MERGE", "COMPLETED"):
        from sqlalchemy import text

        with database.owner.begin() as db:
            db.execute(
                text("UPDATE requirement.requirement SET state=:state WHERE id=:id"),
                {"state": state, "id": requested.requirement.id},
            )
        detail = client.get(base)
        listing = client.get(
            "/api/v1/requirements", params={"workspaceId": requested.requirement.workspace_id}
        )
        assert detail.status_code == listing.status_code == 200
        assert detail.json()["requirement"]["state"] == state
        assert (
            next(
                item for item in listing.json()["items"] if item["id"] == requested.requirement.id
            )["state"]
            == state
        )


class _ProblemEngine:
    @contextmanager
    def begin(self) -> Any:
        yield object()


def _problem_client(monkeypatch: pytest.MonkeyPatch, command: str, error: Exception) -> TestClient:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(v06_routes, command, fail)
    monkeypatch.setattr(v06_routes, "_authorized_details", lambda *_args, **_kwargs: object())
    app = FastAPI()
    app.include_router(
        create_requirement_v06_delivery_router(
            lambda: RequirementHttpRuntime(
                engine=_ProblemEngine(),  # type: ignore[arg-type]
                dependencies=object(),  # type: ignore[arg-type]
            ),
            lambda: Actor("employee-1"),
            lambda *_: None,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _client(
    database: IsolatedRequirementDatabase,
    dependencies: object,
) -> TestClient:
    holder = PrincipalHolder(Actor("employee-1"))
    guard = CapabilityGuard(
        {
            ("requirement.evidence.select", None),
        }
    )

    def permit(principal: object, capability: str, workspace_id: str | None) -> None:
        del principal
        guard.calls.append((capability, workspace_id))

    app = FastAPI()
    app.include_router(
        create_requirement_v06_delivery_router(
            lambda: RequirementHttpRuntime(
                engine=database.runtime,
                dependencies=dependencies,  # type: ignore[arg-type]
            ),
            holder.get,
            permit,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_default_app_publishes_v06_delivery_and_gate_reassignment() -> None:
    app = FastAPI()
    app.include_router(
        create_requirement_v06_delivery_router(
            lambda: None,  # type: ignore[arg-type,return-value]
            lambda: Actor("employee-1"),
            lambda *_: None,
        )
    )
    paths = app.openapi()["paths"]
    write_paths = {
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}/external-validations",
        "/api/v1/requirements/{requirementId}:request-integration-baseline",
        "/api/v1/requirements/{requirementId}/integration-baseline-selections",
        "/api/v1/requirements/{requirementId}/acceptance-confirmations",
        "/api/v1/requirements/{requirementId}/acceptance-decisions",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:request-formal-mr",
        "/api/v1/requirements/{requirementId}/formal-review-decisions",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:request-formal-merge",
        "/api/v1/requirements/{requirementId}/delivery-gates/{gateId}:reassign",
    }
    expected = write_paths | {
        "/api/v1/requirements/{requirementId}/delivery",
        "/api/v1/requirements/{requirementId}/delivery/history",
    }
    assert expected == set(paths)
    for path in write_paths:
        operation = paths[path]["post"]
        headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
        assert headers["Idempotency-Key"]["required"] is True
        assert headers["If-Match"]["required"] is True
        for status in ("401", "403", "404", "409", "422", "500", "503"):
            content = operation["responses"][status]["content"]
            assert set(content) == {"application/problem+json"}
            assert "reason" in content["application/problem+json"]["schema"]["properties"]
    explicit_schemas = app.openapi()["components"]["schemas"]
    v06_requirement = explicit_schemas["RequirementResponseDto"]
    assert {
        "acceptanceCriteriaVersion",
        "acceptanceCriteriaHash",
        "currentIntegrationBaselineSelectionId",
        "currentAcceptanceGateId",
    } <= set(v06_requirement["properties"])
    assert set(explicit_schemas["RequirementState"]["enum"]) >= {
        "AWAITING_ACCEPTANCE",
        "AWAITING_MERGE",
        "COMPLETED",
    }
    default_schema = create_app().openapi()
    assert expected <= set(default_schema["paths"])
    assert default_schema["info"]["version"] == __version__
    default_schemas = default_schema["components"]["schemas"]
    default_requirement = default_schemas["RequirementResponseDto"]
    assert {
        "acceptanceCriteriaVersion",
        "acceptanceCriteriaHash",
        "currentIntegrationBaselineSelectionId",
        "currentAcceptanceGateId",
    } <= set(default_requirement["properties"])
    assert default_schemas["RequirementState"]["enum"] == [
        "CREATED",
        "PREPARING",
        "AWAITING_CONFIRMATION",
        "READY",
        "IN_PROGRESS",
        "VERIFYING",
        "AWAITING_ACCEPTANCE",
        "AWAITING_MERGE",
        "COMPLETED",
        "CANCELED",
    ]
    assert default_schemas["WorkItemState"]["enum"] == [
        "DRAFT",
        "READY",
        "IN_PROGRESS",
        "VERIFYING",
        "AWAITING_MERGE",
        "COMPLETED",
        "CANCELED",
    ]
    problem = default_schemas["Problem"]
    assert "reason" not in problem["properties"]


@pytest.mark.parametrize(
    ("command", "error_type", "path", "body", "title", "type_uri", "reason"),
    [
        pytest.param(
            "request_integration_baseline",
            requirement.DeliverySnapshotConflict,
            f"/api/v1/requirements/{REQUIREMENT_ID}:request-integration-baseline",
            {"expectedRequirementVersion": 1},
            "Requirement snapshot conflict",
            "urn:engineering-platform:problem:requirement:snapshot-conflict",
            "SNAPSHOT_CONFLICT",
            id="snapshot-conflict",
        ),
        pytest.param(
            "submit_external_validation",
            requirement.EvidenceUnavailableOrStale,
            (
                f"/api/v1/requirements/{REQUIREMENT_ID}/work-items/"
                f"{WORK_ITEM_ID}/external-validations"
            ),
            {
                "targetCommitSha": "a" * 40,
                "integrationMergeCommitSha": "b" * 40,
                "reference": "https://ci.example.test/runs/690",
                "notes": "Verified exact immutable artifacts.",
                "artifactReferences": [
                    {
                        "artifactId": "artifact-690",
                        "artifactVersion": "1",
                        "artifactHash": "sha256:" + "c" * 64,
                    }
                ],
            },
            "Delivery evidence unavailable or stale",
            "urn:engineering-platform:problem:requirement:evidence-unavailable-or-stale",
            "EVIDENCE_UNAVAILABLE_OR_STALE",
            id="evidence-unavailable-or-stale",
        ),
        pytest.param(
            "select_integration_baseline",
            requirement.SelectionStale,
            f"/api/v1/requirements/{REQUIREMENT_ID}/integration-baseline-selections",
            {
                "deliverySnapshotId": SUBJECT_ID,
                "integrationBaselineId": "40000000-0000-0000-0000-000000000690",
                "expectedRequirementVersion": 1,
            },
            "Integration baseline selection stale",
            "urn:engineering-platform:problem:requirement:selection-stale",
            "SELECTION_STALE",
            id="selection-stale",
        ),
        pytest.param(
            "confirm_requirement_acceptance",
            requirement.AcceptanceStale,
            f"/api/v1/requirements/{REQUIREMENT_ID}/acceptance-confirmations",
            {"selectionId": SUBJECT_ID},
            "Requirement acceptance stale",
            "urn:engineering-platform:problem:requirement:acceptance-stale",
            "ACCEPTANCE_STALE",
            id="acceptance-stale",
        ),
        pytest.param(
            "decide_formal_review",
            requirement.FormalReviewStale,
            f"/api/v1/requirements/{REQUIREMENT_ID}/formal-review-decisions",
            {"gateId": SUBJECT_ID, "outcome": "APPROVED", "reason": "Reviewed exact head."},
            "Formal review stale",
            "urn:engineering-platform:problem:requirement:review-stale",
            "REVIEW_STALE",
            id="review-stale",
        ),
        pytest.param(
            "request_formal_merge_request",
            requirement.FormalDeliveryBlocked,
            (f"/api/v1/requirements/{REQUIREMENT_ID}/work-items/{WORK_ITEM_ID}:request-formal-mr"),
            {},
            "Formal delivery blocked",
            "urn:engineering-platform:problem:requirement:formal-delivery-blocked",
            "FORMAL_DELIVERY_BLOCKED",
            id="formal-delivery-blocked",
        ),
    ],
)
def test_v06_domain_conflicts_publish_distinct_stable_problem_contracts(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    error_type: type[Exception],
    path: str,
    body: dict[str, object],
    title: str,
    type_uri: str,
    reason: str,
) -> None:
    client = _problem_client(monkeypatch, command, error_type("internal detail must stay private"))

    response = client.post(
        path,
        json=body,
        headers={**SAME_ORIGIN, "Idempotency-Key": f"problem-{reason.lower()}", "If-Match": '"v1"'},
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": type_uri,
        "title": title,
        "status": 409,
        "reason": reason,
    }


def test_selection_acceptance_and_formal_request_are_http_versioned_and_camel_case(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested, evidence, dependencies = _selection_fixture(
        isolated_requirement_database,
        key_suffix="v06-http",
    )
    client = _client(isolated_requirement_database, dependencies)
    base = f"/api/v1/requirements/{requested.requirement.id}"

    selected_response = client.post(
        f"{base}/integration-baseline-selections",
        json={
            "deliverySnapshotId": requested.snapshot.id,
            "integrationBaselineId": evidence.id,
            "expectedRequirementVersion": requested.requirement.requirement_version,
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-http-select",
            "If-Match": f'"v{requested.requirement.revision}"',
        },
    )
    assert selected_response.status_code == 200, selected_response.text
    selected = selected_response.json()
    assert selected["selection"]["integrationBaselineHash"] == evidence.evidence_hash
    assert "integration_baseline_hash" not in selected["selection"]

    confirmation_response = client.post(
        f"{base}/acceptance-confirmations",
        json={"selectionId": selected["selection"]["id"]},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-http-acceptance-open",
            "If-Match": selected_response.headers["etag"],
        },
    )
    assert confirmation_response.status_code == 200, confirmation_response.text
    confirmation = confirmation_response.json()

    decision_response = client.post(
        f"{base}/acceptance-decisions",
        json={
            "gateId": confirmation["gate"]["id"],
            "outcome": DecisionOutcome.APPROVED.value,
            "reason": "The exact Evidence satisfies every criterion.",
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-http-acceptance-decide",
            "If-Match": confirmation_response.headers["etag"],
        },
    )
    assert decision_response.status_code == 200, decision_response.text
    decided = decision_response.json()
    assert decided["requirement"]["state"] == "AWAITING_MERGE"

    formal_response = client.post(
        f"{base}/work-items/{evidence.work_items[0].work_item_id}:request-formal-mr",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-http-formal-request",
            "If-Match": decision_response.headers["etag"],
        },
    )
    assert formal_response.status_code == 202, formal_response.text
    formal = formal_response.json()
    assert formal["workItem"]["formalDeliveryState"] == "MR_PENDING"
    assert formal["outboxTopic"] == "requirement.formal-merge-request.requested"
