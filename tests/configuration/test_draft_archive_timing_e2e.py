import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.configuration import PolicyLifecycle, PolicyRuntimeRegistry
from control_plane.app.modules.configuration.application.drafts import _content_hash
from tests.configuration.test_draft_base_comparison_e2e import publish, saved_draft
from tests.configuration.test_draft_clone_e2e import all_facts, clone, copied_path, tables
from tests.configuration.test_draft_takeover_e2e import account_etag, injected_client, otp
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _grant, _write

pytestmark = pytest.mark.integration


def period_key(namespace: str) -> str:
    return "identity.draft_archive_after" if namespace == "identity" else "draft_archive_after_days"


def query(actor: Any, path: str, draft: Any, *, status: int = 200) -> Any:
    response = actor.client.get(
        path + "/archive-timing", headers={"If-Match": draft.headers["etag"]}
    )
    assert response.status_code == status, response.text
    if status == 200:
        assert (
            response.headers["etag"] == draft.headers["etag"]
            and response.headers["cache-control"] == "no-store"
        )
    return response


@contextmanager
def configured_reader(state: Any, namespace: str, configure: Any) -> Iterator[Any]:
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []

    @contextmanager
    def transaction() -> Iterator[Any]:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            yield configure(lifecycle)

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    try:
        yield replace(runtime, owners=owners)
    finally:
        assert connections and all(db.closed for db in connections)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_actual_current_period_not_candidate_or_base_drives_self_other_clone_and_rollback(
    actors: Any, namespace: str
) -> None:
    state = actors
    key = period_key(namespace)
    path, source = saved_draft(state, namespace, {key: 7})
    before = all_facts(state)
    first = query(state.c, path, source).json()
    assert first["archiveAfterDays"] == 30 and first["current"]["values"][key] == 30
    assert first["draft"]["ownerId"] == state.a.id and first["draft"]["baseBehind"] is False
    assert datetime.fromisoformat(first["expectedArchiveAt"]) == datetime.fromisoformat(
        source.json()["lastMeaningfulActivityAt"]
    ) + timedelta(days=30)
    assert all_facts(state) == before
    publication = saved_draft(state, namespace, {key: 2}, actor=state.b)
    assert publish(state, state.b, *publication).json()["version"] == 2
    stale = state.a.client.get(path)
    before = all_facts(state)
    later = query(state.c, path, stale).json()
    assert later["archiveAfterDays"] == 2 and later["current"]["version"] == 2
    assert later["draft"]["baseBehind"] is True
    assert datetime.fromisoformat(later["expectedArchiveAt"]) == datetime.fromisoformat(
        stale.json()["lastMeaningfulActivityAt"]
    ) + timedelta(days=2)
    assert all_facts(state) == before
    copied = clone(state.c, path, stale)
    copy_path = copied_path(namespace, copied)
    copy_detail = state.c.client.get(copy_path)
    before = all_facts(state)
    copy_result = query(state.c, copy_path, copy_detail).json()
    assert copy_result["archiveAfterDays"] == 2 and copy_result["draft"]["baseBehind"] is True
    assert all_facts(state) == before
    rollback = _write(
        state.c.client,
        f"/api/v1/admin/policies/{namespace}/rollback",
        {
            "toVersion": 1,
            "reason": "Timing rollback source",
            "totpCode": otp(state, state.c.secret),
        },
        etag='"v2"',
        status=201,
    )
    rollback_path = f"/api/v1/admin/policies/{namespace}/drafts/{rollback.json()['id']}"
    before = all_facts(state)
    restored = query(state.c, rollback_path, rollback).json()
    assert restored["archiveAfterDays"] == 2 and restored["draft"]["rollbackFromVersion"] == 1
    assert restored["draft"]["baseBehind"] is False
    assert all_facts(state) == before
    _write(
        state.c.client,
        f"/api/v1/admin/accounts/{state.a.id}/disable",
        {"reason": "Original timing owner disabled"},
        etag=account_etag(state.c.client, state.a.id),
        status=204,
    )
    before = all_facts(state)
    disabled = query(state.c, path, stale).json()
    assert disabled["draft"]["ownerId"] == state.a.id and disabled["archiveAfterDays"] == 2
    assert all_facts(state) == before
    bootstrap.configuration_http_runtime().owners.resolve(namespace).archive(
        now=state.clock.value + timedelta(days=31)
    )
    archived = state.c.client.get(path)
    assert archived.headers["etag"] == stale.headers["etag"]
    before = all_facts(state)
    archived_time = query(state.c, path, archived).json()
    assert archived_time["draft"]["status"] == "ARCHIVED"
    assert archived_time["expectedArchiveAt"] is None and archived_time["inactivityElapsed"] is None
    assert archived_time["archiveAfterDays"] == 2 and all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("microsecond,elapsed", [(-1, False), (0, True), (1, True)])
def test_actual_owner_reads_and_injected_server_clock_preserve_exact_boundary_and_offset(
    actors: Any, namespace: str, microsecond: int, elapsed: bool
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {period_key(namespace): 7})
    expected = datetime.fromisoformat(draft.json()["lastMeaningfulActivityAt"]).astimezone(
        UTC
    ) + timedelta(days=30)
    observed = (expected + timedelta(microseconds=microsecond)).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    clock = Mock()
    clock.now.return_value = observed

    def configure(lifecycle: Any) -> Any:
        return PolicyLifecycle(
            lifecycle.db, lifecycle.owner, replace(lifecycle.dependencies, clock=clock)
        )

    before = all_facts(state)
    with (
        configured_reader(state, namespace, configure) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        response = query(SimpleNamespace(client=client), path, draft).json()
    assert datetime.fromisoformat(response["observedAt"]).isoformat() == observed.isoformat()
    assert datetime.fromisoformat(response["expectedArchiveAt"]) == expected
    assert response["inactivityElapsed"] is elapsed
    clock.now.assert_called_once_with()
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("change", ["validate", "archive", "publish"])
def test_real_public_changes_during_read_are_409_and_fresh_observation_uses_new_facts(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, change: str
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})
    publication = (
        saved_draft(state, namespace, {period_key(namespace): 2}, actor=state.b)
        if change == "publish"
        else None
    )
    changed = []
    written = []

    def configure(lifecycle: Any) -> Any:
        original = lifecycle.owner.draft_summary
        calls = 0

        def race(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                if change == "validate":
                    state.clock.value += timedelta(microseconds=1)
                    _write(state.a.client, path + "/validate", {}, etag=draft.headers["etag"])
                elif change == "archive":
                    bootstrap.configuration_http_runtime().owners.resolve(namespace).archive(
                        now=state.clock.value + timedelta(days=31)
                    )
                else:
                    assert publication is not None
                    publish(state, state.b, *publication)
                changed.append(state.c.client.get(path))
                written.append(all_facts(state))
            return result

        monkeypatch.setattr(lifecycle.owner, "draft_summary", race)
        return lifecycle

    with (
        configured_reader(state, namespace, configure) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        query(SimpleNamespace(client=client), path, draft, status=409)
    assert all_facts(state) == written[0]
    latest = changed[0]
    if change == "validate":
        assert latest.json()["revision"] == draft.json()["revision"] + 1
        assert latest.json()["lastMeaningfulActivityAt"] != draft.json()["lastMeaningfulActivityAt"]
        query(state.c, path, draft, status=409)
    if change == "archive":
        assert (
            latest.headers["etag"] == draft.headers["etag"]
            and latest.json()["status"] == "ARCHIVED"
        )
    before = all_facts(state)
    current = query(state.c, path, latest).json()
    if change == "archive":
        assert current["expectedArchiveAt"] is None
    elif change == "publish":
        assert current["archiveAfterDays"] == 2 and current["current"]["version"] == 2
    else:
        assert (
            current["draft"]["lastMeaningfulActivityAt"]
            == latest.json()["lastMeaningfulActivityAt"]
        )
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_timing_uses_only_metadata_and_ordinary_selects_under_real_active_and_draft_write_locks(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})
    baseline = query(state.c, path, draft).json()
    before = all_facts(state)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    selected = []

    def observe(_conn: Any, _cursor: Any, sql: str, _params: Any, _ctx: Any, _many: Any) -> None:
        if re.search(r"FROM\s+" + re.escape(tables(namespace)[0]) + r"\b", sql, re.I):
            selected.append(sql)

    sql_event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with state.journey.database.owner.begin() as db:
                db.execute(
                    text(
                        f"SELECT 1 FROM {tables(namespace)[2]} "
                        "WHERE namespace=:namespace FOR UPDATE"
                    ),
                    {"namespace": namespace},
                )
                db.execute(
                    text(f"SELECT 1 FROM {tables(namespace)[0]} WHERE id=:id FOR UPDATE"),
                    {"id": draft.json()["id"]},
                )
                observed = pool.submit(query, state.c, path, draft).result(timeout=5)
                assert observed.json() == baseline
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    assert len(selected) == 2
    for sql in selected:
        assert "FOR UPDATE" not in sql and "*" not in sql
        assert set(" ".join(sql.split()).split(" FROM ")[0].removeprefix("SELECT ").split(",")) == {
            "id",
            "namespace",
            "scope",
            "owner_id",
            "revision",
            "status",
            "base_version",
            "schema_revision",
            "content_hash",
            "last_meaningful_activity_at",
            "archived_at",
            "rollback_from_version",
        }
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "fault,status",
    [
        ("activity-only-same-revision", 409),
        ("metadata", 503),
        ("unknown-schema", 503),
        ("missing-current", 503),
        ("period", 503),
        ("same-version-period", 503),
        ("same-version-hash", 503),
        ("overflow", 503),
        ("dependency", 503),
    ],
)
def test_injected_bad_facts_and_same_revision_activity_fail_closed_without_writes(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, fault: str, status: int
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})

    def configure(lifecycle: Any) -> Any:
        if fault in {"missing-current", "period", "same-version-period", "same-version-hash"}:
            original = lifecycle.owner.read_archive_settings
            calls = 0

            def bad_settings(name: str) -> Any:
                nonlocal calls
                snapshot, interval = original(name)
                calls += 1
                if fault == "missing-current":
                    return None, interval
                if fault == "period":
                    return snapshot, timedelta(hours=1)
                if calls == 2 and fault == "same-version-period":
                    return snapshot, timedelta(days=1)
                if calls == 2 and fault == "same-version-hash":
                    values = {**snapshot.values, period_key(namespace): 2}
                    return snapshot.model_copy(
                        update={"values": values, "snapshot_hash": _content_hash(values)}
                    ), timedelta(days=2)
                return snapshot, interval

            monkeypatch.setattr(lifecycle.owner, "read_archive_settings", bad_settings)
        else:
            original_summary = lifecycle.owner.draft_summary
            calls = 0

            def bad_summary(*args: Any, **kwargs: Any) -> Any:
                nonlocal calls
                if fault == "dependency":
                    raise RuntimeError("private SQL/metadata fault")
                row = deepcopy(original_summary(*args, **kwargs))
                assert row is not None
                calls += 1
                if fault == "activity-only-same-revision" and calls == 2:
                    row["last_meaningful_activity_at"] += timedelta(microseconds=1)
                if fault == "metadata":
                    row["content_hash"] = "invalid"
                if fault == "unknown-schema":
                    row["schema_revision"] = 2
                if fault == "overflow":
                    row["last_meaningful_activity_at"] = datetime(9999, 12, 31, tzinfo=UTC)
                return row

            monkeypatch.setattr(lifecycle.owner, "draft_summary", bad_summary)
        return lifecycle

    before = all_facts(state)
    with (
        configured_reader(state, namespace, configure) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        response = query(SimpleNamespace(client=client), path, draft, status=status)
        assert "private" not in response.text and "expectedArchiveAt" not in response.json()
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_default_session_denies_before_owner_and_unknown_targets_are_controlled(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, draft = saved_draft(state, namespace, {})
    runtime = bootstrap.configuration_http_runtime()
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve.side_effect = AssertionError("unauthorized timing must not read owner")
    for scope in (None, state.journey.workspace_id):
        _grant(state.a.client, state.journey.member_id, "platform.configuration.manage", scope)
    with injected_client(
        SimpleNamespace(client=state.journey.member), replace(runtime, owners=owners)
    ) as client:
        query(SimpleNamespace(client=client), path, draft, status=403)
        client.cookies.clear()
        query(SimpleNamespace(client=client), path, draft, status=401)
    _write(
        state.c.client,
        f"/api/v1/admin/super-admins/{state.b.id}",
        {"reason": "Timing reader revoked", "totpCode": otp(state, state.c.secret)},
        etag=account_etag(state.c.client, state.b.id),
        method="DELETE",
    )
    with injected_client(state.b, replace(runtime, owners=owners)) as client:
        assert client.get(
            path + "/archive-timing", headers={"If-Match": draft.headers["etag"]}
        ).status_code in {401, 403}
    stale = SimpleNamespace(client=SimpleNamespace(cookies=dict(state.a.client.cookies)))
    _write(state.a.client, "/api/v1/auth/logout", {})
    with injected_client(stale, replace(runtime, owners=owners)) as client:
        query(SimpleNamespace(client=client), path, draft, status=401)
    owners.resolve.assert_not_called()
    before = all_facts(state)
    missing = f"/api/v1/admin/policies/{namespace}/drafts/{uuid4()}"
    query(state.c, missing, draft, status=404)
    other_namespace = "requirement.gate" if namespace == "identity" else "identity"
    query(state.c, path.replace(namespace, other_namespace), draft, status=404)
    query(state.c, path.replace(namespace, "not.registered"), draft, status=503)
    with injected_client(
        state.c, replace(runtime, owners=replace(runtime.owners, requirement_gate=None))
    ) as client:
        query(
            SimpleNamespace(client=client),
            path.replace(namespace, "requirement.gate"),
            draft,
            status=503,
        )
    assert all_facts(state) == before
