import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.configuration import PolicyLifecycle, PolicyRuntimeRegistry
from control_plane.app.modules.configuration.adapters.identity import IdentityPolicyOwner
from control_plane.app.modules.identity.adapters.configuration_policy import (
    SqlAlchemyIdentityPolicyOwnerRepository,
)
from control_plane.app.modules.requirement.adapters.gate_policy import (
    SqlAlchemyGatePolicyRepository,
)
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_control import assert_postgres_blocked_by
from tests.configuration.test_draft_base_comparison import comparison_state
from tests.configuration.test_draft_base_comparison_e2e import publish, read_comparison, saved_draft
from tests.configuration.test_draft_rebase_e2e import all_facts as rebase_facts
from tests.configuration.test_draft_rebase_e2e import (
    apply,
    body_for,
    observe_owner_wait,
    setup_stale,
)
from tests.configuration.test_draft_takeover_e2e import account_etag, injected_client, otp, takeover
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _grant, _write

pytestmark = pytest.mark.integration


def tables(namespace: str) -> tuple[str, str, str, str]:
    if namespace == "identity":
        return (
            "identity.draft",
            "identity.draft_clone",
            "identity.active_pointer",
            "identity.configuration_idempotency_record",
        )
    return (
        "requirement.gate_policy_draft",
        "requirement.gate_policy_clone",
        "requirement.gate_policy_active_pointer",
        "requirement.gate_policy_idempotency",
    )


def records(state: Any, namespace: str) -> list[Any]:
    with state.journey.database.owner.connect() as db:
        return list(
            db.execute(
                text(f"SELECT to_jsonb(t) FROM {tables(namespace)[1]} t ORDER BY id")
            ).scalars()
        )


def all_facts(state: Any) -> Any:
    facts = rebase_facts(state)
    for namespace in ("identity", "requirement.gate"):
        facts[tables(namespace)[1]] = records(state, namespace)
    return facts


def clone(actor: Any, path: str, source: Any, *, key: str | None = None, status: int = 201) -> Any:
    return _write(
        actor.client, path + "/clone", {}, etag=source.headers["etag"], key=key, status=status
    )


def copied_path(namespace: str, receipt: Any) -> str:
    return f"/api/v1/admin/policies/{namespace}/drafts/{receipt.json()['draft']['id']}"


def assert_new_copy(
    state: Any, namespace: str, actor: Any, source: Any, receipt: Any, before: Any
) -> Any:
    result = receipt.json()
    draft = result["draft"]
    original = source.json()
    assert receipt.headers["etag"] == '"v1"'
    assert draft["id"] != original["id"] and draft["revision"] == 1
    assert draft["ownerId"] == actor.id and draft["status"] == "DRAFT"
    for field in (
        "namespace",
        "scope",
        "schemaRevision",
        "baseVersion",
        "content",
        "contentHash",
        "rollbackFromVersion",
    ):
        assert draft[field] == original[field]
    assert draft["stale"] is (original["baseVersion"] < result["currentVersionAtClone"])
    assert draft["archivedAt"] is None
    assert draft["validationEvidence"] is None and draft["previewEvidence"] is None
    assert result["source"] == {
        "draftId": original["id"],
        "revision": original["revision"],
        "ownerId": original["ownerId"],
        "status": original["status"],
        "baseVersion": original["baseVersion"],
        "schemaRevision": original["schemaRevision"],
        "contentHash": original["contentHash"],
        "rollbackFromVersion": original["rollbackFromVersion"],
        "clonedFromArchivedDraftId": original["id"] if original["status"] == "ARCHIVED" else None,
    }
    after = all_facts(state)
    draft_table, source_table, _, idempotency = tables(namespace)
    assert {
        k: v for k, v in after.items() if k not in {draft_table, source_table, idempotency, "audit"}
    } == {
        k: v
        for k, v in before.items()
        if k not in {draft_table, source_table, idempotency, "audit"}
    }
    assert [row for row in after[draft_table] if row["id"] != draft["id"]] == before[draft_table]
    stored = next(row for row in after[draft_table] if row["id"] == draft["id"])
    assert all(
        value is None
        for key, value in stored.items()
        if key.startswith(("validation_", "preview_"))
    )
    row = next(row for row in records(state, namespace) if row["draft_id"] == draft["id"])
    assert (
        row["source_draft_id"] == original["id"] and row["source_revision"] == original["revision"]
    )
    assert (
        row["source_owner_id"] == original["ownerId"] and row["source_status"] == original["status"]
    )
    assert (
        row["source_content"] == original["content"]
        and row["source_content_hash"] == original["contentHash"]
    )
    assert row["content_hash"] == draft["contentHash"]
    assert row["base_version"] == draft["baseVersion"]
    assert row["current_version"] == result["currentVersionAtClone"]
    assert row["rollback_from_version"] == original["rollbackFromVersion"]
    assert row["cloned_from_archived_draft_id"] == result["source"]["clonedFromArchivedDraftId"]
    audit = next(
        item
        for item in after["audit"]
        if item["action"] == "configuration.draft.cloned" and item["target_id"] == draft["id"]
    )
    summary = json.loads(audit["reason"])
    assert summary["id"] == row["id"] and "source_content" not in summary
    raw_session = actor.client.cookies.get("ep_session")
    assert raw_session and raw_session not in json.dumps(row) + audit["reason"]
    fresh = actor.client.get(copied_path(namespace, receipt))
    assert fresh.status_code == 200 and fresh.json() == draft
    return fresh


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("who", ["self", "other"])
def test_real_full_copy_chain_is_independent_and_new_draft_needs_fresh_publication(
    actors: Any, namespace: str, who: str
) -> None:
    state = actors
    values = deepcopy(comparison_state(namespace).draft.content)
    if namespace == "identity":
        values["identity.password_max_age"] = 42
        values["identity.login_backoff"]["maximumDelaySeconds"] = 1200
    path, source = saved_draft(state, namespace, values)
    actor = state.a if who == "self" else state.b
    before = all_facts(state)
    first = clone(actor, path, source)
    fresh = assert_new_copy(state, namespace, actor, source, first, before)
    assert fresh.json()["stale"] is False
    first_path = copied_path(namespace, first)
    before = all_facts(state)
    second = clone(state.c, first_path, fresh)
    assert_new_copy(state, namespace, state.c, fresh, second, before)
    chain = records(state, namespace)
    assert len(chain) == 2
    changed = _write(
        actor.client,
        first_path,
        {
            "values": {
                "identity.session_cap" if namespace == "identity" else "draft_archive_after_days": 6
            }
        },
        etag=fresh.headers["etag"],
        method="PATCH",
    )
    assert records(state, namespace) == chain
    assert state.a.client.get(path).json() == source.json()
    assert state.c.client.get(copied_path(namespace, second)).json() == second.json()["draft"]
    validated = _write(actor.client, first_path + "/validate", {}, etag=changed.headers["etag"])
    assert validated.json()["valid"] is True
    preview = actor.client.get(
        first_path + "/preview", headers={"If-Match": validated.headers["etag"]}
    )
    assert preview.status_code == 200
    assert publish(state, actor, first_path, preview).json()["version"] == 2
    assert records(state, namespace) == chain


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_published_source_keeps_original_base_and_copy_must_rebase_even_when_source_not_stale(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    source = state.b.client.get(target.current_path)
    assert source.json()["stale"] is (namespace != "identity")
    before = all_facts(state)
    receipt = clone(state.a, target.current_path, source)
    fresh = assert_new_copy(state, namespace, state.a, source, receipt, before)
    assert fresh.json()["stale"] is True and fresh.json()["baseVersion"] == 1
    path = copied_path(namespace, receipt)
    _write(state.a.client, path + "/validate", {}, etag=fresh.headers["etag"], status=409)
    comparison = read_comparison(state.a, path, fresh)
    rebased = apply(state.a, SimpleNamespace(path=path, draft=fresh, body=body_for(comparison)))
    assert rebased.json()["baseVersion"] == 2 and rebased.json()["stale"] is False
    validation = _write(state.a.client, path + "/validate", {}, etag=rebased.headers["etag"])
    preview = state.a.client.get(
        path + "/preview", headers={"If-Match": validation.headers["etag"]}
    )
    assert preview.status_code == 200
    assert publish(state, state.a, path, preview).json()["version"] == 3
    assert records(state, namespace)[0]["base_version"] == 1


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_disabled_source_owner_archived_copy_and_runtime_record_permissions(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    _write(
        state.b.client,
        f"/api/v1/admin/accounts/{state.a.id}/disable",
        {"reason": "Source owner left"},
        etag=account_etag(state.b.client, state.a.id),
        status=204,
    )
    disabled_owner_source = state.b.client.get(target.path)
    assert disabled_owner_source.json()["status"] == "DRAFT"
    before = all_facts(state)
    live_receipt = clone(state.b, target.path, disabled_owner_source)
    assert_new_copy(state, namespace, state.b, disabled_owner_source, live_receipt, before)
    live_record = records(state, namespace)[0]
    assert (
        bootstrap.configuration_http_runtime()
        .owners.resolve(namespace)
        .archive(now=state.clock.value + timedelta(days=31))
        >= 1
    )
    source = state.b.client.get(target.path)
    assert source.json()["status"] == "ARCHIVED" and source.json()["ownerId"] == state.a.id
    before = all_facts(state)
    receipt = clone(state.b, target.path, source)
    assert_new_copy(state, namespace, state.b, source, receipt, before)
    rows = records(state, namespace)
    assert len(rows) == 2 and live_record in rows
    assert {row["source_status"] for row in rows} == {"DRAFT", "ARCHIVED"}
    table = tables(namespace)[1]
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    with engine.connect() as db:
        assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 2
    for statement in (f"UPDATE {table} SET recorded_at=recorded_at", f"DELETE FROM {table}"):
        with pytest.raises(ProgrammingError), engine.begin() as db:
            db.execute(text(statement))
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.execute(text(f"INSERT INTO {table} SELECT * FROM {table}"))
    assert records(state, namespace) == rows


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_rollback_origin_and_original_receipt_survive_source_copy_takeover_and_archive(
    actors: Any, namespace: str
) -> None:
    state = actors
    setup_stale(state, namespace)
    rollback = _write(
        state.a.client,
        f"/api/v1/admin/policies/{namespace}/rollback",
        {"toVersion": 1, "reason": "Clone rollback origin", "totpCode": otp(state, state.a.secret)},
        etag='"v2"',
        status=201,
    )
    path = f"/api/v1/admin/policies/{namespace}/drafts/{rollback.json()['id']}"
    source = state.a.client.get(path)
    assert source.json()["rollbackFromVersion"] == 1 and source.json()["baseVersion"] == 2
    before = all_facts(state)
    receipt = clone(state.a, path, source, key="clone-original-receipt")
    fresh = assert_new_copy(state, namespace, state.a, source, receipt, before)
    assert fresh.json()["rollbackFromVersion"] == 1
    takeover(state, state.b, path, source.headers["etag"])
    takeover(state, state.b, copied_path(namespace, receipt), fresh.headers["etag"])
    runtime = bootstrap.configuration_http_runtime()
    assert (
        runtime.owners.resolve(namespace).archive(now=state.clock.value + timedelta(days=31)) >= 1
    )
    assert state.b.client.get(path).json()["status"] == "ARCHIVED"
    assert state.b.client.get(copied_path(namespace, receipt)).json()["status"] == "ARCHIVED"
    blocked = Mock()
    blocked.check.side_effect = AssertionError("historical clone cannot rerun current check")
    before = all_facts(state)
    with injected_client(state.a, replace(runtime, draft_authorization=blocked)) as client:
        replay = _write(
            client,
            path + "/clone",
            {},
            etag=source.headers["etag"],
            key="clone-original-receipt",
            status=201,
        )
    blocked.check.assert_not_called()
    assert replay.content == receipt.content and replay.headers["etag"] == receipt.headers["etag"]
    assert all_facts(state) == before
    changed = state.b.client.get(path)
    clone(state.a, path, changed, key="clone-original-receipt", status=409)
    clone(
        state.a, copied_path(namespace, receipt), source, key="clone-original-receipt", status=409
    )


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_clone_same_key_replays_once_and_different_keys_both_create(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, same_key: bool
) -> None:
    state = actors
    path, source = saved_draft(state, namespace, {})
    owner_class: Any = (
        IdentityPolicyOwner if namespace == "identity" else SqlAlchemyGatePolicyRepository
    )
    persist = owner_class.record_clone
    held, release, started = Event(), Event(), Event()
    first_pids: list[int] = []
    waiting_pids: list[int] = []

    def hold(owner: Any, **values: Any) -> None:
        persist(owner, **values)
        first_pids.append(owner.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        held.set()
        assert release.wait(10)

    monkeypatch.setattr(owner_class, "record_clone", hold)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]

    def observe(
        connection: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
    ) -> None:
        relevant = (
            (tables(namespace)[3] in statement and "INSERT" in statement.upper())
            if same_key
            else (tables(namespace)[2] in statement and "FOR UPDATE" in statement.upper())
        )
        if held.is_set() and not waiting_pids and relevant:
            waiting_pids.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()

    sql_event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(clone, state.a, path, source, key="concurrent-clone-one")
            try:
                assert held.wait(5)
                second = pool.submit(
                    clone,
                    state.a,
                    path,
                    source,
                    key="concurrent-clone-one" if same_key else "concurrent-clone-two",
                )
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
    assert (left.content == right.content) is same_key
    assert (left.json()["draft"]["id"] == right.json()["draft"]["id"]) is same_key
    assert len(records(state, namespace)) == (1 if same_key else 2)
    assert state.a.client.get(path).json() == source.json()


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("contender", ["edit", "takeover", "rebase", "archive", "publish"])
def test_clone_first_keeps_locked_source_facts_and_allows_subsequent_source_commands(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, contender: str
) -> None:
    state = actors
    if contender == "rebase":
        target = setup_stale(state, namespace)
        path, source = target.path, target.draft
    else:
        path, source = saved_draft(state, namespace, comparison_state(namespace).draft.content)
    owner_class: Any = (
        IdentityPolicyOwner if namespace == "identity" else SqlAlchemyGatePolicyRepository
    )
    persist = owner_class.record_clone
    held, release = Event(), Event()
    first_pids: list[int] = []

    def hold(owner: Any, **values: Any) -> None:
        persist(owner, **values)
        first_pids.append(owner.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        held.set()
        assert release.wait(10)

    monkeypatch.setattr(owner_class, "record_clone", hold)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    started, pids, observe = observe_owner_wait(
        engine, namespace, held, archive_update=namespace == "identity" and contender == "archive"
    )

    def compete() -> Any:
        if contender == "edit":
            return _write(
                state.a.client,
                path,
                {
                    "values": {
                        "identity.session_cap"
                        if namespace == "identity"
                        else "draft_archive_after_days": 6
                    }
                },
                etag=source.headers["etag"],
                method="PATCH",
            )
        if contender == "takeover":
            return takeover(state, state.b, path, source.headers["etag"])
        if contender == "rebase":
            return apply(state.a, target)
        if contender == "publish":
            return publish(state, state.a, path, source)
        return (
            bootstrap.configuration_http_runtime()
            .owners.resolve(namespace)
            .archive(now=state.clock.value + timedelta(days=31))
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(clone, state.a, path, source)
            try:
                assert held.wait(5)
                other = pool.submit(compete)
                assert started.wait(5)
                assert_postgres_blocked_by(
                    cast(IsolatedAgentDatabase, state.journey.database),
                    waiting_pid=pids[0],
                    blocking_pid=first_pids[0],
                )
            finally:
                release.set()
            receipt, changed = first.result(timeout=10), other.result(timeout=10)
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    row = records(state, namespace)[0]
    assert row["source_content"] == source.json()["content"]
    assert row["source_revision"] == source.json()["revision"]
    assert row["source_owner_id"] == source.json()["ownerId"]
    assert row["source_status"] == "DRAFT"
    copied = state.c.client.get(copied_path(namespace, receipt)).json()
    assert (
        copied["content"] == source.json()["content"]
        and copied["baseVersion"] == source.json()["baseVersion"]
    )
    latest = state.c.client.get(path).json()
    if contender == "archive":
        assert changed >= 1 and latest["status"] == "ARCHIVED"
    elif contender == "publish":
        assert changed.json()["version"] == 2
        assert row["current_version"] == 1 and copied["stale"] is True
    else:
        assert latest["revision"] == source.json()["revision"] + 1
        if contender == "takeover":
            assert latest["ownerId"] == state.b.id
        if contender == "rebase":
            assert latest["baseVersion"] == 2 and copied["stale"] is True


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("contender", ["edit", "takeover", "rebase", "archive"])
def test_source_change_first_checks_revision_and_records_same_revision_archive(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, contender: str
) -> None:
    state = actors
    if contender == "rebase":
        target = setup_stale(state, namespace)
        path, source = target.path, target.draft
    else:
        path, source = saved_draft(state, namespace, {})
    owner_class: Any = (
        IdentityPolicyOwner if namespace == "identity" else SqlAlchemyGatePolicyRepository
    )
    name = {
        "edit": "update_draft",
        "takeover": "takeover_draft",
        "rebase": "rebase_draft",
        "archive": "archive_draft",
    }[contender]
    persist = getattr(owner_class, name)
    held, release = Event(), Event()
    first_pids: list[int] = []

    def hold(owner: Any, *args: Any, **values: Any) -> Any:
        result = persist(owner, *args, **values)
        first_pids.append(owner.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        held.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(owner_class, name, hold)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    started, pids, observe = observe_owner_wait(engine, namespace, held)
    if contender in {"edit", "takeover"} or (namespace == "identity" and contender == "archive"):
        # These original commands hold the source row, without locking the Active pointer.
        sql_event.remove(engine, "before_cursor_execute", observe)

        def observe(
            connection: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
        ) -> None:
            if (
                held.is_set()
                and not pids
                and "FOR UPDATE" in statement.upper()
                and tables(namespace)[0] in statement
            ):
                pids.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
                started.set()

        sql_event.listen(engine, "before_cursor_execute", observe)

    def first_command() -> Any:
        if contender == "edit":
            return _write(
                state.a.client, path, {"values": {}}, etag=source.headers["etag"], method="PATCH"
            )
        if contender == "takeover":
            return takeover(state, state.b, path, source.headers["etag"])
        if contender == "rebase":
            return apply(state.a, target)
        return (
            bootstrap.configuration_http_runtime()
            .owners.resolve(namespace)
            .archive(now=state.clock.value + timedelta(days=31))
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_command)
            try:
                assert held.wait(5)
                pending = pool.submit(
                    clone, state.c, path, source, status=201 if contender == "archive" else 409
                )
                assert started.wait(5)
                assert_postgres_blocked_by(
                    cast(IsolatedAgentDatabase, state.journey.database),
                    waiting_pid=pids[0],
                    blocking_pid=first_pids[0],
                )
            finally:
                release.set()
            first.result(timeout=10)
            response = pending.result(timeout=10)
            assert response.status_code == (201 if contender == "archive" else 409)
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    latest = state.c.client.get(path)
    if contender == "archive":
        assert latest.headers["etag"] == source.headers["etag"]
        assert response.json()["source"]["status"] == "ARCHIVED"
        assert response.json()["source"]["clonedFromArchivedDraftId"] == source.json()["id"]
        row = records(state, namespace)[0]
        assert row["source_status"] == "ARCHIVED"
        assert row["source_revision"] == source.json()["revision"]
        assert row["cloned_from_archived_draft_id"] == source.json()["id"]
    else:
        assert records(state, namespace) == []
    accepted = clone(state.c, path, latest)
    assert accepted.json()["source"]["revision"] == latest.json()["revision"]
    assert accepted.json()["source"]["status"] == latest.json()["status"]


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_publication_first_clone_uses_current_locked_version_and_not_source_stale_flag(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str
) -> None:
    state = actors
    path, source = saved_draft(state, namespace, comparison_state(namespace).draft.content)
    owner_class: Any = (
        SqlAlchemyIdentityPolicyOwnerRepository
        if namespace == "identity"
        else SqlAlchemyGatePolicyRepository
    )
    name = "publish_version" if namespace == "identity" else "publish"
    persist = getattr(owner_class, name)
    held, release = Event(), Event()
    first_pids: list[int] = []

    def hold(owner: Any, *args: Any, **values: Any) -> Any:
        result = persist(owner, *args, **values)
        first_pids.append(owner.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        held.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(owner_class, name, hold)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    started, pids, observe = observe_owner_wait(engine, namespace, held)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(publish, state, state.a, path, source)
            try:
                assert held.wait(5)
                pending = pool.submit(clone, state.b, path, source)
                assert started.wait(5)
                assert_postgres_blocked_by(
                    cast(IsolatedAgentDatabase, state.journey.database),
                    waiting_pid=pids[0],
                    blocking_pid=first_pids[0],
                )
            finally:
                release.set()
            assert first.result(timeout=10).json()["version"] == 2
            receipt = pending.result(timeout=10)
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    assert receipt.json()["currentVersionAtClone"] == 2
    assert receipt.json()["draft"]["baseVersion"] == 1 and receipt.json()["draft"]["stale"] is True
    assert state.c.client.get(path).json()["stale"] is (namespace != "identity")
    assert records(state, namespace)[0]["current_version"] == 2


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_default_session_reserved_permission_and_revoked_replay_refuse_before_owner_reads(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, source = saved_draft(state, namespace, {})
    for scope in (None, state.journey.workspace_id):
        _grant(state.c.client, state.journey.member_id, "platform.configuration.manage", scope)
    runtime = bootstrap.configuration_http_runtime()
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve.side_effect = AssertionError("denied clone cannot access owner")
    with injected_client(
        SimpleNamespace(client=state.journey.member), replace(runtime, owners=owners)
    ) as client:
        _write(client, path + "/clone", {}, etag=source.headers["etag"], status=403)
        client.cookies.clear()
        _write(client, path + "/clone", {}, etag=source.headers["etag"], status=401)
    clone(state.b, path, source, key="revoke-clone-replay")
    _write(
        state.c.client,
        f"/api/v1/admin/super-admins/{state.b.id}",
        {"reason": "Current actor revoked", "totpCode": otp(state, state.c.secret)},
        etag=account_etag(state.c.client, state.b.id),
        method="DELETE",
    )
    with injected_client(state.b, replace(runtime, owners=owners)) as client:
        response = client.post(
            path + "/clone",
            json={},
            headers={
                "Origin": "https://testserver",
                "If-Match": source.headers["etag"],
                "Idempotency-Key": "revoke-clone-replay",
            },
        )
        assert response.status_code in {401, 403}
    owners.resolve.assert_not_called()


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("change", ["revoke", "logout"])
@pytest.mark.parametrize("lock_source", [False, True])
def test_current_session_is_rechecked_after_actual_active_or_source_lock_wait(
    actors: Any, namespace: str, change: str, lock_source: bool
) -> None:
    state = actors
    path, source = saved_draft(state, namespace, {})
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    held = Event()
    started, pids, observe = observe_owner_wait(engine, namespace, held)
    if lock_source:
        sql_event.remove(engine, "before_cursor_execute", observe)
        source_table = tables(namespace)[0]

        def observe(
            connection: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
        ) -> None:
            if (
                held.is_set()
                and not pids
                and "FOR UPDATE" in statement.upper()
                and source_table in statement
            ):
                pids.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
                started.set()

        sql_event.listen(engine, "before_cursor_execute", observe)
    before = all_facts(state)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with state.journey.database.owner.begin() as locking:
                blocker = locking.execute(text("SELECT pg_backend_pid()")).scalar_one()
                locking.execute(
                    text(
                        f"SELECT 1 FROM {tables(namespace)[0]} WHERE id=:id FOR UPDATE"
                        if lock_source
                        else (
                            f"SELECT 1 FROM {tables(namespace)[2]} "
                            "WHERE namespace=:namespace AND scope='PLATFORM' FOR UPDATE"
                        )
                    ),
                    {"id": source.json()["id"], "namespace": namespace},
                )
                held.set()
                pending = pool.submit(
                    state.a.client.post,
                    path + "/clone",
                    json={},
                    headers={
                        "Origin": "https://testserver",
                        "Idempotency-Key": "waiting-clone",
                        "If-Match": source.headers["etag"],
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
                            "reason": "Clone waited during revocation",
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
    after = all_facts(state)
    assert records(state, namespace) == []
    assert after[tables(namespace)[0]] == before[tables(namespace)[0]]
    assert after[tables(namespace)[2]] == before[tables(namespace)[2]]
    assert not [row for row in after["audit"] if row["action"] == "configuration.draft.cloned"]


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "fault,status", [("authorization", 503), ("source", 500), ("audit", 500), ("receipt", 500)]
)
def test_real_clone_fault_rolls_back_copy_provenance_audit_and_receipt_and_releases_connection(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, fault: str, status: int
) -> None:
    state = actors
    path, source = saved_draft(state, namespace, {})
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []

    @contextmanager
    def transaction() -> Iterator[Any]:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            if fault == "audit":
                audit = Mock()
                audit.append_in_transaction.side_effect = RuntimeError("synthetic audit fault")
                lifecycle = PolicyLifecycle(
                    lifecycle.db, lifecycle.owner, replace(runtime.dependencies, audit=audit)
                )
            elif fault in {"source", "receipt"}:
                name = "record_clone" if fault == "source" else "complete_idempotency"
                persist = getattr(lifecycle.owner, name)

                def fail_after_write(*args: Any, **kwargs: Any) -> Any:
                    persist(*args, **kwargs)
                    raise RuntimeError("synthetic persistence fault")

                monkeypatch.setattr(lifecycle.owner, name, fail_after_write)
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
        authorizer.check.side_effect = RuntimeError("synthetic current authorization outage")
    before = all_facts(state)
    with injected_client(
        state.a, replace(runtime, owners=owners, draft_authorization=authorizer)
    ) as client:
        _write(client, path + "/clone", {}, etag=source.headers["etag"], status=status)
    assert connections and all(db.closed for db in connections)
    assert all_facts(state) == before
