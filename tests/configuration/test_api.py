from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.configuration import ConfigurationDependencies
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from control_plane.app.shared.security import SecretMaterial
from tests.configuration.policy_helpers import (
    snapshot_with_idle_minutes,
    temporary_active_snapshot,
)


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


class _FailingAudit:
    def append_in_transaction(self, db: Any, envelope: AuditEnvelope) -> None:
        del db, envelope
        raise RuntimeError("credential-sentinel")


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


@pytest.mark.integration
def test_create_unknown_key_replays_one_value_safe_denial_audit(
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
            lambda _principal, _capability, _scope: None,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    key = f"unknown-create-{uuid4()}"
    headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "Idempotency-Key": key,
    }

    try:
        first = client.post(
            "/api/v1/admin/policies/identity/drafts",
            json={"values": {"identity.unknown": "credential-sentinel"}},
            headers={**headers, "X-Request-ID": "req-createunknownone"},
        )
        replay = client.post(
            "/api/v1/admin/policies/identity/drafts",
            json={"values": {"identity.unknown": "credential-sentinel"}},
            headers={**headers, "X-Request-ID": "req-createunknowntwo"},
        )
        with configuration_owner_engine.connect() as db:
            audits = (
                db.execute(
                    text(
                        "SELECT action, target_type, target_id, reason, correlation_id "
                        "FROM audit.audit_event WHERE actor=:actor "
                        "AND action='configuration.draft.create_denied'"
                    ),
                    {"actor": actor_id},
                )
                .mappings()
                .all()
            )

        assert first.status_code == replay.status_code == 422
        assert first.json()["requestId"] == "req-createunknownone"
        assert replay.json()["requestId"] == "req-createunknowntwo"
        assert [dict(row) for row in audits] == [
            {
                "action": "configuration.draft.create_denied",
                "target_type": "configuration_namespace",
                "target_id": "identity",
                "reason": "namespace=identity; reasonCode=UNREGISTERED_KEY",
                "correlation_id": "req-createunknownone",
            }
        ]
        assert "credential-sentinel" not in audits[0]["reason"]
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor"),
                {"actor": actor_id},
            )


@pytest.mark.integration
def test_update_deterministic_denials_each_append_one_safe_audit(
    configuration_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration.api import (
        ConfigurationHttpRuntime,
        create_configuration_router,
    )

    owner_id = f"admin-{uuid4()}"
    other_id = f"admin-{uuid4()}"
    runtime = ConfigurationHttpRuntime(
        engine=configuration_rw_engine,
        dependencies=_dependencies(),
        secret_manager=_Secrets(),
    )

    def client_for(actor_id: str) -> TestClient:
        app = FastAPI()
        register_problem_handlers(app)
        app.middleware("http")(request_id_middleware)
        app.include_router(
            create_configuration_router(
                lambda: runtime,
                lambda: SimpleNamespace(account_id=actor_id),
                lambda _principal, _capability, _scope: None,
            )
        )
        return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)

    owner_client = client_for(owner_id)
    other_client = client_for(other_id)
    common = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}
    draft_id: str | None = None
    try:
        created = owner_client.post(
            "/api/v1/admin/policies/identity/drafts",
            json={"values": {}},
            headers={**common, "Idempotency-Key": f"create-{uuid4()}"},
        )
        assert created.status_code == 201
        draft_id = created.json()["id"]
        missing_id = str(uuid4())
        cases = [
            (
                owner_client,
                draft_id,
                {"identity.unknown": "credential-sentinel"},
                "req-updateunknown",
                422,
            ),
            (owner_client, missing_id, {}, "req-updatemissing", 404),
            (other_client, draft_id, {}, "req-updateowner", 403),
        ]
        for client, target_id, values, request_id, status in cases:
            key = f"denial-{uuid4()}"
            headers = {
                **common,
                "Idempotency-Key": key,
                "If-Match": '"v1"',
            }
            first = client.patch(
                f"/api/v1/admin/policies/identity/drafts/{target_id}",
                json={"values": values},
                headers={**headers, "X-Request-ID": request_id},
            )
            replay = client.patch(
                f"/api/v1/admin/policies/identity/drafts/{target_id}",
                json={"values": values},
                headers={**headers, "X-Request-ID": f"{request_id}replay"},
            )
            assert first.status_code == replay.status_code == status
            assert first.json()["requestId"] == request_id
            assert replay.json()["requestId"] == f"{request_id}replay"

        with configuration_owner_engine.begin() as db:
            db.execute(
                text(
                    "UPDATE identity.draft SET status='ARCHIVED', "
                    "archived_at=last_meaningful_activity_at "
                    "WHERE id=:draft_id"
                ),
                {"draft_id": draft_id},
            )
        archived_key = f"denial-{uuid4()}"
        archived_headers = {
            **common,
            "Idempotency-Key": archived_key,
            "If-Match": '"v1"',
        }
        archived = owner_client.patch(
            f"/api/v1/admin/policies/identity/drafts/{draft_id}",
            json={"values": {}},
            headers={**archived_headers, "X-Request-ID": "req-updatearchived"},
        )
        archived_replay = owner_client.patch(
            f"/api/v1/admin/policies/identity/drafts/{draft_id}",
            json={"values": {}},
            headers={**archived_headers, "X-Request-ID": "req-updatearchivedreplay"},
        )
        assert archived.status_code == archived_replay.status_code == 409

        with configuration_owner_engine.connect() as db:
            audits = (
                db.execute(
                    text(
                        "SELECT actor, target_id, reason, correlation_id "
                        "FROM audit.audit_event WHERE actor IN (:owner, :other) "
                        "AND action='configuration.draft.update_denied'"
                    ),
                    {"owner": owner_id, "other": other_id},
                )
                .mappings()
                .all()
            )

        assert {
            (row["actor"], row["target_id"], row["reason"], row["correlation_id"]) for row in audits
        } == {
            (
                owner_id,
                draft_id,
                "namespace=identity; reasonCode=UNREGISTERED_KEY",
                "req-updateunknown",
            ),
            (
                owner_id,
                missing_id,
                "namespace=identity; reasonCode=DRAFT_NOT_FOUND",
                "req-updatemissing",
            ),
            (
                other_id,
                draft_id,
                "namespace=identity; reasonCode=DRAFT_OWNER_REQUIRED",
                "req-updateowner",
            ),
            (
                owner_id,
                draft_id,
                "namespace=identity; reasonCode=DRAFT_ARCHIVED",
                "req-updatearchived",
            ),
        }
        assert all("credential-sentinel" not in row["reason"] for row in audits)
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text(
                    "DELETE FROM identity.configuration_idempotency_record "
                    "WHERE actor IN (:owner, :other)"
                ),
                {"owner": owner_id, "other": other_id},
            )
            if draft_id is not None:
                db.execute(text("DELETE FROM identity.draft WHERE id=:id"), {"id": draft_id})


@pytest.mark.integration
def test_create_rolls_back_and_returns_safe_503_for_invalid_active_snapshot(
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
            lambda _principal, _capability, _scope: None,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    snapshot, _snapshot_hash = snapshot_with_idle_minutes(60)
    snapshot["identity.session_idle_timeout"] = "credential-sentinel"
    headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "Idempotency-Key": f"invalid-active-{uuid4()}",
        "X-Request-ID": "req-invalidactive",
    }

    with temporary_active_snapshot(configuration_owner_engine, snapshot):
        response = client.post(
            "/api/v1/admin/policies/identity/drafts",
            json={"values": {}},
            headers=headers,
        )
        with configuration_owner_engine.connect() as db:
            facts = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM identity.draft WHERE owner_id=:actor), "
                    "(SELECT count(*) FROM identity.configuration_idempotency_record "
                    " WHERE actor=:actor), "
                    "(SELECT count(*) FROM audit.audit_event WHERE actor=:actor)"
                ),
                {"actor": actor_id},
            ).one()

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "title": "Effective policy unavailable",
        "status": 503,
        "requestId": "req-invalidactive",
    }
    assert facts == (0, 0, 0)
    assert "credential-sentinel" not in response.text


@pytest.mark.integration
def test_create_rolls_back_all_facts_when_audit_append_fails(
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
    runtime = ConfigurationHttpRuntime(
        engine=configuration_rw_engine,
        dependencies=ConfigurationDependencies(
            clock=_Clock(),
            random=_Random(),
            audit=_FailingAudit(),
        ),
        secret_manager=_Secrets(),
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_configuration_router(
            lambda: runtime,
            lambda: SimpleNamespace(account_id=actor_id),
            lambda _principal, _capability, _scope: None,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)

    response = client.post(
        "/api/v1/admin/policies/identity/drafts",
        json={"values": {}},
        headers={
            "Origin": "https://testserver",
            "Sec-Fetch-Site": "same-origin",
            "Idempotency-Key": f"failing-audit-{uuid4()}",
            "X-Request-ID": "req-failingaudit",
        },
    )
    with configuration_owner_engine.connect() as db:
        facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM identity.draft WHERE owner_id=:actor), "
                "(SELECT count(*) FROM identity.configuration_idempotency_record "
                " WHERE actor=:actor), "
                "(SELECT count(*) FROM audit.audit_event WHERE actor=:actor)"
            ),
            {"actor": actor_id},
        ).one()

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "title": "Internal server error",
        "status": 500,
        "requestId": "req-failingaudit",
    }
    assert facts == (0, 0, 0)
    assert "credential-sentinel" not in response.text
