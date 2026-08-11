from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.configuration import ConfigurationDependencies
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from control_plane.app.shared.security import SecretMaterial


@dataclass
class _Clock:
    value: datetime = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class _Random:
    def uuid4(self) -> object:
        return uuid4()


class _Secrets:
    def load(self) -> SecretMaterial:
        return SecretMaterial(b"p" * 32, b"t" * 32, b"i" * 32)


def _dependencies() -> ConfigurationDependencies:
    return ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )


def test_configuration_http_contract_has_exact_operations_security_and_preflight() -> None:
    from control_plane.app.bootstrap.app import create_app

    schema = create_app().openapi()
    operations = {
        ("/api/v1/admin/policies", "get"): "policies_catalog",
        ("/api/v1/admin/policies/{namespace}/drafts", "post"): "draft_create",
        (
            "/api/v1/admin/policies/{namespace}/drafts/{draft_id}",
            "patch",
        ): "draft_update",
        (
            "/api/v1/admin/policies/{namespace}/drafts/{draft_id}/validate",
            "post",
        ): "draft_validate",
    }
    for (path, method), operation_id in operations.items():
        operation = schema["paths"][path][method]
        assert operation["operationId"] == operation_id
        assert operation["security"]
        expected_success = "201" if operation_id == "draft_create" else "200"
        assert set(operation["responses"]) >= {
            expected_success,
            "401",
            "403",
            "409",
            "422",
            "500",
            "503",
        }

    create = schema["paths"]["/api/v1/admin/policies/{namespace}/drafts"]["post"]
    update = schema["paths"]["/api/v1/admin/policies/{namespace}/drafts/{draft_id}"]["patch"]
    validate = schema["paths"]["/api/v1/admin/policies/{namespace}/drafts/{draft_id}/validate"][
        "post"
    ]
    create_parameters = {value["name"]: value for value in create["parameters"]}
    assert create_parameters["Idempotency-Key"]["required"] is True
    for operation in (update, validate):
        parameters = {value["name"]: value for value in operation["parameters"]}
        assert parameters["Idempotency-Key"]["required"] is True
        assert parameters["If-Match"]["required"] is True
    assert create["responses"]["201"]["headers"]["ETag"]["schema"]["type"] == "string"
    for operation in (update, validate):
        assert operation["responses"]["200"]["headers"]["ETag"]["schema"]["type"] == "string"


@pytest.mark.integration
def test_draft_api_enforces_etags_durable_replay_validation_and_current_request_id(
    configuration_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration.api import (
        ConfigurationHttpRuntime,
        create_configuration_router,
    )

    actor_id = f"admin-{uuid4()}"
    guarded: list[tuple[str, str | None]] = []
    runtime = ConfigurationHttpRuntime(
        engine=configuration_rw_engine,
        dependencies=_dependencies(),
        secret_manager=_Secrets(),
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_configuration_router(
            lambda: runtime,
            lambda: SimpleNamespace(account_id=actor_id),
            lambda _principal, capability, scope: guarded.append((capability, scope)),
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    same_origin = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}

    created = client.post(
        "/api/v1/admin/policies/identity/drafts",
        json={"values": {"identity.session_idle_timeout": 45}},
        headers={**same_origin, "Idempotency-Key": f"create-{uuid4()}"},
    )
    draft_id = created.json()["id"]
    assert created.status_code == 201
    assert created.headers["etag"] == '"v1"'
    assert created.json()["baseVersion"] == 1

    update_key = f"update-{uuid4()}"
    updated = client.patch(
        f"/api/v1/admin/policies/identity/drafts/{draft_id}",
        json={"values": {"identity.session_idle_timeout": 10}},
        headers={
            **same_origin,
            "Idempotency-Key": update_key,
            "If-Match": '"v1"',
        },
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"v2"'

    stale_key = f"stale-{uuid4()}"
    stale_headers = {
        **same_origin,
        "Idempotency-Key": stale_key,
        "If-Match": '"v1"',
        "X-Request-ID": "req-stalefirst",
    }
    stale_first = client.patch(
        f"/api/v1/admin/policies/identity/drafts/{draft_id}",
        json={"values": {"identity.session_idle_timeout": 20}},
        headers=stale_headers,
    )
    stale_replay = client.patch(
        f"/api/v1/admin/policies/identity/drafts/{draft_id}",
        json={"values": {"identity.session_idle_timeout": 20}},
        headers={**stale_headers, "X-Request-ID": "req-stalereplay"},
    )
    assert stale_first.status_code == stale_replay.status_code == 409
    assert stale_first.json()["requestId"] == "req-stalefirst"
    assert stale_replay.json()["requestId"] == "req-stalereplay"

    validated = client.post(
        f"/api/v1/admin/policies/identity/drafts/{draft_id}/validate",
        json={},
        headers={
            **same_origin,
            "Idempotency-Key": f"validate-{uuid4()}",
            "If-Match": '"v2"',
        },
    )
    assert validated.status_code == 200
    assert validated.headers["etag"] == '"v3"'
    assert validated.json()["valid"] is False
    assert validated.json()["issues"] == [
        {
            "code": "BELOW_MINIMUM",
            "key": "identity.session_idle_timeout",
            "message": "Value is below the permitted minimum.",
        }
    ]
    assert all(item == ("platform.configuration.manage", None) for item in guarded)

    with configuration_owner_engine.connect() as db:
        claims, stale_audits = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM identity.configuration_idempotency_record "
                " WHERE actor=:actor), "
                "(SELECT count(*) FROM audit.audit_event "
                " WHERE actor=:actor AND target_id=:draft_id "
                " AND action='configuration.draft.update_denied')"
            ),
            {"actor": actor_id, "draft_id": draft_id},
        ).one()
    assert claims == 4
    assert stale_audits == 1

    with configuration_owner_engine.begin() as db:
        db.execute(
            text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor"),
            {"actor": actor_id},
        )
        db.execute(text("DELETE FROM identity.draft WHERE id=:id"), {"id": draft_id})


def test_configuration_writes_reject_cross_site_before_database_or_guard() -> None:
    from control_plane.app.bootstrap.app import create_app

    client = TestClient(create_app(), base_url="https://testserver")
    response = client.post(
        "/api/v1/admin/policies/identity/drafts",
        json={"values": {}},
        headers={
            "Idempotency-Key": "cross-site-config-1",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
            "X-Request-ID": "req-configcsrf",
        },
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["requestId"] == "req-configcsrf"
