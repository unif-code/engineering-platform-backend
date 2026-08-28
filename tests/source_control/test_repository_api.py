from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.api import (
    SourceControlQueryRuntime,
    create_repository_query_router,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware

WORKSPACE_ID = "20000000-0000-0000-0000-000000000401"
OTHER_WORKSPACE_ID = "20000000-0000-0000-0000-000000000402"


@dataclass(frozen=True, slots=True)
class Principal:
    account_id: str


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
    engine: Engine,
    *,
    guard: CapabilityGuard,
    runtime_provider: Any | None = None,
) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_repository_query_router(
            runtime_provider
            or (
                lambda: SourceControlQueryRuntime(
                    engine=engine,
                    dependencies=SimpleNamespace(  # type: ignore[arg-type]
                        repository_factory=SqlAlchemySourceControlRepository
                    ),
                )
            ),
            lambda: Principal("employee-1"),
            guard,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _insert_repository(
    engine: Engine,
    *,
    repository_id: str,
    workspace_id: str,
    project_id: str,
    project_path: str,
) -> None:
    with engine.begin() as db:
        SqlAlchemySourceControlRepository(db).insert_workspace_repository(
            id=repository_id,
            workspace_id=workspace_id,
            provider="GITLAB",
            project_id=project_id,
            project_path=project_path,
            default_branch="main",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref="secret-ref:webhook",
            status="AUTHORIZED",
            revision=1,
            now=datetime(2026, 8, 28, tzinfo=UTC),
        )


def test_repository_choice_query_is_scoped_and_safe(
    isolated_source_control_rw_engine: Engine,
) -> None:
    _insert_repository(
        isolated_source_control_rw_engine,
        repository_id="10000000-0000-0000-0000-000000000401",
        workspace_id=WORKSPACE_ID,
        project_id="101",
        project_path="platform/backend",
    )
    _insert_repository(
        isolated_source_control_rw_engine,
        repository_id="10000000-0000-0000-0000-000000000402",
        workspace_id=OTHER_WORKSPACE_ID,
        project_id="102",
        project_path="platform/other",
    )
    guard = CapabilityGuard({("requirement.create", WORKSPACE_ID)})
    client = _client(isolated_source_control_rw_engine, guard=guard)

    response = client.get(f"/api/v1/workspaces/{WORKSPACE_ID}/repositories")
    cross_workspace = client.get(f"/api/v1/workspaces/{OTHER_WORKSPACE_ID}/repositories")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "repositoryId": "10000000-0000-0000-0000-000000000401",
                "provider": "GITLAB",
                "projectPath": "platform/backend",
                "defaultBranch": "main",
            }
        ]
    }
    assert all(
        name not in response.text
        for name in (
            "connectionRef",
            "credentialSecretRef",
            "webhookSigningSecretRef",
            "effect",
        )
    )
    assert cross_workspace.status_code == 403
    assert guard.calls == [
        ("requirement.create", WORKSPACE_ID),
        ("requirement.create", OTHER_WORKSPACE_ID),
    ]


def test_repository_choice_query_fails_closed_when_dependency_is_unavailable(
    isolated_source_control_rw_engine: Engine,
) -> None:
    guard = CapabilityGuard({("requirement.create", WORKSPACE_ID)})

    def unavailable_runtime() -> SourceControlQueryRuntime:
        raise SQLAlchemyError("unavailable")

    response = _client(
        isolated_source_control_rw_engine,
        guard=guard,
        runtime_provider=unavailable_runtime,
    ).get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/repositories",
        headers={"X-Request-ID": "req-sourcecontrolunavailable"},
    )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "title": "Source Control repository query unavailable",
        "status": 503,
        "requestId": "req-sourcecontrolunavailable",
    }


def test_default_repository_choice_route_requires_a_session() -> None:
    app = create_app()
    operation = app.openapi()["paths"]["/api/v1/workspaces/{workspaceId}/repositories"]["get"]
    response = TestClient(app, raise_server_exceptions=False).get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/repositories"
    )

    assert operation["security"] == [{"EpSessionCookie": []}]
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
