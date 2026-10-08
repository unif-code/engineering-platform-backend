import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.configuration import PolicyRuntimeRegistry
from control_plane.app.modules.configuration.application.drafts import _content_hash
from tests.configuration.test_draft_base_comparison_e2e import publish, saved_draft
from tests.configuration.test_draft_clone_e2e import all_facts, clone, copied_path, tables
from tests.configuration.test_draft_rebase_e2e import apply, setup_stale
from tests.configuration.test_draft_takeover_e2e import account_etag, injected_client, otp, takeover
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _grant, _write

pytestmark = pytest.mark.integration


def query(actor: Any, namespace: str, *, status: int = 200, **params: Any) -> Any:
    response = actor.client.get(f"/api/v1/admin/policies/{namespace}/drafts", params=params)
    assert response.status_code == status, response.text
    if status == 200:
        assert response.headers["cache-control"] == "no-store" and "etag" not in response.headers
    return response


def create(actor: Any, namespace: str) -> tuple[str, Any]:
    response = _write(
        actor.client, f"/api/v1/admin/policies/{namespace}/drafts", {"values": {}}, status=201
    )
    return f"/api/v1/admin/policies/{namespace}/drafts/{response.json()['id']}", response


def assert_summary(row: Any, detail: Any, current: int) -> None:
    source = detail.json()
    assert set(row) == {
        "id",
        "namespace",
        "scope",
        "ownerId",
        "revision",
        "status",
        "baseVersion",
        "schemaRevision",
        "contentHash",
        "lastMeaningfulActivityAt",
        "archivedAt",
        "rollbackFromVersion",
        "baseBehind",
    }
    assert {k: v for k, v in row.items() if k != "baseBehind"} == {
        k: source[k] for k in row if k != "baseBehind"
    }
    assert row["baseBehind"] is (row["baseVersion"] < current)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_default_and_all_filters_describe_real_clone_rollback_archived_and_disabled_owners(
    actors: Any, namespace: str
) -> None:
    state = actors
    a_path, a_draft = saved_draft(
        state,
        namespace,
        {"identity.session_cap" if namespace == "identity" else "draft_archive_after_days": 7},
    )
    b_path, b_draft = saved_draft(state, namespace, {}, actor=state.b)
    receipt = clone(state.c, a_path, a_draft)
    clone_path = copied_path(namespace, receipt)
    assert publish(state, state.a, a_path, a_draft).json()["version"] == 2
    publishing_source = state.c.client.get(a_path)
    assert publishing_source.json()["stale"] is (namespace != "identity")
    fresh_path, _ = create(state.c, namespace)
    rollback = _write(
        state.c.client,
        f"/api/v1/admin/policies/{namespace}/rollback",
        {
            "toVersion": 1,
            "reason": "Directory rollback source",
            "totpCode": otp(state, state.c.secret),
        },
        etag='"v2"',
        status=201,
    )
    rollback_path = f"/api/v1/admin/policies/{namespace}/drafts/{rollback.json()['id']}"
    all_paths = [a_path, b_path, clone_path, fresh_path, rollback_path]
    before = all_facts(state)
    page = query(state.c, namespace).json()
    assert page["currentVersion"] == 2 and page["nextCursor"] is None
    expected = {path.rsplit("/", 1)[-1]: state.c.client.get(path) for path in all_paths}
    assert {row["id"] for row in page["items"]} == set(expected)
    assert [row["id"] for row in page["items"]] == sorted(expected)
    for row in page["items"]:
        assert_summary(row, expected[row["id"]], 2)
    assert (
        next(row for row in page["items"] if row["id"] == publishing_source.json()["id"])[
            "baseBehind"
        ]
        is True
    )
    for owner in ["ALL", "MINE"]:
        for view in ["ALL", "ACTIVE", "STALE", "ARCHIVED"]:
            result = query(state.c, namespace, view=view, owner=owner).json()
            wanted = [
                row
                for row in page["items"]
                if (owner == "ALL" or row["ownerId"] == state.c.id)
                and (
                    view == "ALL"
                    or (view == "ARCHIVED" and row["status"] == "ARCHIVED")
                    or (
                        row["status"] == "DRAFT"
                        and (
                            (view == "ACTIVE" and not row["baseBehind"])
                            or (view == "STALE" and row["baseBehind"])
                        )
                    )
                )
            ]
            assert result["items"] == wanted
            assert result["view"] == view and result["owner"] == owner
    assert all_facts(state) == before
    _write(
        state.c.client,
        f"/api/v1/admin/accounts/{state.b.id}/disable",
        {"reason": "Old directory owner disabled"},
        etag=account_etag(state.c.client, state.b.id),
        status=204,
    )
    before = all_facts(state)
    assert any(row["ownerId"] == state.b.id for row in query(state.c, namespace).json()["items"])
    assert all_facts(state) == before
    revisions = {path: state.c.client.get(path).headers["etag"] for path in all_paths}
    bootstrap.configuration_http_runtime().owners.resolve(namespace).archive(
        now=state.clock.value + timedelta(days=31)
    )
    before = all_facts(state)
    archived = query(state.c, namespace, view="ARCHIVED").json()
    assert len(archived["items"]) == len(all_paths)
    for row in archived["items"]:
        detail = state.c.client.get(f"/api/v1/admin/policies/{namespace}/drafts/{row['id']}")
        assert_summary(row, detail, 2)
        assert (
            detail.headers["etag"]
            == revisions[f"/api/v1/admin/policies/{namespace}/drafts/{row['id']}"]
        )
    assert query(state.c, namespace, view="ACTIVE").json()["items"] == []
    assert query(state.c, namespace, view="STALE").json()["items"] == []
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("change", ["takeover", "archive"])
def test_uuid_cursor_accepts_empty_continuation_after_actual_matching_set_changes(
    actors: Any, namespace: str, change: str
) -> None:
    state = actors
    sources = [create(state.c, namespace) for _ in range(2)]
    first = query(state.c, namespace, owner="MINE", view="ACTIVE", limit=1).json()
    assert len(first["items"]) == 1 and first["nextCursor"] == first["items"][0]["id"]
    current = first["currentVersion"]
    second = query(
        state.c,
        namespace,
        owner="MINE",
        view="ACTIVE",
        limit=1,
        cursor=first["nextCursor"],
        current_version=current,
    ).json()
    assert len(second["items"]) == 1 and second["nextCursor"] is None
    assert second["items"][0]["id"] > first["nextCursor"]
    if change == "takeover":
        for path, draft in sources:
            takeover(state, state.b, path, draft.headers["etag"])
    else:
        bootstrap.configuration_http_runtime().owners.resolve(namespace).archive(
            now=state.clock.value + timedelta(days=31)
        )
    before = all_facts(state)
    empty = query(
        state.c,
        namespace,
        owner="MINE",
        view="ACTIVE",
        limit=1,
        cursor=first["nextCursor"],
        current_version=current,
    ).json()
    assert empty["items"] == [] and empty["nextCursor"] is None
    assert query(state.c, namespace, owner="MINE", view="ACTIVE").json()["items"] == []
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_metadata_select_is_ordered_filtered_without_body_detail_history_or_write_locks(
    actors: Any, namespace: str
) -> None:
    state = actors
    paths = [create(state.c, namespace) for _ in range(3)]
    initial = query(state.c, namespace, owner="MINE").json()
    state.clock.value += timedelta(seconds=1)
    _write(
        state.c.client,
        paths[0][0],
        {"values": {}},
        etag=paths[0][1].headers["etag"],
        method="PATCH",
    )
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    selected = []

    def observe(
        _conn: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
    ) -> None:
        if re.search(r"FROM\s+" + re.escape(tables(namespace)[0]) + r"\b", statement, re.I):
            selected.append(statement)

    before = all_facts(state)
    sql_event.listen(engine, "before_cursor_execute", observe)
    try:
        response = query(
            state.c,
            namespace,
            owner="MINE",
            view="ACTIVE",
            limit=1,
            cursor=initial["items"][0]["id"],
            current_version=initial["currentVersion"],
        ).json()
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    assert [row["id"] for row in response["items"]] == [initial["items"][1]["id"]]
    assert len(selected) == 1
    sql = " ".join(selected[0].split())
    columns = sql.split(" FROM ")[0].removeprefix("SELECT ").split(",")
    assert set(columns) == {
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
    assert (
        "ORDER BY id ASC" in sql
        and "LIMIT" in sql
        and "FOR UPDATE" not in sql
        and "OFFSET" not in sql
    )
    assert (
        "owner_id=" in sql and "status='DRAFT'" in sql and "base_version=" in sql and "id>" in sql
    )
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_directory_reads_old_committed_metadata_while_real_active_and_draft_rows_are_locked(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, draft = create(state.c, namespace)
    baseline = query(state.c, namespace).json()
    before = all_facts(state)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with state.journey.database.owner.begin() as db:
            db.execute(
                text(f"SELECT 1 FROM {tables(namespace)[2]} WHERE namespace=:namespace FOR UPDATE"),
                {"namespace": namespace},
            )
            db.execute(
                text(f"SELECT 1 FROM {tables(namespace)[0]} WHERE id=:id FOR UPDATE"),
                {"id": draft.json()["id"]},
            )
            observed = pool.submit(query, state.c, namespace).result(timeout=5)
            assert observed.json() == baseline
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_publication_between_current_reads_is_409_before_new_draft_base_validation(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str
) -> None:
    state = actors
    create(state.c, namespace)
    publication = saved_draft(
        state,
        namespace,
        {"identity.session_cap" if namespace == "identity" else "draft_archive_after_days": 6},
        actor=state.b,
    )
    first = query(state.c, namespace, limit=1).json()
    assert first["nextCursor"] is not None
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []
    written = []

    @contextmanager
    def transaction() -> Iterator[Any]:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            original = lifecycle.owner.list_draft_summaries

            def race(*args: Any, **kwargs: Any) -> Any:
                assert publish(state, state.b, *publication).json()["version"] == 2
                _, new = create(state.c, namespace)
                assert new.json()["baseVersion"] == 2
                written.append(all_facts(state))
                return original(*args, **kwargs)

            monkeypatch.setattr(lifecycle.owner, "list_draft_summaries", race)
            yield lifecycle

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    with injected_client(state.c, replace(runtime, owners=owners)) as client:
        query(SimpleNamespace(client=client), namespace, status=409)
    assert all(db.closed for db in connections) and all_facts(state) == written[0]
    before = all_facts(state)
    query(state.c, namespace, status=409, limit=1, cursor=first["nextCursor"], current_version=1)
    renewed = query(state.c, namespace).json()
    assert renewed["currentVersion"] == 2
    assert any(row["baseVersion"] == 2 and not row["baseBehind"] for row in renewed["items"])
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_session_reserved_qualification_and_unconfigured_namespace_fail_closed(
    actors: Any, namespace: str
) -> None:
    state = actors
    create(state.a, namespace)
    runtime = bootstrap.configuration_http_runtime()
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve.side_effect = AssertionError("denied directory must not read owners")
    for scope in (None, state.journey.workspace_id):
        _grant(state.a.client, state.journey.member_id, "platform.configuration.manage", scope)
    with injected_client(
        SimpleNamespace(client=state.journey.member), replace(runtime, owners=owners)
    ) as client:
        query(SimpleNamespace(client=client), namespace, status=403)
        client.cookies.clear()
        query(SimpleNamespace(client=client), namespace, status=401)
    _write(
        state.c.client,
        f"/api/v1/admin/super-admins/{state.b.id}",
        {"reason": "Directory reader revoked", "totpCode": otp(state, state.c.secret)},
        etag=account_etag(state.c.client, state.b.id),
        method="DELETE",
    )
    with injected_client(state.b, replace(runtime, owners=owners)) as client:
        assert client.get(f"/api/v1/admin/policies/{namespace}/drafts").status_code in {401, 403}
    old_session = SimpleNamespace(client=SimpleNamespace(cookies=dict(state.a.client.cookies)))
    _write(state.a.client, "/api/v1/auth/logout", {})
    with injected_client(old_session, replace(runtime, owners=owners)) as client:
        query(SimpleNamespace(client=client), namespace, status=401)
    owners.resolve.assert_not_called()
    before = all_facts(state)
    query(state.c, "unregistered.namespace", status=503)
    with injected_client(
        state.c, replace(runtime, owners=replace(runtime.owners, requirement_gate=None))
    ) as client:
        query(SimpleNamespace(client=client), "requirement.gate", status=503)
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "fault",
    [
        "metadata",
        "duplicate",
        "order",
        "future-base",
        "lookahead",
        "dependency",
        "same-version-facts",
    ],
)
def test_faults_fail_the_entire_page_release_connections_and_leave_all_facts_unchanged(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, fault: str
) -> None:
    state = actors
    for _ in range(3):
        create(state.c, namespace)
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []

    @contextmanager
    def transaction() -> Iterator[Any]:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            if fault == "same-version-facts":
                original_current = lifecycle.owner.active_snapshot
                calls = 0

                def changed(name: str) -> Any:
                    nonlocal calls
                    value = original_current(name)
                    calls += 1
                    if calls == 2:
                        content = {
                            **value.values,
                            (
                                "identity.session_cap"
                                if namespace == "identity"
                                else "draft_archive_after_days"
                            ): 8,
                        }
                        return value.model_copy(
                            update={"values": content, "snapshot_hash": _content_hash(content)}
                        )
                    return value

                monkeypatch.setattr(lifecycle.owner, "active_snapshot", changed)
            else:
                original = lifecycle.owner.list_draft_summaries

                def corrupt(*args: Any, **kwargs: Any) -> Any:
                    if fault == "dependency":
                        raise RuntimeError("private SELECT failure")
                    rows = deepcopy(original(*args, **kwargs))
                    if fault == "metadata":
                        rows[0]["archived_at"] = state.clock.value
                    elif fault == "duplicate":
                        rows[1] = deepcopy(rows[0])
                    elif fault == "order":
                        rows.reverse()
                    elif fault == "future-base":
                        rows[0]["base_version"] = 99
                    elif fault == "lookahead":
                        rows[-1]["schema_revision"] = 2
                    return rows

                monkeypatch.setattr(lifecycle.owner, "list_draft_summaries", corrupt)
            yield lifecycle

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    before = all_facts(state)
    with injected_client(state.c, replace(runtime, owners=owners)) as client:
        response = query(
            SimpleNamespace(client=client),
            namespace,
            status=503,
            limit=1 if fault == "lookahead" else 50,
        )
        assert "private" not in response.text and "items" not in response.json()
    assert connections and all(db.closed for db in connections)
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_actual_rebase_moves_same_uuid_from_stale_to_active_without_freezing_collection(
    actors: Any,
    namespace: str,
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    before = all_facts(state)
    stale = query(state.a, namespace, view="STALE", owner="MINE").json()
    row = next(item for item in stale["items"] if item["id"] == target.draft.json()["id"])
    assert row["baseBehind"] is True
    assert all_facts(state) == before
    rebased = apply(state.a, target)
    before = all_facts(state)
    active = query(
        state.a, namespace, view="ACTIVE", owner="MINE", current_version=stale["currentVersion"]
    ).json()
    changed = next(item for item in active["items"] if item["id"] == row["id"])
    assert_summary(changed, rebased, stale["currentVersion"])
    assert changed["baseBehind"] is False and changed["revision"] == row["revision"] + 1
    assert all(
        item["id"] != row["id"]
        for item in query(state.a, namespace, view="STALE", owner="MINE").json()["items"]
    )
    assert all_facts(state) == before
