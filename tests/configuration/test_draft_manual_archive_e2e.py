import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
import control_plane.app.shared.idempotency as idempotency
from control_plane.app.modules.configuration import PolicyLifecycle, PolicyRuntimeRegistry
from control_plane.app.modules.configuration.adapters.identity import IdentityPolicyOwner
from control_plane.app.modules.requirement.adapters.gate_policy import (
    SqlAlchemyGatePolicyRepository,
)
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_control import assert_postgres_blocked_by
from tests.configuration.test_draft_base_comparison_e2e import publish, read_comparison, saved_draft
from tests.configuration.test_draft_clone_e2e import all_facts, clone, copied_path, tables
from tests.configuration.test_draft_rebase_e2e import apply, body_for, setup_stale
from tests.configuration.test_draft_takeover_e2e import account_etag, injected_client, otp, takeover
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _grant, _write

pytestmark = pytest.mark.integration


def outbox_table(namespace: str) -> str:
    return (
        "identity.configuration_outbox"
        if namespace == "identity"
        else "requirement.gate_policy_outbox"
    )


def archive(
    actor: Any, path: str, draft: Any, *, key: str = "manual-archive", status: int = 200
) -> Any:
    response = _write(
        actor.client, path + "/archive", {}, etag=draft.headers["etag"], key=key, status=status
    )
    if status == 200:
        assert response.headers["etag"] == draft.headers["etag"]
        assert response.headers["cache-control"] == "no-store"
    return response


def archived_facts(state: Any, namespace: str, draft_id: str) -> tuple[list[Any], list[Any]]:
    facts = all_facts(state)
    return (
        [
            row
            for row in facts[outbox_table(namespace)]
            if row["event_type"] == "DRAFT_ARCHIVED" and row["payload"]["draftId"] == draft_id
        ],
        [
            row
            for row in facts["audit"]
            if row["action"] == "configuration.draft.archived" and row["target_id"] == draft_id
        ],
    )


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("kind", ["saved", "stale", "rollback", "rebased", "cloned"])
def test_real_owner_archive_retains_facts_clears_evidence_and_allows_explicit_clone(
    actors: Any, namespace: str, kind: str
) -> None:
    state = actors
    if kind in {"stale", "rebased"}:
        target = setup_stale(state, namespace)
        path, draft = target.path, target.draft
        if kind == "rebased":
            draft = apply(state.a, target, body=body_for(target.comparison))
    else:
        path, draft = saved_draft(state, namespace, {})
        if kind == "cloned":
            copied = clone(state.a, path, draft)
            path = copied_path(namespace, copied)
            draft = state.a.client.get(path)
        if kind == "rollback":
            publish(state, state.a, path, draft)
            draft = _write(
                state.a.client,
                f"/api/v1/admin/policies/{namespace}/rollback",
                {
                    "toVersion": 1,
                    "reason": "Manual archive rollback draft",
                    "totpCode": otp(state, state.a.secret),
                },
                etag='"v2"',
                status=201,
            )
            path = f"/api/v1/admin/policies/{namespace}/drafts/{draft.json()['id']}"
            created_etag = draft.headers["etag"]
            draft = state.a.client.get(path)
            assert draft.status_code == 200, draft.text
            assert draft.headers["etag"] == created_etag
    comparison = read_comparison(state.a, path, draft) if kind == "saved" else None
    before = all_facts(state)
    original = draft.json()
    response = archive(state.a, path, draft)
    receipt = response.json()
    assert receipt == {
        "draftId": original["id"],
        "namespace": namespace,
        "scope": "PLATFORM",
        "draftRevision": original["revision"],
        "status": "ARCHIVED",
        "archivedAt": receipt["archivedAt"],
    }
    fresh = state.a.client.get(path)
    assert fresh.headers["etag"] == draft.headers["etag"]
    changed = {"status", "archivedAt", "validationEvidence", "previewEvidence"}
    assert {key: value for key, value in fresh.json().items() if key not in changed} == {
        key: value for key, value in original.items() if key not in changed
    }
    assert fresh.json()["archivedAt"] == receipt["archivedAt"]
    assert fresh.json()["validationEvidence"] is None and fresh.json()["previewEvidence"] is None
    after = all_facts(state)
    allowed = {tables(namespace)[0], tables(namespace)[3], outbox_table(namespace), "audit"}
    assert {key: value for key, value in after.items() if key not in allowed} == {
        key: value for key, value in before.items() if key not in allowed
    }
    stored = next(row for row in after[tables(namespace)[0]] if row["id"] == original["id"])
    for field in stored:
        if field.startswith(("validation_", "preview_")):
            assert stored[field] is None
    events, audits = archived_facts(state, namespace, original["id"])
    assert len(events) == len(audits) == 1
    payload = events[0]["payload"]
    assert payload == {
        "draftId": original["id"],
        "namespace": namespace,
        "scope": "PLATFORM",
        "draftRevision": original["revision"],
        "source": "MANUAL",
        "actorId": state.a.id,
        "actorType": "human",
        "result": "ARCHIVED",
    }
    assert audits[0]["actor_type"] == "human" and audits[0]["actor"] == state.a.id
    assert json.loads(audits[0]["reason"])["source"] == "MANUAL"
    replay_before = all_facts(state)
    replay = archive(state.a, path, draft)
    assert replay.content == response.content and all_facts(state) == replay_before
    archive(state.a, path, draft, key="other-manual-key", status=409)
    refused_operations: list[tuple[str, dict[str, Any]]] = [("/validate", {}), ("", {"values": {}})]
    for suffix, body in refused_operations:
        refused = _write(
            state.a.client,
            path + suffix,
            body,
            etag=draft.headers["etag"],
            method="PATCH" if not suffix else "POST",
            status=409,
        )
        assert refused.json()["title"] == "Draft archived"
    takeover(state, state.b, path, draft.headers["etag"], status=409)
    if comparison is not None:
        refused = apply(
            state.a,
            SimpleNamespace(path=path, draft=draft, body=body_for(comparison)),
            status=409,
        )
        assert refused.json()["title"] == "Draft archived"
        _write(
            state.a.client,
            path + "/publish",
            {"reason": "Archived target cannot publish", "totpCode": otp(state, state.a.secret)},
            etag=draft.headers["etag"],
            status=409,
        )
    copied = clone(state.b, path, fresh)
    assert copied.json()["source"]["clonedFromArchivedDraftId"] == original["id"]
    assert (
        copied.json()["draft"]["status"] == "DRAFT"
        and copied.json()["draft"]["id"] != original["id"]
    )
    assert copied.json()["draft"]["validationEvidence"] is None
    assert copied.json()["draft"]["previewEvidence"] is None


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_manual_archive_does_not_need_or_lock_the_target_active_pointer(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with state.journey.database.owner.begin() as db:
            db.execute(
                text(f"SELECT 1 FROM {tables(namespace)[2]} WHERE namespace=:ns FOR UPDATE"),
                {"ns": namespace},
            )
            result = pool.submit(archive, state.a, path, draft).result(timeout=5)
    assert result.json()["status"] == "ARCHIVED"


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("change", ["revoke", "logout"])
def test_real_session_qualification_is_rechecked_after_draft_lock_wait(
    actors: Any, namespace: str, change: str
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    started = Event()
    pids: list[int] = []

    def observe(
        connection: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
    ) -> None:
        if not pids and "FOR UPDATE" in statement and tables(namespace)[0] in statement:
            pids.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()

    before = all_facts(state)
    sql_event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with state.journey.database.owner.begin() as locking:
                blocker = locking.execute(text("SELECT pg_backend_pid()")).scalar_one()
                locking.execute(
                    text(f"SELECT 1 FROM {tables(namespace)[0]} WHERE id=:id FOR UPDATE"),
                    {"id": draft.json()["id"]},
                )
                pending = pool.submit(
                    state.a.client.post,
                    path + "/archive",
                    json={},
                    headers={
                        "Origin": "https://testserver",
                        "Idempotency-Key": "waiting-archive",
                        "If-Match": draft.headers["etag"],
                    },
                )
                assert started.wait(5)
                assert_postgres_blocked_by(
                    cast(IsolatedAgentDatabase, state.journey.database),
                    waiting_pid=pids[0],
                    blocking_pid=blocker,
                )
                if change == "revoke":
                    _write(
                        state.c.client,
                        f"/api/v1/admin/super-admins/{state.a.id}",
                        {
                            "reason": "Archive waited during revocation",
                            "totpCode": otp(state, state.c.secret),
                        },
                        etag=account_etag(state.c.client, state.a.id),
                        method="DELETE",
                    )
                else:
                    _write(state.a.client, "/api/v1/auth/logout", {})
            response = pending.result(timeout=10)
            assert response.status_code in ({401, 403} if change == "revoke" else {401}), (
                response.text
            )
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    assert all_facts(state)[tables(namespace)[0]] == before[tables(namespace)[0]]
    assert archived_facts(state, namespace, draft.json()["id"]) == ([], [])


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "race",
    [
        "edit-first",
        "takeover-first",
        "automatic-first",
        "manual-edit",
        "manual-takeover",
        "manual-automatic",
        "same-key",
        "different-key",
    ],
)
def test_real_public_competitors_serialize_on_cas_and_keep_one_archive_fact(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, race: str
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})
    first_kind = race.split("-")[0] if race.endswith("-first") else "manual"
    second_kind = (
        "manual"
        if race.endswith("-first") or race in {"same-key", "different-key"}
        else race.split("-")[1]
    )
    owner_class: Any = (
        IdentityPolicyOwner if namespace == "identity" else SqlAlchemyGatePolicyRepository
    )
    method = {
        "edit": "update_draft",
        "takeover": "takeover_draft",
        "automatic": "archive_draft",
        "manual": "archive_draft",
    }[first_kind]
    persist = getattr(owner_class, method)
    held, release, started = Event(), Event(), Event()
    first_pids: list[int] = []
    waiting_pids: list[int] = []

    def hold(owner: Any, *args: Any, **values: Any) -> Any:
        result = persist(owner, *args, **values)
        if not first_pids:
            first_pids.append(owner.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            held.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(owner_class, method, hold)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]

    def observe(
        connection: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
    ) -> None:
        relevant = (
            (tables(namespace)[3] in statement and "INSERT" in statement.upper())
            if race == "same-key"
            else (
                tables(namespace)[0] in statement
                and (
                    "FOR UPDATE" in statement.upper()
                    or statement.upper().lstrip().startswith("UPDATE")
                )
            )
        )
        if held.is_set() and not waiting_pids and relevant:
            waiting_pids.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()

    def invoke(kind: str, *, second: bool = False) -> Any:
        status = (
            409
            if second
            and (first_kind != "manual" or kind in {"edit", "takeover"} or race == "different-key")
            else 200
        )
        if kind == "manual":
            return archive(
                state.a,
                path,
                draft,
                key="second-archive" if race == "different-key" and second else "racing-archive",
                status=status,
            )
        if kind == "edit":
            return _write(
                state.a.client,
                path,
                {"values": {}},
                etag=draft.headers["etag"],
                method="PATCH",
                status=status,
            )
        if kind == "takeover":
            return takeover(state, state.b, path, draft.headers["etag"], status=status)
        return (
            bootstrap.configuration_http_runtime()
            .owners.resolve(namespace)
            .archive(now=state.clock.value + timedelta(days=31))
        )

    sql_event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke, first_kind)
            try:
                assert held.wait(5)
                second = pool.submit(invoke, second_kind, second=True)
                assert started.wait(5)
                assert_postgres_blocked_by(
                    cast(IsolatedAgentDatabase, state.journey.database),
                    waiting_pid=waiting_pids[0],
                    blocking_pid=first_pids[0],
                )
            finally:
                release.set()
            left, right = first.result(timeout=10), second.result(timeout=10)
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    events, audits = archived_facts(state, namespace, draft.json()["id"])
    expected = 0 if first_kind in {"edit", "takeover"} else 1
    assert len(events) == len(audits) == expected
    if first_kind == "manual":
        assert events[0]["payload"]["source"] == "MANUAL" and audits[0]["actor_type"] == "human"
        assert state.a.client.get(path).headers["etag"] == draft.headers["etag"]
    if first_kind == "automatic":
        assert left == 1 and audits[0]["actor_type"] == "workload"
        assert "source" not in events[0]["payload"] and "cutoff" in events[0]["payload"]
    if second_kind == "automatic":
        assert right == 0
    if race == "same-key":
        assert left.content == right.content


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "fault,status", [("authorization", 503), ("outbox", 500), ("audit", 500), ("seal", 500)]
)
def test_owner_transaction_rolls_back_archival_outbox_audit_and_receipt_and_releases_connection(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, fault: str, status: int
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []

    @contextmanager
    def transaction() -> Iterator[Any]:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            if fault == "audit":
                audit = Mock()
                audit.append_in_transaction.side_effect = RuntimeError("synthetic audit failure")
                lifecycle = PolicyLifecycle(
                    lifecycle.db, lifecycle.owner, replace(runtime.dependencies, audit=audit)
                )
            if fault == "outbox":
                execute = lifecycle.db.execute

                def fail_insert(statement: Any, *args: Any, **kwargs: Any) -> Any:
                    if f"INSERT INTO {outbox_table(namespace)}" in str(statement):
                        raise RuntimeError("synthetic outbox failure")
                    return execute(statement, *args, **kwargs)

                monkeypatch.setattr(lifecycle.db, "execute", fail_insert)
            yield lifecycle

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    authorizer = runtime.draft_authorization
    if fault == "authorization":
        authorizer = Mock()
        authorizer.check.side_effect = RuntimeError("synthetic current authorization failure")
    if fault == "seal":
        monkeypatch.setattr(
            idempotency, "seal", Mock(side_effect=RuntimeError("synthetic sealing failure"))
        )
    before = all_facts(state)
    with injected_client(
        state.a, replace(runtime, owners=owners, draft_authorization=authorizer)
    ) as client:
        archive(SimpleNamespace(client=client), path, draft, status=status)
    assert connections and all(connection.closed for connection in connections)
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_default_session_owner_rules_and_revoked_replay_remain_authoritative(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})
    archive(state.b, path, draft, status=403)
    _write(
        state.c.client,
        f"/api/v1/admin/accounts/{state.b.id}/disable",
        {"reason": "Disabled non-owner"},
        etag=account_etag(state.c.client, state.b.id),
        status=204,
    )
    runtime = bootstrap.configuration_http_runtime()
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve.side_effect = AssertionError("unauthorized archive cannot read owner")
    for scope in (None, state.journey.workspace_id):
        _grant(state.a.client, state.journey.member_id, "platform.configuration.manage", scope)
    with injected_client(
        SimpleNamespace(client=state.journey.member), replace(runtime, owners=owners)
    ) as client:
        archive(SimpleNamespace(client=client), path, draft, status=403)
        client.cookies.clear()
        archive(SimpleNamespace(client=client), path, draft, status=401)
    archive(state.a, path, draft, key="revoked-archive-replay")
    _write(
        state.c.client,
        f"/api/v1/admin/super-admins/{state.a.id}",
        {
            "reason": "Revoke archived draft owner",
            "totpCode": otp(state, state.c.secret),
        },
        etag=account_etag(state.c.client, state.a.id),
        method="DELETE",
    )
    before = all_facts(state)
    with injected_client(state.a, replace(runtime, owners=owners)) as client:
        response = client.post(
            path + "/archive",
            json={},
            headers={
                "Origin": "https://testserver",
                "Idempotency-Key": "revoked-archive-replay",
                "If-Match": draft.headers["etag"],
            },
        )
        assert response.status_code in {401, 403}
    owners.resolve.assert_not_called()
    assert all_facts(state) == before
    missing = f"/api/v1/admin/policies/{namespace}/drafts/{uuid4()}"
    archive(state.c, missing, draft, status=404)
