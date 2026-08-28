from dataclasses import dataclass, field, replace
from typing import Any, cast

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from control_plane.app.bootstrap.app import (
    authorization_capability_guard,
    authorization_http_runtime,
    create_app,
    requirement_dependencies,
    requirement_http_runtime,
)
from control_plane.app.modules.authorization.api.dependencies import current_principal
from control_plane.app.modules.requirement import (
    DecisionOutcome,
    RequirementDependencies,
    RequirementState,
    RequirementType,
    start_requirement_preparation,
)
from control_plane.app.modules.requirement.adapters import (
    ComposedAutomaticAssignmentGuard,
)
from control_plane.app.modules.requirement.api import (
    RequirementHttpRuntime,
    create_requirement_baseline_router,
    create_requirement_delivery_router,
    create_requirement_foundation_router,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_baseline_gate import _gate_dependencies
from tests.requirement.test_commands import WORKSPACE_ID, Actor
from tests.requirement.test_delivery_commands import _merge_ready_work_item, _ready_requirement

SAME_ORIGIN = {"Origin": "http://testserver"}


def test_bootstrap_exposes_only_v03_requirement_endpoints_with_fail_closed_dependencies() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    assert {
        "/api/v1/requirements",
        "/api/v1/requirements/{requirementId}",
    } <= set(paths)
    assert {
        "/api/v1/requirements/{requirementId}/sdd-baselines",
        "/api/v1/requirements/{requirementId}/baseline-confirmations",
        "/api/v1/requirements/{requirementId}/baseline-decisions",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:start",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:request-integration-mr",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:request-integration-merge",
    }.isdisjoint(paths)
    assert set(paths["/api/v1/requirements"]) >= {"get", "post"}
    create = paths["/api/v1/requirements"]["post"]
    create_headers = {item["name"]: item for item in create["parameters"] if item["in"] == "header"}
    assert create_headers["Idempotency-Key"]["required"] is True
    assert "If-Match" not in create_headers
    requirement_schema = schema["components"]["schemas"]["RequirementResponseDto"]
    assert requirement_schema["properties"]["workspaceId"]["format"] == "uuid"
    assert requirement_schema["properties"]["state"] == {
        "$ref": "#/components/schemas/RequirementState"
    }
    assert requirement_schema["properties"]["createdAt"]["format"] == "date-time"
    work_item_schema = schema["components"]["schemas"]["WorkItemResponseDto"]
    assert work_item_schema["properties"]["repositoryBlockedReasonCode"]["anyOf"] == [
        {"$ref": "#/components/schemas/RepositoryBindingBlockedReason"},
        {"type": "null"},
    ]
    assert work_item_schema["properties"]["repositoryBlockedAt"]["anyOf"] == [
        {"format": "date-time", "type": "string"},
        {"type": "null"},
    ]
    assert "integrationDeliveryState" not in work_item_schema["properties"]
    assert "integrationMergeRequestBindingId" not in work_item_schema["properties"]

    dependencies = requirement_dependencies()
    route = dependencies.route_snapshots.current(RequirementType.FEAT)
    assert route.version == 1
    assert route.snapshot_hash.startswith("sha256:")
    assert route.required_capabilities == ("code.change",)
    assert isinstance(dependencies.assignment_guard, ComposedAutomaticAssignmentGuard)
    assert not dependencies.assignment_guard.can_auto_assign(
        actor_id="employee-1",
        workspace_id=WORKSPACE_ID,
        repository_id="repository-1",
        required_capabilities=route.required_capabilities,
    )
    assert dependencies.artifacts is None
    assert dependencies.gate_policies is None
    assert dependencies.reviewer_guard is None


def _dormant_requirement_app() -> FastAPI:
    app = FastAPI()
    protected_principal = current_principal(authorization_http_runtime)
    for router_factory in (
        create_requirement_foundation_router,
        create_requirement_baseline_router,
        create_requirement_delivery_router,
    ):
        app.include_router(
            router_factory(
                requirement_http_runtime,
                cast(Any, protected_principal),
                authorization_capability_guard,
            )
        )
    return app


def test_dormant_requirement_routers_are_explicitly_composable() -> None:
    paths = _dormant_requirement_app().openapi()["paths"]

    for suffix in (
        "sdd-baselines",
        "baseline-confirmations",
        "baseline-decisions",
    ):
        operation = paths[f"/api/v1/requirements/{{requirementId}}/{suffix}"]["post"]
        headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
        assert headers["Idempotency-Key"]["required"] is True
        assert headers["If-Match"]["required"] is True
        assert operation["security"] == [{"EpSessionCookie": []}]
    for suffix, status in (
        ("start", "200"),
        ("request-integration-mr", "202"),
        ("request-integration-merge", "202"),
    ):
        operation = paths["/api/v1/requirements/{requirementId}/work-items/{workItemId}:" + suffix][
            "post"
        ]
        headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
        assert headers["Idempotency-Key"]["required"] is True
        assert headers["If-Match"]["required"] is True
        assert status in operation["responses"]
        assert operation["security"] == [{"EpSessionCookie": []}]


@dataclass(slots=True)
class PrincipalHolder:
    value: Actor

    def get(self) -> Actor:
        return self.value


@dataclass(slots=True)
class CapabilityGuard:
    allowed: set[tuple[str, str | None]]
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def __call__(self, principal: Any, capability: str, workspace_id: str | None) -> None:
        del principal
        key = (capability, workspace_id)
        self.calls.append(key)
        if key not in self.allowed:
            raise HTTPException(status_code=403, detail="Forbidden")


def _client(
    database: IsolatedRequirementDatabase,
    *,
    dependencies: RequirementDependencies | None = None,
    allowed: set[tuple[str, str | None]] | None = None,
) -> tuple[TestClient, PrincipalHolder, CapabilityGuard, RequirementDependencies]:
    resolved_dependencies = dependencies or _gate_dependencies()
    holder = PrincipalHolder(Actor("employee-1"))
    guard = CapabilityGuard(
        allowed
        or {
            ("requirement.create", WORKSPACE_ID),
            ("requirement.read", WORKSPACE_ID),
            ("requirement.baseline.submit", WORKSPACE_ID),
            ("requirement.baseline.decide", WORKSPACE_ID),
        }
    )
    app = FastAPI()
    app.middleware("http")(request_id_middleware)
    register_problem_handlers(app)
    for router_factory in (
        create_requirement_foundation_router,
        create_requirement_baseline_router,
        create_requirement_delivery_router,
    ):
        app.include_router(
            router_factory(
                lambda: RequirementHttpRuntime(
                    engine=database.runtime,
                    dependencies=resolved_dependencies,
                ),
                holder.get,
                guard,
            )
        )
    return (
        TestClient(app, raise_server_exceptions=False),
        holder,
        guard,
        resolved_dependencies,
    )


def _create_body(*, workspace_id: str = WORKSPACE_ID) -> dict[str, object]:
    return {
        "workspaceId": workspace_id,
        "type": "feat",
        "title": "Govern delivery",
        "description": "Create an auditable human delivery flow.",
        "acceptanceCriteria": ["The SDD baseline is approved."],
        "initialRepositoryId": "repository-1",
    }


def _create_via_api(client: TestClient, *, key: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/requirements",
        json=_create_body(),
        headers={**SAME_ORIGIN, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_create_list_and_detail_use_camel_case_cursor_shape_and_etag(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    client, _holder, guard, _dependencies = _client(isolated_requirement_database)

    created = client.post(
        "/api/v1/requirements",
        json=_create_body(),
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "requirement-api-create-1",
            "X-Request-ID": "req-requirement-create",
        },
    )

    assert created.status_code == 201
    assert created.headers["etag"] == '"v1"'
    assert created.json()["requirement"]["workspaceId"] == WORKSPACE_ID
    assert created.json()["requirement"]["state"] == RequirementState.CREATED.value
    assert created.json()["workItem"]["repositoryState"] == "WAITING_REPOSITORY"
    assert "workspace_id" not in created.text
    requirement_id = created.json()["requirement"]["id"]

    listed = client.get(
        "/api/v1/requirements",
        params={"workspaceId": WORKSPACE_ID, "limit": 20},
    )
    assert listed.status_code == 200
    assert list(listed.json()) == ["items", "nextCursor"]
    assert [item["id"] for item in listed.json()["items"]] == [requirement_id]

    detail = client.get(f"/api/v1/requirements/{requirement_id}")
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"v1"'
    assert detail.json()["requirement"]["id"] == requirement_id
    assert len(detail.json()["workItems"]) == 1
    assert guard.calls == [
        ("requirement.create", WORKSPACE_ID),
        ("requirement.read", WORKSPACE_ID),
        ("requirement.read", WORKSPACE_ID),
    ]


def test_write_preflight_and_strict_browser_dto_return_problem_details(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    client, _holder, _guard, _dependencies = _client(isolated_requirement_database)

    missing_key = client.post(
        "/api/v1/requirements",
        json=_create_body(),
        headers=SAME_ORIGIN,
    )
    forbidden_field = client.post(
        "/api/v1/requirements",
        json={**_create_body(), "state": "READY", "createdBy": "forged"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "requirement-forged-body"},
    )
    invalid_workspace = client.post(
        "/api/v1/requirements",
        json=_create_body(workspace_id="not-a-uuid"),
        headers={**SAME_ORIGIN, "Idempotency-Key": "requirement-invalid-workspace"},
    )
    invalid_requirement_id = client.get("/api/v1/requirements/not-a-uuid")
    invalid_list_workspace = client.get(
        "/api/v1/requirements",
        params={"workspaceId": "not-a-uuid"},
    )
    cross_origin = client.post(
        "/api/v1/requirements",
        json=_create_body(),
        headers={
            "Origin": "https://attacker.example",
            "Idempotency-Key": "requirement-cross-origin",
        },
    )
    valid_requirement_id = "00000000-0000-0000-0000-000000000301"
    missing_if_match = client.post(
        f"/api/v1/requirements/{valid_requirement_id}/sdd-baselines",
        json={"artifactId": "sdd-1", "artifactVersion": "version-1"},
        headers={**SAME_ORIGIN, "Idempotency-Key": "requirement-missing-revision"},
    )
    missing_versioned_key = client.post(
        f"/api/v1/requirements/{valid_requirement_id}/sdd-baselines",
        json={"artifactId": "sdd-1", "artifactVersion": "version-1"},
        headers={**SAME_ORIGIN, "If-Match": '"v1"'},
    )
    invalid_subject_id = client.post(
        f"/api/v1/requirements/{valid_requirement_id}/baseline-confirmations",
        json={"sddBaselineId": "not-a-uuid"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "requirement-invalid-subject",
            "If-Match": '"v1"',
        },
    )

    assert missing_key.status_code == 422
    assert missing_key.headers["content-type"].startswith("application/problem+json")
    assert missing_key.json()["title"] == "Missing Idempotency-Key"
    assert forbidden_field.status_code == 422
    assert forbidden_field.headers["content-type"].startswith("application/problem+json")
    assert forbidden_field.json()["title"] == "Validation failed"
    for response in (
        invalid_workspace,
        invalid_requirement_id,
        invalid_list_workspace,
        missing_if_match,
        missing_versioned_key,
        invalid_subject_id,
    ):
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")
    assert cross_origin.status_code == 403
    assert cross_origin.headers["content-type"].startswith("application/problem+json")


def test_create_http_idempotency_replays_and_rejects_payload_conflicts(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    client, _holder, _guard, _dependencies = _client(isolated_requirement_database)
    headers = {**SAME_ORIGIN, "Idempotency-Key": "requirement-http-replay"}

    first = client.post("/api/v1/requirements", json=_create_body(), headers=headers)
    replay = client.post("/api/v1/requirements", json=_create_body(), headers=headers)
    conflict = client.post(
        "/api/v1/requirements",
        json={**_create_body(), "title": "Different Requirement"},
        headers=headers,
    )

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["etag"] == first.headers["etag"] == '"v1"'
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")
    assert conflict.json()["title"] == "Idempotency conflict"


def test_baseline_registration_confirmation_and_decision_enforce_revision_headers(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    client, holder, _guard, dependencies = _client(isolated_requirement_database)
    created = _create_via_api(client, key="requirement-baseline-create")
    requirement_id = created["requirement"]["id"]
    with isolated_requirement_database.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=requirement_id,
            expected_revision=1,
            actor=holder.value,
            idempotency_key="requirement-baseline-start",
            dependencies=dependencies,
        )

    malformed = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-baselines",
        json={"artifactId": "sdd-1", "artifactVersion": "version-1"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "requirement-baseline-malformed",
            "If-Match": 'W/"v2"',
        },
    )
    assert malformed.status_code == 422
    assert malformed.headers["content-type"].startswith("application/problem+json")

    registered = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-baselines",
        json={"artifactId": "sdd-1", "artifactVersion": "version-1"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "requirement-baseline-register",
            "If-Match": f'"v{prepared.revision}"',
        },
    )
    assert registered.status_code == 201
    assert registered.headers["etag"] == '"v3"'
    assert registered.json()["baseline"]["artifactHash"] == "sha256:sdd-1"

    confirmed = client.post(
        f"/api/v1/requirements/{requirement_id}/baseline-confirmations",
        json={"sddBaselineId": registered.json()["baseline"]["id"]},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "requirement-baseline-confirm",
            "If-Match": registered.headers["etag"],
        },
    )
    assert confirmed.status_code == 201
    assert confirmed.headers["etag"] == '"v4"'
    assert confirmed.json()["assignment"]["currentReviewerId"] == "reviewer-1"

    holder.value = Actor("reviewer-1")
    decided = client.post(
        f"/api/v1/requirements/{requirement_id}/baseline-decisions",
        json={
            "gateId": confirmed.json()["gate"]["id"],
            "outcome": DecisionOutcome.APPROVED.value,
            "reason": "The SDD is executable.",
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "requirement-baseline-decide",
            "If-Match": confirmed.headers["etag"],
        },
    )
    assert decided.status_code == 200
    assert decided.headers["etag"] == '"v5"'
    assert decided.json()["requirement"]["state"] == RequirementState.READY.value
    assert decided.json()["decision"]["reviewerId"] == "reviewer-1"


def test_dependency_unavailable_is_a_503_problem_and_not_a_fake_success(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    dependencies = replace(_gate_dependencies(), artifacts=None)
    client, holder, _guard, _resolved = _client(
        isolated_requirement_database,
        dependencies=dependencies,
    )
    created = _create_via_api(client, key="requirement-unavailable-create")
    requirement_id = created["requirement"]["id"]
    with isolated_requirement_database.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=requirement_id,
            expected_revision=1,
            actor=holder.value,
            idempotency_key="requirement-unavailable-start",
            dependencies=dependencies,
        )

    response = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-baselines",
        json={"artifactId": "sdd-1", "artifactVersion": "version-1"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "requirement-unavailable-register",
            "If-Match": f'"v{prepared.revision}"',
            "X-Request-ID": "req-artifactunavailable",
        },
    )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "title": "Requirement dependency unavailable",
        "status": 503,
        "requestId": "req-artifactunavailable",
    }


def test_unknown_capability_and_cross_workspace_access_are_denied(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    client, _holder, guard, _dependencies = _client(isolated_requirement_database)
    created = _create_via_api(client, key="requirement-scope-create")
    requirement_id = created["requirement"]["id"]
    guard.allowed.clear()

    unknown = client.post(
        "/api/v1/requirements",
        json=_create_body(),
        headers={**SAME_ORIGIN, "Idempotency-Key": "requirement-unknown-capability"},
    )
    cross_workspace = client.get(f"/api/v1/requirements/{requirement_id}")

    for response in (unknown, cross_workspace):
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["title"] == "Forbidden"
    assert guard.calls[-2:] == [
        ("requirement.create", WORKSPACE_ID),
        ("requirement.read", WORKSPACE_ID),
    ]


def _versioned_headers(key: str, revision: int) -> dict[str, str]:
    return {
        **SAME_ORIGIN,
        "Idempotency-Key": key,
        "If-Match": f'"v{revision}"',
    }


def _provider_private_fields(value: object) -> set[str]:
    forbidden = {
        "branch",
        "taskBranch",
        "baseCommitSha",
        "projectId",
        "mrIid",
        "headSha",
        "gitlabProjectId",
    }
    if isinstance(value, dict):
        return (set(value) & forbidden) | {
            item for nested in value.values() for item in _provider_private_fields(nested)
        }
    if isinstance(value, list):
        return {item for nested in value for item in _provider_private_fields(nested)}
    return set()


def test_integration_routes_require_server_capabilities_and_concurrency_headers(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    ready = _ready_requirement(isolated_requirement_database, key_suffix="http-delivery")
    client, _holder, guard, _dependencies = _client(
        isolated_requirement_database,
        allowed={
            ("work_item.execute", WORKSPACE_ID),
            ("merge_request.merge", WORKSPACE_ID),
        },
    )
    requirement_id = ready.requirement.id
    work_item_id = ready.work_items[0].id
    base_url = f"/api/v1/requirements/{requirement_id}/work-items/{work_item_id}"

    started = client.post(
        f"{base_url}:start",
        headers=_versioned_headers("http-start-1", ready.requirement.revision),
    )
    requested_mr = client.post(
        f"{base_url}:request-integration-mr",
        headers=_versioned_headers("http-mr-1", ready.requirement.revision + 1),
    )
    binding_id = "30000000-0000-0000-0000-000000000302"
    with isolated_requirement_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.requirement SET state='VERIFYING', "
                "revision=revision + 1 WHERE id=:requirement_id"
            ),
            {"requirement_id": requirement_id},
        )
        db.execute(
            text(
                "UPDATE requirement.work_item SET state='VERIFYING', "
                "integration_delivery_state='MR_OPEN', "
                "integration_merge_request_binding_id=:binding_id, revision=revision + 1 "
                "WHERE id=:work_item_id"
            ),
            {"binding_id": binding_id, "work_item_id": work_item_id},
        )
    requested_merge = client.post(
        f"{base_url}:request-integration-merge",
        headers=_versioned_headers("http-merge-1", ready.requirement.revision + 3),
    )

    assert started.status_code == 200
    assert started.headers["etag"] == f'"v{ready.requirement.revision + 1}"'
    assert requested_mr.status_code == 202
    assert requested_mr.headers["etag"] == f'"v{ready.requirement.revision + 2}"'
    assert requested_merge.status_code == 202
    assert requested_merge.headers["etag"] == f'"v{ready.requirement.revision + 4}"'
    assert guard.calls == [
        ("work_item.execute", WORKSPACE_ID),
        ("work_item.execute", WORKSPACE_ID),
        ("merge_request.merge", WORKSPACE_ID),
    ]
    for response in (started, requested_mr, requested_merge):
        assert _provider_private_fields(response.json()) == set()


def test_integration_routes_reject_missing_concurrency_headers_and_provider_body(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    client, _holder, _guard, _dependencies = _client(isolated_requirement_database)
    requirement_id = "00000000-0000-0000-0000-000000000301"
    work_item_id = "00000000-0000-0000-0000-000000000302"
    urls = [
        f"/api/v1/requirements/{requirement_id}/work-items/{work_item_id}:start",
        f"/api/v1/requirements/{requirement_id}/work-items/{work_item_id}:request-integration-mr",
        f"/api/v1/requirements/{requirement_id}/work-items/{work_item_id}:request-integration-merge",
    ]

    for index, url in enumerate(urls):
        missing_key = client.post(url, headers={**SAME_ORIGIN, "If-Match": '"v1"'})
        missing_revision = client.post(
            url,
            headers={**SAME_ORIGIN, "Idempotency-Key": f"missing-revision-{index}"},
        )
        forged_provider_body = client.post(
            url,
            json={"branch": "attacker", "mrIid": 7, "headSha": "forged"},
            headers=_versioned_headers(f"forged-provider-{index}", 1),
        )
        for response in (missing_key, missing_revision, forged_provider_body):
            assert response.status_code == 422
            assert response.headers["content-type"].startswith("application/problem+json")


def test_integration_route_hides_cross_requirement_work_item(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    first = _ready_requirement(isolated_requirement_database, key_suffix="http-cross-first")
    second = _ready_requirement(isolated_requirement_database, key_suffix="http-cross-second")
    client, _holder, _guard, _dependencies = _client(
        isolated_requirement_database,
        allowed={("work_item.execute", WORKSPACE_ID)},
    )

    response = client.post(
        f"/api/v1/requirements/{first.requirement.id}/work-items/{second.work_items[0].id}:start",
        headers=_versioned_headers("http-cross-requirement", first.requirement.revision),
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_start_route_reports_current_owner_mismatch_as_an_actor_denial(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    ready = _ready_requirement(isolated_requirement_database, key_suffix="http-owner-denied")
    client, holder, _guard, _dependencies = _client(
        isolated_requirement_database,
        allowed={("work_item.execute", WORKSPACE_ID)},
    )
    holder.value = Actor("employee-2")

    response = client.post(
        f"/api/v1/requirements/{ready.requirement.id}/work-items/{ready.work_items[0].id}:start",
        headers=_versioned_headers("http-owner-denied", ready.requirement.revision),
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "WorkItem actor denied"


def test_merge_route_requires_capability_and_current_owner_together(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    current, _binding_id = _merge_ready_work_item(
        isolated_requirement_database,
        key_suffix="http-merge-non-owner",
    )
    client, holder, guard, _dependencies = _client(
        isolated_requirement_database,
        allowed={("merge_request.merge", WORKSPACE_ID)},
    )
    holder.value = Actor("merge-operator-1")

    response = client.post(
        f"/api/v1/requirements/{current.requirement.id}/work-items/"
        f"{current.work_items[0].id}:request-integration-merge",
        headers=_versioned_headers("http-merge-non-owner", current.requirement.revision),
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "WorkItem actor denied"
    assert guard.calls == [("merge_request.merge", WORKSPACE_ID)]
