from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch
from uuid import uuid4

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.configuration import PolicyLifecycle, PolicyRuntimeRegistry
from control_plane.app.modules.configuration.adapters.identity import IdentityPolicyOwner
from control_plane.app.modules.identity.adapters.runtime import SystemClock
from control_plane.app.modules.requirement.adapters.gate_policy import (
    SqlAlchemyGatePolicyRepository,
)
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_control import assert_postgres_blocked_by
from tests.source_control.test_v06_production_e2e import Journey, _grant, _write
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database
from tests.test_e2e_access_governance import SAME_ORIGIN, _initialize

pytestmark = pytest.mark.integration


def account_etag(client: TestClient, account_id: str) -> str:
    response = client.get("/api/v1/admin/accounts", params={"limit": 100})
    assert response.status_code == 200, response.text
    assert response.json()["nextCursor"] is None
    etag = next(row["etag"] for row in response.json()["items"] if row["id"] == account_id)
    assert isinstance(etag, str)
    return etag


def otp(state: Any, secret: str) -> str:
    state.clock.value += timedelta(seconds=30)
    return pyotp.TOTP(secret).at(state.clock.value)


@pytest.fixture
def actors(journey: Journey, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    with ExitStack() as stack:
        others = []
        for index in (5, 6):
            employee_no = f"{index:08}"
            created = _write(
                journey.admin,
                "/api/v1/admin/accounts",
                {
                    "employeeNo": employee_no,
                    "displayName": f"Policy Admin {index}",
                    "profession": "BACKEND",
                    "reason": "Synthetic takeover administrator",
                },
                status=201,
            ).json()
            client = stack.enter_context(
                TestClient(bootstrap.create_app(), base_url="https://testserver")
            )
            password = f"Takeover!Password#{index:04}"
            secret = _initialize(
                client,
                employee_no=employee_no,
                temporary_password=created["temporaryPassword"],
                password=password,
                key=f"takeover-init-{index}",
                request_ids={
                    name: f"req-takeover-{index}-{name}"
                    for name in ("login", "password", "enroll", "confirm")
                },
            )
            others.append(
                SimpleNamespace(
                    id=created["account"]["id"],
                    client=client,
                    secret=secret,
                    password=password,
                    employee_no=employee_no,
                )
            )
        clock = SimpleNamespace(value=datetime.now(UTC))
        monkeypatch.setattr(SystemClock, "now", lambda _self: clock.value)
        monkeypatch.setattr(
            "control_plane.app.shared.security.totp.time.time", lambda: clock.value.timestamp()
        )
        state = SimpleNamespace(
            journey=journey,
            clock=clock,
            a=SimpleNamespace(
                id=journey.admin.get("/api/v1/me").json()["accountId"],
                client=journey.admin,
                secret=journey.admin_totp,
            ),
            b=others[0],
            c=others[1],
        )
        for actor in others:
            _write(
                journey.admin,
                "/api/v1/admin/super-admins",
                {
                    "accountId": actor.id,
                    "reason": "Policy takeover responsibility",
                    "totpCode": otp(state, journey.admin_totp),
                },
                etag=account_etag(journey.admin, actor.id),
            )
            actor.client.cookies.clear()
            response = actor.client.post(
                "/api/v1/auth/login",
                json={"employeeNo": actor.employee_no, "password": actor.password},
                headers={**SAME_ORIGIN, "Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 200 and response.json()["state"] == "TOTP_REQUIRED", (
                response.text
            )
            authenticated = actor.client.post(
                "/api/v1/auth/totp",
                json={
                    "challengeToken": response.json()["challengeToken"],
                    "code": otp(state, actor.secret),
                },
                headers={**SAME_ORIGIN, "Idempotency-Key": str(uuid4())},
            )
            assert authenticated.status_code == 200, authenticated.text
            assert actor.client.get("/api/v1/me").json()["isSuperAdmin"] is True
        yield state


def prepare(state: Any, namespace: str, actor: Any = None) -> tuple[str, Any]:
    actor = state.a if actor is None else actor
    values = (
        {"identity.session_cap": 4} if namespace == "identity" else {"draft_archive_after_days": 7}
    )
    created = _write(
        actor.client, f"/api/v1/admin/policies/{namespace}/drafts", {"values": values}, status=201
    )
    path = f"/api/v1/admin/policies/{namespace}/drafts/{created.json()['id']}"
    validated = _write(actor.client, path + "/validate", {}, etag=created.headers["etag"])
    assert validated.json()["valid"] is True
    preview = actor.client.get(path + "/preview", headers={"If-Match": validated.headers["etag"]})
    assert preview.status_code == 200, preview.text
    return path, actor.client.get(path)


def takeover(
    state: Any,
    actor: Any,
    path: str,
    etag: str,
    *,
    key: str | None = None,
    status: int = 200,
    reason: str = "  原因原文\n承接草稿  ",
) -> Any:
    state.clock.value += timedelta(seconds=1)
    return _write(
        actor.client, path + "/takeover", {"reason": reason}, etag=etag, key=key, status=status
    )


def policy_facts(state: Any) -> Any:
    with state.journey.database.owner.connect() as db:
        tables = (
            "identity.draft",
            "identity.version",
            "identity.active_pointer",
            "identity.configuration_idempotency_record",
            "identity.configuration_outbox",
            "requirement.gate_policy_draft",
            "requirement.gate_policy_version",
            "requirement.gate_policy_active_pointer",
            "requirement.gate_policy_idempotency",
            "requirement.gate_policy_outbox",
            "requirement.gate_policy_receipt",
            "identity.policy_reauth_consumption",
        )
        result = {
            name: db.execute(text(f"SELECT to_jsonb(t) FROM {name} t ORDER BY 1")).scalars().all()
            for name in tables
        }
        result["audit"] = (
            db.execute(
                text(
                    "SELECT to_jsonb(t) FROM audit.audit_event t "
                    "WHERE action LIKE 'configuration.%' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
        result["totp"] = list(
            db.execute(text("SELECT id,totp_last_step FROM identity.account ORDER BY id"))
        )
        return result


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_two_real_admins_read_takeover_clear_bindings_and_replay_after_owner_and_archive_changes(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, original = prepare(state, namespace)
    before = policy_facts(state)
    read = state.b.client.get(path)
    assert read.status_code == 200 and read.content == original.content
    assert (
        read.headers["etag"] == original.headers["etag"]
        and read.headers["cache-control"] == "no-store"
    )
    assert policy_facts(state) == before
    accepted = takeover(state, state.b, path, original.headers["etag"], key="takeover-b")
    value = accepted.json()
    assert value["ownerId"] == state.b.id and value["revision"] == original.json()["revision"] + 1
    assert value["lastMeaningfulActivityAt"] != original.json()["lastMeaningfulActivityAt"]
    assert value["validationEvidence"] is None and value["previewEvidence"] is None
    for key in (
        "id",
        "namespace",
        "scope",
        "content",
        "contentHash",
        "schemaRevision",
        "baseVersion",
        "stale",
        "archivedAt",
        "rollbackFromVersion",
    ):
        assert value[key] == original.json()[key]
    table = "identity.draft" if namespace == "identity" else "requirement.gate_policy_draft"
    with state.journey.database.owner.connect() as db:
        row = (
            db.execute(text(f"SELECT * FROM {table} WHERE id=:id"), {"id": value["id"]})
            .mappings()
            .one()
        )
        assert all(
            item is None for key, item in row.items() if key.startswith(("validation_", "preview_"))
        )
    after = policy_facts(state)
    assert after["totp"] == before["totp"]
    assert (
        after["identity.policy_reauth_consumption"] == before["identity.policy_reauth_consumption"]
    )
    for action in ("update", "validate", "preview", "publish"):
        for etag, status in ((original.headers["etag"], 409), (accepted.headers["etag"], 403)):
            if action == "preview":
                response = state.a.client.get(path + "/preview", headers={"If-Match": etag})
                assert response.status_code == status, response.text
            else:
                suffix = "" if action == "update" else f"/{action}"
                body: dict[str, Any] = (
                    {"values": {}}
                    if action == "update"
                    else {"reason": "Old publish context", "totpCode": "000000"}
                    if action == "publish"
                    else {}
                )
                _write(
                    state.a.client,
                    path + suffix,
                    body,
                    etag=etag,
                    method="PATCH" if action == "update" else "POST",
                    status=status,
                )
    unchanged = state.b.client.get(path)
    takeover(state, state.b, path, unchanged.headers["etag"], status=409)
    back = takeover(state, state.a, path, unchanged.headers["etag"])
    before_replay = policy_facts(state)
    replay = takeover(state, state.b, path, original.headers["etag"], key="takeover-b")
    assert replay.content == accepted.content and replay.headers["etag"] == accepted.headers["etag"]
    assert policy_facts(state) == before_replay
    assert state.b.client.get(path).json()["ownerId"] == state.a.id
    runtime = bootstrap.configuration_http_runtime().owners.resolve(namespace)
    assert runtime.archive(now=state.clock.value + timedelta(days=31)) >= 1
    archived = state.b.client.get(path)
    assert archived.status_code == 200 and archived.json()["status"] == "ARCHIVED"
    takeover(state, state.b, path, archived.headers["etag"], status=409)
    replay = takeover(state, state.b, path, original.headers["etag"], key="takeover-b")
    assert replay.content == accepted.content and replay.headers["etag"] == accepted.headers["etag"]
    assert back.json()["ownerId"] == state.a.id


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_new_owner_revalidates_previews_and_publishes_while_stale_draft_takeover_does_not_rebase(
    actors: Any, namespace: str
) -> None:
    state = actors
    sibling = _write(
        state.a.client, f"/api/v1/admin/policies/{namespace}/drafts", {"values": {}}, status=201
    )
    sibling_path = f"/api/v1/admin/policies/{namespace}/drafts/{sibling.json()['id']}"
    path, original = prepare(state, namespace)
    taken = takeover(state, state.b, path, original.headers["etag"])
    validated = _write(state.b.client, path + "/validate", {}, etag=taken.headers["etag"])
    assert validated.json()["valid"] is True
    preview = state.b.client.get(path + "/preview", headers={"If-Match": validated.headers["etag"]})
    assert preview.status_code == 200, preview.text
    published = _write(
        state.b.client,
        path + "/publish",
        {"reason": "New owner completed fresh checks", "totpCode": otp(state, state.b.secret)},
        etag=preview.headers["etag"],
        status=201,
    )
    assert published.json()["version"] == 2
    after_publish = state.c.client.get(path)
    transferred = takeover(state, state.c, path, after_publish.headers["etag"])
    assert transferred.json()["baseVersion"] == taken.json()["baseVersion"]
    stale = state.b.client.get(sibling_path)
    assert stale.status_code == 200 and stale.json()["stale"] is True
    claimed = takeover(state, state.b, sibling_path, stale.headers["etag"])
    assert claimed.json()["baseVersion"] == 1 and claimed.json()["stale"] is True
    _write(
        state.b.client,
        sibling_path,
        {"values": {}},
        etag=claimed.headers["etag"],
        method="PATCH",
        status=409,
    )
    _write(
        state.b.client,
        sibling_path + "/publish",
        {"reason": "No implicit rebase", "totpCode": "000000"},
        etag=claimed.headers["etag"],
        status=409,
    )


@contextmanager
def injected_client(actor: Any, runtime: Any) -> Iterator[TestClient]:
    with patch.object(bootstrap, "configuration_http_runtime", lambda: runtime):
        app = bootstrap.create_app()
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        client.cookies.update(actor.client.cookies)
        yield client


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_original_owner_disable_does_not_block_takeover_and_current_actor_revocation_blocks_replay(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, original = prepare(state, namespace)
    runtime = bootstrap.configuration_http_runtime()
    denied = Mock(side_effect=AssertionError("denied request cannot read owner"))
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve = denied
    trap = replace(runtime, owners=owners)
    for scope in (None, state.journey.workspace_id):
        _grant(state.a.client, state.journey.member_id, "platform.configuration.manage", scope)
    with injected_client(SimpleNamespace(client=state.journey.member), trap) as client:
        assert client.get(path).status_code == 403
        _write(
            client,
            path + "/takeover",
            {"reason": "ordinary Grant is not Super Admin"},
            etag=original.headers["etag"],
            status=403,
        )
        client.cookies.clear()
        assert client.get(path).status_code == 401
    denied.assert_not_called()
    _write(
        state.b.client,
        f"/api/v1/admin/accounts/{state.a.id}/disable",
        {"reason": "Original owner departed"},
        etag=account_etag(state.b.client, state.a.id),
        status=204,
    )
    assert state.b.client.get(path).status_code == 200
    accepted = takeover(
        state, state.b, path, original.headers["etag"], key="disabled-owner-takeover"
    )
    assert accepted.json()["ownerId"] == state.b.id
    _write(
        state.c.client,
        f"/api/v1/admin/super-admins/{state.b.id}",
        {"reason": "Current actor revoked", "totpCode": otp(state, state.c.secret)},
        etag=account_etag(state.c.client, state.b.id),
        method="DELETE",
    )
    with injected_client(state.b, trap) as client:
        assert client.get(path).status_code in (401, 403)
        result = client.post(
            path + "/takeover",
            json={"reason": "  原因原文\n承接草稿  "},
            headers={
                **SAME_ORIGIN,
                "If-Match": original.headers["etag"],
                "Idempotency-Key": "disabled-owner-takeover",
            },
        )
        assert result.status_code in (401, 403)
    denied.assert_not_called()


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("failure", ["owner", "audit", "receipt"])
def test_takeover_failure_rolls_back_owner_all_evidence_audit_and_sealed_receipt(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, failure: str
) -> None:
    state = actors
    path, original = prepare(state, namespace)
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []

    @contextmanager
    def transaction() -> Iterator[Any]:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            if failure == "audit":
                audit = Mock()
                audit.append_in_transaction.side_effect = RuntimeError(
                    "synthetic takeover audit failure"
                )
                lifecycle = PolicyLifecycle(
                    lifecycle.db, lifecycle.owner, replace(runtime.dependencies, audit=audit)
                )
            else:
                name = "takeover_draft" if failure == "owner" else "complete_idempotency"
                persist = getattr(lifecycle.owner, name)

                def fail_after_write(*args: Any, **kwargs: Any) -> Any:
                    persist(*args, **kwargs)
                    raise RuntimeError("synthetic takeover persistence failure")

                monkeypatch.setattr(lifecycle.owner, name, fail_after_write)
            yield lifecycle

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    before = policy_facts(state)
    with injected_client(state.b, replace(runtime, owners=owners)) as client:
        _write(
            client,
            path + "/takeover",
            {"reason": "Fault injection"},
            etag=original.headers["etag"],
            status=500,
        )
    assert policy_facts(state) == before
    assert connections and all(db.closed for db in connections)
    assert state.b.client.get(path).content == original.content


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("contender", ["takeover", "edit", "archive", "publish"])
def test_takeover_races_preserve_revision_and_publish_lock_order(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, contender: str
) -> None:
    state = actors
    path, original = prepare(state, namespace)
    runtime = bootstrap.configuration_http_runtime().owners.resolve(namespace)
    owner_class = IdentityPolicyOwner if namespace == "identity" else SqlAlchemyGatePolicyRepository
    owner_table = "identity.draft" if namespace == "identity" else "requirement.gate_policy_draft"
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    held, release, contender_started = Event(), Event(), Event()
    owner_pids: list[int] = []
    contender_pids: list[int] = []
    persist = owner_class.takeover_draft

    def hold_after_write(owner: Any, *args: Any, **kwargs: Any) -> Any:
        updated = persist(owner, *args, **kwargs)
        if updated is not None:
            owner_pids.append(owner.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            held.set()
            assert release.wait(10), "test did not release takeover transaction"
        return updated

    def observe(
        connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: Any,
    ) -> None:
        if held.is_set() and owner_table in statement and not contender_pids:
            contender_pids.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            contender_started.set()

    monkeypatch.setattr(owner_class, "takeover_draft", hold_after_write)
    sql_event.listen(engine, "before_cursor_execute", observe)
    publish_code = otp(state, state.a.secret) if contender == "publish" else "000000"
    original_activity = datetime.fromisoformat(original.json()["lastMeaningfulActivityAt"])
    state.clock.value += timedelta(seconds=1)

    def compete() -> Any:
        if contender == "takeover":
            return _write(
                state.c.client,
                path + "/takeover",
                {"reason": "competing takeover"},
                etag=original.headers["etag"],
                status=409,
            )
        if contender == "edit":
            return _write(
                state.a.client,
                path,
                {"values": {}},
                etag=original.headers["etag"],
                method="PATCH",
                status=409,
            )
        if contender == "publish":
            return _write(
                state.a.client,
                path + "/publish",
                {"reason": "old publish context", "totpCode": publish_code},
                etag=original.headers["etag"],
                status=409,
            )
        return runtime.archive(now=original_activity + timedelta(days=30, microseconds=500000))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            taking = pool.submit(
                _write,
                state.b.client,
                path + "/takeover",
                {"reason": "hold actual owner commit"},
                etag=original.headers["etag"],
            )
            try:
                assert held.wait(5)
                competing = pool.submit(compete)
                assert contender_started.wait(5)
                assert_postgres_blocked_by(
                    cast(IsolatedAgentDatabase, state.journey.database),
                    waiting_pid=contender_pids[0],
                    blocking_pid=owner_pids[0],
                )
            finally:
                release.set()
            accepted = taking.result(timeout=10)
            result = competing.result(timeout=10)
            if contender == "archive":
                assert result == 0
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    current = state.b.client.get(path)
    assert current.status_code == 200 and current.json()["ownerId"] == state.b.id
    assert (
        current.json()["status"] == "DRAFT" and current.headers["etag"] == accepted.headers["etag"]
    )
    assert current.json()["content"] == original.json()["content"]
