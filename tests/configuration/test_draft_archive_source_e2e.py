import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.configuration import PolicyRuntimeRegistry
from tests.configuration.test_draft_archive_timing_e2e import configured_reader
from tests.configuration.test_draft_base_comparison_e2e import publish, saved_draft
from tests.configuration.test_draft_clone_e2e import all_facts, tables
from tests.configuration.test_draft_manual_archive_e2e import archive, outbox_table
from tests.configuration.test_draft_takeover_e2e import account_etag, injected_client, otp
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _grant, _write

pytestmark = pytest.mark.integration


def query(actor: Any, path: str, target: Any, *, status: int = 200) -> Any:
    response = actor.client.get(
        path + "/archive-source", headers={"If-Match": target.headers["etag"]}
    )
    assert response.status_code == status, response.text
    if status == 200:
        assert response.headers["etag"] == target.headers["etag"]
        assert response.headers["cache-control"] == "no-store"
    return response


def archived(state: Any, namespace: str, source: str) -> tuple[str, Any]:
    path, target = saved_draft(state, namespace, {})
    if source == "MANUAL":
        archive(state.a, path, target)
    else:
        assert (
            bootstrap.configuration_http_runtime()
            .owners.resolve(namespace)
            .archive(now=state.clock.value + timedelta(days=31))
            == 1
        )
    return path, state.a.client.get(path)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("source", ["MANUAL", "INACTIVITY"])
def test_real_sources_remain_historical_after_policy_publication_and_owner_disable(
    actors: Any, namespace: str, source: str
) -> None:
    state = actors
    path, target = archived(state, namespace, source)
    before = all_facts(state)
    response = query(state.c, path, target).json()
    assert all_facts(state) == before
    record = response["record"]
    assert response["recordStatus"] == "RECORDED" and response["status"] == "ARCHIVED"
    assert record["source"] == source and datetime.fromisoformat(
        record["recordedAt"]
    ) == datetime.fromisoformat(target.json()["archivedAt"])
    if source == "MANUAL":
        assert (
            record["actorId"] == state.a.id and record["draftRevision"] == target.json()["revision"]
        )
        assert "policyVersion" not in record and "cutoff" not in record
    else:
        assert (
            record["policyVersion"] == 1
            and "actorId" not in record
            and "draftRevision" not in record
        )
    key = "identity.session_cap" if namespace == "identity" else "draft_archive_after_days"
    publication = saved_draft(state, namespace, {key: 7}, actor=state.b)
    assert publish(state, state.b, *publication).json()["version"] == 2
    _write(
        state.c.client,
        f"/api/v1/admin/accounts/{state.a.id}/disable",
        {"reason": "Historical archive owner disabled"},
        etag=account_etag(state.c.client, state.a.id),
        status=204,
    )
    before = all_facts(state)
    with (
        configured_reader(state, namespace, lambda lifecycle: lifecycle) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        assert query(SimpleNamespace(client=client), path, target).json() == response
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_unarchived_and_missing_historical_record_are_distinct_without_audit_fallback(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, target = saved_draft(state, namespace, {})
    before = all_facts(state)
    pending = query(state.c, path, target).json()
    assert (
        pending["recordStatus"] == "NOT_ARCHIVED"
        and pending["record"] is None
        and pending["archivedAt"] is None
    )
    assert all_facts(state) == before
    archive(state.a, path, target)
    target = state.c.client.get(path)
    # Missing-history fixture: keep the real Archive Audit intact, without using it as a fallback.
    with state.journey.database.owner.begin() as db:
        db.execute(text(f"DELETE FROM {outbox_table(namespace)} WHERE event_type='DRAFT_ARCHIVED'"))
    before = all_facts(state)
    value = query(state.c, path, target).json()
    assert value["recordStatus"] == "UNKNOWN" and value["record"] is None
    assert value["archivedAt"] == target.json()["archivedAt"]
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "fault",
    [
        "manual-actor",
        "manual-revision",
        "extra",
        "result",
        "occurred-at",
        "missing-inactivity-policy",
    ],
)
def test_actual_corrupt_event_rows_are_unavailable_and_read_releases_connection(
    actors: Any, namespace: str, fault: str
) -> None:
    state = actors
    path, target = archived(
        state, namespace, "INACTIVITY" if fault == "missing-inactivity-policy" else "MANUAL"
    )
    table = outbox_table(namespace)
    with state.journey.database.owner.begin() as db:
        row = (
            db.execute(
                text(
                    f"SELECT id,payload,occurred_at FROM {table} WHERE event_type='DRAFT_ARCHIVED'"
                )
            )
            .mappings()
            .one()
        )
        payload = deepcopy(row["payload"])
        occurred_at = row["occurred_at"]
        if fault == "manual-actor":
            payload["actorId"] = state.b.id
        elif fault == "manual-revision":
            payload["draftRevision"] = True
        elif fault == "extra":
            payload["private-extra"] = True
        elif fault == "result":
            payload["result"] = "SKIPPED"
        elif fault == "occurred-at":
            occurred_at += timedelta(microseconds=1)
        else:
            del payload["policyVersion"]
        db.execute(
            text(
                f"UPDATE {table} SET payload=CAST(:payload AS JSONB),occurred_at=:time WHERE id=:id"
            ),
            {"payload": json.dumps(payload), "time": occurred_at, "id": row["id"]},
        )
    before = all_facts(state)
    with (
        configured_reader(state, namespace, lambda lifecycle: lifecycle) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        response = query(SimpleNamespace(client=client), path, target, status=503)
        assert "recordStatus" not in response.json() and "private-extra" not in response.text
    assert all_facts(state) == before


def test_requirement_duplicate_records_are_detected_by_bounded_read_not_deduplicated(
    actors: Any,
) -> None:
    state = actors
    namespace = "requirement.gate"
    path, target = archived(state, namespace, "MANUAL")
    table = outbox_table(namespace)
    with state.journey.database.owner.begin() as db:
        for _ in range(2):
            db.execute(
                text(
                    f"INSERT INTO {table} "
                    "(id,namespace,scope,event_type,aggregate_id,payload,occurred_at) "
                    "SELECT :id,namespace,scope,event_type,aggregate_id,payload,occurred_at "
                    f"FROM {table} "
                    "WHERE event_type='DRAFT_ARCHIVED' LIMIT 1"
                ),
                {"id": str(uuid4())},
            )
    before = all_facts(state)
    query(state.c, path, target, status=503)
    assert len(before[table]) == 3 and all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("source", ["MANUAL", "INACTIVITY"])
def test_source_query_observes_same_revision_archive_race_as_409_then_reads_new_fact(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, source: str
) -> None:
    state = actors
    path, target = saved_draft(state, namespace, {})
    committed = []

    def configure(lifecycle: Any) -> Any:
        original = lifecycle.owner.draft_summary
        calls = 0

        def race(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            row = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                if source == "MANUAL":
                    archive(state.a, path, target)
                else:
                    bootstrap.configuration_http_runtime().owners.resolve(namespace).archive(
                        now=state.clock.value + timedelta(days=31)
                    )
                committed.append(all_facts(state))
            return row

        monkeypatch.setattr(lifecycle.owner, "draft_summary", race)
        return lifecycle

    with (
        configured_reader(state, namespace, configure) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        query(SimpleNamespace(client=client), path, target, status=409)
    assert all_facts(state) == committed[0]
    before = all_facts(state)
    value = query(
        state.c, path, target
    ).json()  # Same ETag, but a new legitimate ARCHIVED observation.
    assert (
        value["draftRevision"] == target.json()["revision"] and value["record"]["source"] == source
    )
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("source", ["MANUAL", "INACTIVITY"])
def test_only_summary_and_business_event_columns_are_read_under_real_write_locks(
    actors: Any, namespace: str, source: str
) -> None:
    state = actors
    path, target = archived(state, namespace, source)
    expected = query(state.c, path, target).json()
    before = all_facts(state)
    table = outbox_table(namespace)
    draft_table = tables(namespace)[0]
    if namespace == "identity":
        with state.journey.database.owner.begin() as db:
            db.execute(
                text(
                    f"UPDATE {table} SET delivered_at=occurred_at WHERE event_type='DRAFT_ARCHIVED'"
                )
            )
        before = all_facts(state)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    statements = []

    def observe(
        _conn: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
    ) -> None:
        if f"FROM {table} " in statement or f"FROM {draft_table} " in statement:
            statements.append(statement)

    sql_event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with state.journey.database.owner.begin() as db:
                db.execute(
                    text(f"SELECT 1 FROM {draft_table} WHERE id=:id FOR UPDATE"),
                    {"id": target.json()["id"]},
                )
                db.execute(
                    text(f"SELECT 1 FROM {table} WHERE event_type='DRAFT_ARCHIVED' FOR UPDATE")
                )
                db.execute(
                    text(f"SELECT 1 FROM {tables(namespace)[2]} WHERE namespace=:ns FOR UPDATE"),
                    {"ns": namespace},
                )
                response = pool.submit(query, state.c, path, target).result(timeout=5)
                assert response.json() == expected
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    assert len(statements) == 3 and all(
        "FOR UPDATE" not in sql and "*" not in sql for sql in statements
    )
    record_sql = next(sql for sql in statements if f"FROM {table} " in sql)
    assert "LIMIT 2" in record_sql and "delivered_at" not in record_sql
    assert set(record_sql.split(" FROM ")[0].removeprefix("SELECT ").split(",")) == {
        "id",
        "namespace",
        "scope",
        "event_type",
        "aggregate_id",
        "payload",
        "occurred_at",
    }
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_current_session_reserved_qualification_denies_before_owner_reads(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, target = archived(state, namespace, "MANUAL")
    runtime = bootstrap.configuration_http_runtime()
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve.side_effect = AssertionError("denied source query cannot read owner")
    for scope in (None, state.journey.workspace_id):
        _grant(state.a.client, state.journey.member_id, "platform.configuration.manage", scope)
    with injected_client(
        SimpleNamespace(client=state.journey.member), replace(runtime, owners=owners)
    ) as client:
        query(SimpleNamespace(client=client), path, target, status=403)
        client.cookies.clear()
        query(SimpleNamespace(client=client), path, target, status=401)
    query(state.b, path, target)
    _write(
        state.c.client,
        f"/api/v1/admin/super-admins/{state.b.id}",
        {
            "reason": "Source reader revoked",
            "totpCode": otp(state, state.c.secret),
        },
        etag=account_etag(state.c.client, state.b.id),
        method="DELETE",
    )
    with injected_client(state.b, replace(runtime, owners=owners)) as client:
        response = client.get(
            path + "/archive-source", headers={"If-Match": target.headers["etag"]}
        )
        assert response.status_code in {401, 403}
    owners.resolve.assert_not_called()
    query(state.c, f"/api/v1/admin/policies/{namespace}/drafts/{uuid4()}", target, status=404)
    query(state.c, path.replace(namespace, "not.registered"), target, status=503)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_migrations_grant_only_business_read_columns_and_keep_source_index_nonunique(
    actors: Any, namespace: str
) -> None:
    state = actors
    role = "identity_rw" if namespace == "identity" else "requirement_rw"
    table = outbox_table(namespace)
    with state.journey.database.owner.connect() as db:
        for column in (
            "id",
            "namespace",
            "scope",
            "event_type",
            "aggregate_id",
            "payload",
            "occurred_at",
        ):
            assert (
                db.execute(
                    text("SELECT has_column_privilege(:role,:table,:column,'SELECT')"),
                    {"role": role, "table": table, "column": column},
                ).scalar_one()
                is True
            )
        for privilege in ("UPDATE", "DELETE"):
            assert (
                db.execute(
                    text("SELECT has_table_privilege(:role,:table,:privilege)"),
                    {"role": role, "table": table, "privilege": privilege},
                ).scalar_one()
                is False
            )
        if namespace == "identity":
            assert (
                db.execute(
                    text(
                        "SELECT has_column_privilege('identity_rw',"
                        "'identity.configuration_outbox','delivered_at','SELECT')"
                    )
                ).scalar_one()
                is False
            )
            assert (
                db.execute(
                    text(
                        "SELECT has_column_privilege('configuration_rw',"
                        "'identity.configuration_outbox','id','SELECT')"
                    )
                ).scalar_one()
                is False
            )
        else:
            definition = db.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname='requirement' "
                    "AND indexname='ix_req_policy_archive_source'"
                )
            ).scalar_one()
            assert (
                "UNIQUE" not in definition
                and "aggregate_id" in definition
                and "DRAFT_ARCHIVED" in definition
            )


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("fault", ["event-dependency", "metadata"])
def test_owner_read_failure_is_controlled_without_writes_and_releases_connection(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, fault: str
) -> None:
    state = actors
    path, target = archived(state, namespace, "MANUAL")

    def configure(lifecycle: Any) -> Any:
        if fault == "event-dependency":
            monkeypatch.setattr(
                lifecycle.owner,
                "archive_records",
                Mock(side_effect=RuntimeError("private source dependency")),
            )
        else:
            original = lifecycle.owner.draft_summary

            def bad(*args: Any, **kwargs: Any) -> Any:
                return {**original(*args, **kwargs), "content_hash": "private-bad-metadata"}

            monkeypatch.setattr(lifecycle.owner, "draft_summary", bad)
        return lifecycle

    before = all_facts(state)
    with (
        configured_reader(state, namespace, configure) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        response = query(SimpleNamespace(client=client), path, target, status=503)
        assert "private" not in response.text
    assert all_facts(state) == before
