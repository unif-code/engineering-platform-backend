import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.audit.adapters.sqlalchemy_repository import (
    SqlAlchemyAuditEventRepository,
)
from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.audit.api import create_audit_router
from control_plane.app.modules.audit.api import routes as audit_routes
from control_plane.app.modules.audit.api.routes import AUDIT_READ_CAPABILITY
from control_plane.app.modules.authorization import (
    Scope,
    bump_version,
    grant,
)
from control_plane.app.modules.authorization.adapters import (
    SqlAlchemyIdentitySessionValidator,
)
from control_plane.app.modules.authorization.api import (
    AuthorizationHttpRuntime,
)
from control_plane.app.modules.authorization.api.dependencies import require_capability
from control_plane.app.modules.authorization.application import DecisionDependencies
from control_plane.app.modules.identity import SessionKind, SessionPrincipal
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.authorization.helpers import authorization_dependencies
from tests.identity.task5_helpers import dependencies as identity_dependencies
from tests.identity.test_auth_flow import _initialize_account

pytestmark = pytest.mark.integration


def test_audit_query_contract_is_registered_with_platform_filters() -> None:
    operation = create_app().openapi()["paths"]["/api/v1/admin/audit-events"]["get"]

    assert operation["operationId"] == "audit_events_list"
    assert {parameter["name"] for parameter in operation["parameters"]} >= {
        "actor",
        "targetType",
        "targetId",
        "from",
        "to",
        "requestId",
        "cursor",
        "limit",
    }


def _client(engine: Engine) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_audit_router(
            lambda: engine,
            Depends(lambda: object()),
        )
    )
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def test_filters_and_stable_cursor_cover_three_pages_without_gaps(
    owner_engine: Engine,
    rw_engine: Engine,
) -> None:
    base = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    rows = [
        {
            "id": f"event-{index}",
            "occurred_at": base + timedelta(seconds=index // 2),
            "actor": "alice" if index < 6 else "bob",
            "actor_type": "HUMAN",
            "action": "test.query",
            "target_type": "ACCOUNT",
            "target_id": f"account-{index}",
            "result": "OK",
            "reason": None,
            "correlation_id": f"correlation-{index}",
            "schema_version": 1,
            "request_id": "req-three-pages",
        }
        for index in range(7)
    ]
    with owner_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO audit.audit_event "
                "(id,occurred_at,actor,actor_type,action,target_type,target_id,result,reason,"
                "correlation_id,schema_version,request_id) VALUES "
                "(:id,:occurred_at,:actor,:actor_type,:action,:target_type,:target_id,:result,"
                ":reason,:correlation_id,:schema_version,:request_id)"
            ),
            rows,
        )

    client = _client(rw_engine)
    ids: list[str] = []
    cursor = None
    for _ in range(3):
        response = client.get(
            "/api/v1/admin/audit-events",
            params={
                "actor": "alice",
                "targetType": "ACCOUNT",
                "from": base.isoformat(),
                "to": (base + timedelta(seconds=10)).isoformat(),
                "requestId": "req-three-pages",
                "limit": 2,
                **({"cursor": cursor} if cursor is not None else {}),
            },
        )
        assert response.status_code == 200
        ids.extend(item["id"] for item in response.json()["items"])
        cursor = response.json()["nextCursor"]

    assert ids == [f"event-{index}" for index in range(6)]
    assert len(ids) == len(set(ids))
    assert cursor is None


@pytest.mark.parametrize(
    "malformed",
    [base64.urlsafe_b64encode(json.dumps(["bad"]).encode()).decode(), "a"],
)
def test_malformed_cursor_is_validation_problem_with_current_request_id(
    rw_engine: Engine,
    malformed: str,
) -> None:
    response = _client(rw_engine).get(
        "/api/v1/admin/audit-events",
        params={"cursor": malformed},
        headers={"X-Request-ID": "req-invalidcursor"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["requestId"] == "req-invalidcursor"


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-08-12T00:00:00"},
        {"from": "2026-08-12T00:00:00Z", "to": "2026-08-12T00:00:00Z"},
    ],
)
def test_invalid_time_bounds_are_validation_problems(
    rw_engine: Engine,
    params: dict[str, str],
) -> None:
    response = _client(rw_engine).get("/api/v1/admin/audit-events", params=params)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_query_store_failure_is_safe_service_unavailable_problem(
    rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise SQLAlchemyError("secret database failure")

    monkeypatch.setattr(audit_routes, "list_events", unavailable)
    response = _client(rw_engine).get(
        "/api/v1/admin/audit-events",
        headers={"X-Request-ID": "req-auditunavailable"},
    )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "title": "Audit unavailable",
        "status": 503,
        "requestId": "req-auditunavailable",
    }


class _AlwaysMember:
    def is_formal_member(self, workspace_id: str, account_id: str) -> bool:
        del workspace_id, account_id
        return True


def test_real_session_and_platform_capability_guard_query_without_meaningful_activity(
    owner_engine: Engine,
    rw_engine: Engine,
    identity_rw_engine: Engine,
    authorization_rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secret, token = _initialize_account(
        identity_rw_engine,
        identity_dependencies(),
        monkeypatch,
    )
    with owner_engine.connect() as db:
        account_id, before_last_seen = db.execute(
            text(
                "SELECT a.id, s.last_seen_at FROM identity.account a "
                "JOIN identity.session s ON s.account_id=a.id WHERE s.kind='FULL'"
            )
        ).one()
    runtime = AuthorizationHttpRuntime(
        engine=authorization_rw_engine,
        dependencies=authorization_dependencies(),
        decision_dependencies=DecisionDependencies(
            identity=SqlAlchemyIdentitySessionValidator(
                identity_rw_engine,
                identity_dependencies(),
            ),
            workspace=_AlwaysMember(),
        ),
        organization_summary=lambda _account_id: None,
        workspace_summaries=lambda _account_id: [],
    )
    with authorization_rw_engine.begin() as db:
        bump_version(
            db,
            account_id=str(account_id),
            dependencies=runtime.dependencies,
        )

    capability_dependency = require_capability(
        AUDIT_READ_CAPABILITY,
        runtime_provider=lambda: runtime,
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_audit_router(
            lambda: rw_engine,
            capability_dependency,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)

    assert client.get("/api/v1/admin/audit-events").status_code == 401
    client.cookies.set("ep_session", token)
    denied = client.get(
        "/api/v1/admin/audit-events",
        headers={"X-Request-ID": "req-auditdenied"},
    )
    assert denied.status_code == 403
    assert denied.json()["requestId"] == "req-auditdenied"

    with authorization_rw_engine.begin() as db:
        grant(
            db,
            principal_id=str(account_id),
            capability=AUDIT_READ_CAPABILITY,
            scope=Scope.platform(),
            actor=SessionPrincipal(
                account_id=str(account_id),
                employee_no="00000001",
                display_name="Alice",
                session_kind=SessionKind.FULL,
                is_super_admin=False,
            ),
            reason="audit reader",
            dependencies=authorization_dependencies(),
        )
    with owner_engine.connect() as db:
        audit_count = db.execute(text("SELECT count(*) FROM audit.audit_event")).scalar_one()
    allowed = client.get("/api/v1/admin/audit-events")
    assert allowed.status_code == 200
    with owner_engine.connect() as db:
        after_last_seen = db.execute(
            text("SELECT last_seen_at FROM identity.session WHERE kind='FULL'")
        ).scalar_one()
        after_audit_count = db.execute(text("SELECT count(*) FROM audit.audit_event")).scalar_one()
    assert after_last_seen == before_last_seen
    assert after_audit_count == audit_count


def test_transactional_append_captures_current_request_id_and_can_be_filtered(
    owner_engine: Engine,
    identity_rw_engine: Engine,
    rw_engine: Engine,
) -> None:
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)

    @app.post("/write")
    def write() -> dict[str, str]:
        with identity_rw_engine.begin() as db:
            record_in_transaction(
                db,
                AuditEnvelope(
                    id="request-correlation-event",
                    actor="00000001",
                    actor_type="HUMAN",
                    action="test.request.correlation",
                    target_type="ACCOUNT",
                    target_id="account-1",
                    result="OK",
                    correlation_id="domain-correlation",
                ),
                SqlAlchemyTransactionalAuditAppender(),
            )
        return {"status": "ok"}

    response = TestClient(app).post(
        "/write",
        headers={"X-Request-ID": "req-appendcapture"},
    )
    assert response.status_code == 200
    with owner_engine.connect() as db:
        request_id = db.execute(
            text("SELECT request_id FROM audit.audit_event WHERE id='request-correlation-event'")
        ).scalar_one()
    assert request_id == "req-appendcapture"

    query = _client(rw_engine).get(
        "/api/v1/admin/audit-events",
        params={"requestId": "req-appendcapture"},
    )
    assert query.status_code == 200
    assert [item["id"] for item in query.json()["items"]] == ["request-correlation-event"]


def test_direct_audit_append_captures_server_request_context(rw_engine: Engine) -> None:
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)

    @app.post("/write")
    def write() -> dict[str, str]:
        SqlAlchemyAuditEventRepository(rw_engine).append(
            AuditEnvelope(
                id="direct-request-correlation-event",
                actor="SYSTEM",
                actor_type="SYSTEM",
                action="test.direct.request.correlation",
                target_type="TEST",
                target_id="direct-1",
                result="OK",
                correlation_id="direct-correlation",
                request_id="untrusted-caller-value",
            )
        )
        return {"status": "ok"}

    response = TestClient(app).post(
        "/write",
        headers={"X-Request-ID": "req-directcapture"},
    )
    assert response.status_code == 200
    query = _client(rw_engine).get(
        "/api/v1/admin/audit-events",
        params={"requestId": "req-directcapture", "targetId": "direct-1"},
    )
    assert query.status_code == 200
    assert [item["id"] for item in query.json()["items"]] == ["direct-request-correlation-event"]


def test_migration_keeps_audit_rw_append_only_and_domain_roles_function_only(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as db:
        audit_privileges = {
            privilege: db.execute(
                text("SELECT has_table_privilege('audit_rw','audit.audit_event',:p)"),
                {"p": privilege},
            ).scalar_one()
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        domain_table_access = {
            role: {
                privilege: db.execute(
                    text("SELECT has_table_privilege(:role,'audit.audit_event',:p)"),
                    {"role": role, "p": privilege},
                ).scalar_one()
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
            for role in (
                "identity_rw",
                "organization_rw",
                "workspace_rw",
                "authorization_rw",
                "configuration_rw",
            )
        }
        function_access = {
            role: db.execute(
                text(
                    "SELECT has_function_privilege(:role, "
                    "'audit.append_event(text,timestamptz,text,text,text,text,text,text,"
                    "text,text,integer)', "
                    "'EXECUTE')"
                ),
                {"role": role},
            ).scalar_one()
            for role in (
                "identity_rw",
                "organization_rw",
                "workspace_rw",
                "authorization_rw",
                "configuration_rw",
            )
        }

    assert audit_privileges == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False}
    assert all(not any(privileges.values()) for privileges in domain_table_access.values())
    assert all(function_access.values())


def test_migration_adds_nullable_request_id_and_query_indexes(owner_engine: Engine) -> None:
    with owner_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO audit.audit_event "
                "(id,occurred_at,actor,actor_type,action,target_type,target_id,result,reason,"
                "correlation_id,schema_version) VALUES "
                "('legacy-event',now(),'SYSTEM','SYSTEM','legacy','TEST','legacy','OK',NULL,"
                "'legacy-correlation',1)"
            )
        )
        nullable, default = db.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema='audit' AND table_name='audit_event' "
                "AND column_name='request_id'"
            )
        ).one()
        indexes = set(
            db.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='audit' "
                    "AND tablename='audit_event'"
                )
            ).scalars()
        )
        legacy_request_id = db.execute(
            text("SELECT request_id FROM audit.audit_event WHERE id='legacy-event'")
        ).scalar_one()

    assert nullable == "YES"
    assert default is None
    assert legacy_request_id is None
    assert indexes >= {
        "ix_audit_event_occurred_id",
        "ix_audit_event_request_occurred_id",
    }
