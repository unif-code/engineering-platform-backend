import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
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
from control_plane.app.modules.requirement.domain.gate_policy import MAX_ARCHIVE_DAYS
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_control import assert_postgres_blocked_by
from tests.configuration.test_draft_base_comparison import comparison_state
from tests.configuration.test_draft_base_comparison_e2e import publish, read_comparison, saved_draft
from tests.configuration.test_draft_takeover_e2e import (
    account_etag,
    injected_client,
    otp,
    policy_facts,
    takeover,
)
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _grant, _write

pytestmark = pytest.mark.integration


def history_table(namespace: str) -> str:
    return "identity.draft_rebase" if namespace == "identity" else "requirement.gate_policy_rebase"


def all_facts(state: Any) -> Any:
    facts = policy_facts(state)
    with state.journey.database.owner.connect() as db:
        for namespace in ("identity", "requirement.gate"):
            table = history_table(namespace)
            facts[table] = (
                db.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")).scalars().all()
            )
    return facts


def body_for(response: Any, choice: str = "CURRENT", custom: Any = None) -> dict[str, Any]:
    value = response.json()
    body = {
        key: value[key]
        for key in (
            "baseVersion",
            "currentVersion",
            "schemaRevision",
            "baseSnapshotHash",
            "currentSnapshotHash",
            "draftContentHash",
        )
    }
    body["resolutions"] = {
        item["key"]: {"choice": choice, **({"value": custom} if choice == "CUSTOM" else {})}
        for item in value["items"]
        if item["change"] == "CONFLICT"
    }
    return body


def setup_stale(state: Any, namespace: str) -> Any:
    values = comparison_state(namespace)
    path, draft = saved_draft(state, namespace, values.draft.content)
    current_path, current_draft = saved_draft(
        state, namespace, values.current.values, actor=state.b
    )
    assert publish(state, state.b, current_path, current_draft).json()["version"] == 2
    stale = state.a.client.get(path)
    assert stale.json()["stale"] is True
    comparison = read_comparison(state.a, path, stale)
    return SimpleNamespace(
        path=path,
        draft=stale,
        comparison=comparison,
        body=body_for(comparison),
        current_path=current_path,
    )


def apply(
    actor: Any,
    target: Any,
    *,
    body: dict[str, Any] | None = None,
    etag: str | None = None,
    key: str | None = None,
    status: int = 200,
) -> Any:
    return _write(
        actor.client,
        target.path + "/rebase",
        target.body if body is None else body,
        etag=target.draft.headers["etag"] if etag is None else etag,
        key=key,
        status=status,
    )


def records(state: Any, namespace: str) -> list[Any]:
    with state.journey.database.owner.connect() as db:
        return list(
            db.execute(
                text(
                    f"SELECT to_jsonb(t) FROM {history_table(namespace)} t ORDER BY recorded_at,id"
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_rebase_custom_updates_only_target_and_history_then_fresh_publication_still_works(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    body = body_for(target.comparison, "CUSTOM", 6 if namespace == "identity" else 15)
    before = all_facts(state)
    state.clock.value += timedelta(seconds=1)
    response = apply(state.a, target, body=body, key="custom-rebase")
    value = response.json()
    assert value["id"] == target.draft.json()["id"] and value["ownerId"] == state.a.id
    assert value["baseVersion"] == 2 and value["revision"] == target.draft.json()["revision"] + 1
    assert (
        value["stale"] is False
        and value["validationEvidence"] is None
        and value["previewEvidence"] is None
    )
    assert value["lastMeaningfulActivityAt"] != target.draft.json()["lastMeaningfulActivityAt"]
    after = all_facts(state)
    for table in (
        "identity.version",
        "identity.active_pointer",
        "requirement.gate_policy_version",
        "requirement.gate_policy_active_pointer",
        "identity.policy_reauth_consumption",
        "identity.configuration_outbox",
        "requirement.gate_policy_outbox",
        "totp",
    ):
        assert after[table] == before[table]
    row = records(state, namespace)[0]
    assert (
        row["before_content"] == target.draft.json()["content"]
        and row["after_content"] == value["content"]
    )
    assert (
        row["before_content_hash"] == target.draft.json()["contentHash"]
        and row["after_content_hash"] == value["contentHash"]
    )
    assert row["base_version"] == 1 and row["current_version"] == 2
    assert row["after_revision"] == row["before_revision"] + 1 == value["revision"]
    assert set(row["selections"]) == set(value["content"])
    audit = [event for event in after["audit"] if event["action"] == "configuration.draft.rebased"]
    assert len(audit) == 1 and json.loads(audit[0]["reason"])["id"] == row["id"]
    assert "before_content" not in json.loads(audit[0]["reason"])
    assert all(
        set(item) == {"change", "source"}
        for item in json.loads(audit[0]["reason"])["selections"].values()
    )
    raw_session = state.a.client.cookies.get("ep_session")
    assert (
        raw_session and raw_session not in json.dumps(row) and raw_session not in audit[0]["reason"]
    )
    _write(
        state.a.client,
        target.path + "/publish",
        {"reason": "Old publish context", "totpCode": "000000"},
        etag=target.draft.headers["etag"],
        status=409,
    )
    validated = _write(state.a.client, target.path + "/validate", {}, etag=response.headers["etag"])
    assert validated.json()["valid"] is True
    preview = state.a.client.get(
        target.path + "/preview", headers={"If-Match": validated.headers["etag"]}
    )
    assert preview.status_code == 200
    assert publish(state, state.a, target.path, preview).json()["version"] == 3
    assert records(state, namespace) == [row]

    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    table = history_table(namespace)
    with engine.connect() as db:
        assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 1
    for statement in (f"UPDATE {table} SET recorded_at=recorded_at", f"DELETE FROM {table}"):
        with pytest.raises(ProgrammingError), engine.begin() as db:
            db.execute(text(statement))
    assert records(state, namespace) == [row]
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.execute(text(f"INSERT INTO {table} SELECT * FROM {table}"))
    assert records(state, namespace) == [row]


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_no_conflict_base_advance_uses_owner_publication_facts_and_clears_evidence(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    source = state.b.client.get(target.current_path)
    assert source.json()["baseVersion"] == 1
    if namespace == "identity":
        assert source.json()["stale"] is False
        assert source.json()["validationEvidence"] is not None
        assert source.json()["previewEvidence"] is not None
    else:
        assert source.json()["stale"] is True
        assert source.json()["validationEvidence"] is None
        assert source.json()["previewEvidence"] is None
    observation = read_comparison(state.b, target.current_path, source)
    body = body_for(observation)
    assert body["resolutions"] == {}
    h = SimpleNamespace(path=target.current_path, draft=source, body=body)
    response = apply(state.b, h)
    assert response.json()["content"] == source.json()["content"]
    assert (
        response.json()["baseVersion"] == 2
        and response.json()["revision"] == source.json()["revision"] + 1
    )
    table = "identity.draft" if namespace == "identity" else "requirement.gate_policy_draft"
    with state.journey.database.owner.connect() as db:
        row = (
            db.execute(text(f"SELECT * FROM {table} WHERE id=:id"), {"id": response.json()["id"]})
            .mappings()
            .one()
        )
        assert all(
            value is None
            for key, value in row.items()
            if key.startswith(("validation_", "preview_"))
        )
    assert len(records(state, namespace)) == 1
    current = read_comparison(state.b, target.current_path, response)
    apply(state.b, h, body=body_for(current), etag=response.headers["etag"], status=409)
    assert len(records(state, namespace)) == 1


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_owner_revision_and_reserved_permission_are_enforced_before_apply(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    apply(state.b, target, status=403)
    taken = takeover(state, state.b, target.path, target.draft.headers["etag"])
    apply(state.a, target, status=409)
    apply(state.a, target, etag=taken.headers["etag"], status=403)
    observation = read_comparison(state.b, target.path, taken)
    accepted = apply(state.b, target, body=body_for(observation), etag=taken.headers["etag"])
    assert accepted.json()["ownerId"] == state.b.id
    for scope in (None, state.journey.workspace_id):
        _grant(state.c.client, state.journey.member_id, "platform.configuration.manage", scope)
    runtime = bootstrap.configuration_http_runtime()
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve.side_effect = AssertionError("unauthorized Apply cannot access owner")
    with injected_client(
        SimpleNamespace(client=state.journey.member), replace(runtime, owners=owners)
    ) as client:
        _write(
            client,
            target.path + "/rebase",
            target.body,
            etag=target.draft.headers["etag"],
            status=403,
        )
        client.cookies.clear()
        _write(
            client,
            target.path + "/rebase",
            target.body,
            etag=target.draft.headers["etag"],
            status=401,
        )
    owners.resolve.assert_not_called()


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_history_and_original_receipt_survive_new_current_second_rebase_takeover_and_archive(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    target.body = body_for(target.comparison, "DRAFT")
    first = apply(state.a, target, key="original-rebase")
    first_record = records(state, namespace)[0]
    values = (
        {"identity.session_cap": 3} if namespace == "identity" else {"draft_archive_after_days": 5}
    )
    path, draft = saved_draft(state, namespace, values, actor=state.b)
    assert publish(state, state.b, path, draft).json()["version"] == 3
    stale = state.a.client.get(target.path)
    assert stale.json()["stale"] is True
    observed = read_comparison(state.a, target.path, stale)
    second = apply(
        state.a,
        target,
        body=body_for(observed, "DRAFT"),
        etag=stale.headers["etag"],
        key="second-rebase",
    )
    assert second.json()["baseVersion"] == 3
    history = records(state, namespace)
    assert len(history) == 2 and history[0] == first_record
    taken = takeover(state, state.b, target.path, second.headers["etag"])
    runtime = bootstrap.configuration_http_runtime()
    assert (
        runtime.owners.resolve(namespace).archive(now=state.clock.value + timedelta(days=31)) >= 1
    )
    assert state.b.client.get(target.path).json()["status"] == "ARCHIVED"
    blocked = Mock()
    blocked.check.side_effect = AssertionError(
        "historical replay cannot run commit-time authorization"
    )
    before = all_facts(state)
    with injected_client(state.a, replace(runtime, draft_authorization=blocked)) as client:
        replay = _write(
            client,
            target.path + "/rebase",
            target.body,
            etag=target.draft.headers["etag"],
            key="original-rebase",
        )
    assert replay.content == first.content and replay.headers["etag"] == first.headers["etag"]
    blocked.check.assert_not_called()
    assert all_facts(state) == before
    assert records(state, namespace) == history
    changed = deepcopy(target.body)
    changed["currentSnapshotHash"] = "f" * 64
    apply(state.a, target, body=changed, key="original-rebase", status=409)
    apply(state.a, target, etag=taken.headers["etag"], key="original-rebase", status=409)


def observe_owner_wait(
    engine: Any,
    namespace: str,
    held: Event,
    *,
    archive_update: bool = False,
) -> tuple[Event, list[int], Any]:
    started = Event()
    pids: list[int] = []
    tables = (
        ("identity.active_pointer", "identity.draft")
        if namespace == "identity"
        else ("requirement.gate_policy_active_pointer", "requirement.gate_policy_draft")
    )

    def observe(
        connection: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
    ) -> None:
        locking = "FOR UPDATE" in statement.upper() or (
            archive_update
            and statement.upper().lstrip().startswith("UPDATE IDENTITY.DRAFT SET STATUS='ARCHIVED'")
        )
        if held.is_set() and not pids and locking and any(table in statement for table in tables):
            pids.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()

    sql_event.listen(engine, "before_cursor_execute", observe)
    return started, pids, observe


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("change", ["revoke", "logout"])
def test_public_revocation_or_logout_during_actual_active_lock_wait_blocks_commit(
    actors: Any,
    namespace: str,
    change: str,
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    table = (
        "identity.active_pointer"
        if namespace == "identity"
        else "requirement.gate_policy_active_pointer"
    )
    held = Event()
    started, pids, observe = observe_owner_wait(engine, namespace, held)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with state.journey.database.owner.begin() as locking:
                blocking_pid = locking.execute(text("SELECT pg_backend_pid()")).scalar_one()
                locking.execute(
                    text(
                        f"SELECT namespace FROM {table} "
                        "WHERE namespace=:namespace AND scope='PLATFORM' FOR UPDATE"
                    ),
                    {"namespace": namespace},
                )
                held.set()
                pending = pool.submit(
                    state.a.client.post,
                    target.path + "/rebase",
                    json=target.body,
                    headers={
                        "Origin": "https://testserver",
                        "Idempotency-Key": "waiting-rebase",
                        "If-Match": target.draft.headers["etag"],
                    },
                )
                assert started.wait(5)
                assert_postgres_blocked_by(
                    cast(IsolatedAgentDatabase, state.journey.database),
                    waiting_pid=pids[0],
                    blocking_pid=blocking_pid,
                )
                if change == "revoke":
                    _write(
                        state.c.client,
                        f"/api/v1/admin/super-admins/{state.a.id}",
                        {
                            "reason": "Qualification changed while rebase waited",
                            "totpCode": otp(state, state.c.secret),
                        },
                        etag=account_etag(state.c.client, state.a.id),
                        method="DELETE",
                    )
                else:
                    _write(state.a.client, "/api/v1/auth/logout", {})
            result = pending.result(timeout=10)
            assert result.status_code in ({401, 403} if change == "revoke" else {401}), result.text
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    assert records(state, namespace) == []
    assert state.c.client.get(target.path).json() == target.draft.json()
    assert not [
        event
        for event in all_facts(state)["audit"]
        if event["action"] == "configuration.draft.rebased"
    ]


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("contender", ["rebase", "edit", "takeover", "archive", "publish"])
def test_apply_first_serializes_real_contenders_and_never_loses_history(
    actors: Any,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    contender: str,
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    publication = None
    if contender == "publish":
        values = (
            {"identity.session_cap": 3}
            if namespace == "identity"
            else {"draft_archive_after_days": 5}
        )
        publication = saved_draft(state, namespace, values, actor=state.b)
    owner_class: Any = (
        IdentityPolicyOwner if namespace == "identity" else SqlAlchemyGatePolicyRepository
    )
    persist = owner_class.rebase_draft
    held, release = Event(), Event()
    first_pids = []

    def hold_after_update(owner: Any, *args: Any, **kwargs: Any) -> Any:
        result = persist(owner, *args, **kwargs)
        assert result is not None
        first_pids.append(owner.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        held.set()
        assert release.wait(10), "test did not release Rebase transaction"
        return result

    monkeypatch.setattr(owner_class, "rebase_draft", hold_after_update)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    started, pids, observe = observe_owner_wait(
        engine,
        namespace,
        held,
        archive_update=namespace == "identity" and contender == "archive",
    )
    state.clock.value += timedelta(seconds=1)

    def compete() -> Any:
        if contender == "rebase":
            return apply(state.a, target, key="second-concurrent", status=409)
        if contender == "edit":
            return _write(
                state.a.client,
                target.path,
                {"values": {}},
                etag=target.draft.headers["etag"],
                method="PATCH",
                status=409,
            )
        if contender == "takeover":
            return takeover(state, state.b, target.path, target.draft.headers["etag"], status=409)
        if contender == "publish":
            assert publication is not None
            return publish(state, state.b, *publication)
        days = target.comparison.json()["items"]
        key = (
            "identity.draft_archive_after"
            if namespace == "identity"
            else "draft_archive_after_days"
        )
        interval = next(item["currentValue"] for item in days if item["key"] == key)
        at = datetime.fromisoformat(target.draft.json()["lastMeaningfulActivityAt"]) + timedelta(
            days=interval, microseconds=500000
        )
        return bootstrap.configuration_http_runtime().owners.resolve(namespace).archive(now=at)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(apply, state.a, target, key="first-concurrent")
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
            accepted = first.result(timeout=10)
            result = other.result(timeout=10)
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    latest = state.c.client.get(target.path)
    assert latest.json()["ownerId"] == state.a.id and latest.json()["status"] == "DRAFT"
    assert latest.json()["revision"] == accepted.json()["revision"]
    assert latest.json()["baseVersion"] == 2 and len(records(state, namespace)) == 1
    assert latest.json()["stale"] is (contender == "publish")
    if contender == "publish":
        assert result.json()["version"] == 3


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_publication_first_forces_apply_to_reread_current_after_active_lock_wait(
    actors: Any,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    values = (
        {"identity.session_cap": 3} if namespace == "identity" else {"draft_archive_after_days": 5}
    )
    pending_publication = saved_draft(state, namespace, values, actor=state.b)
    owner_class: Any = (
        SqlAlchemyIdentityPolicyOwnerRepository
        if namespace == "identity"
        else SqlAlchemyGatePolicyRepository
    )
    name = "publish_version" if namespace == "identity" else "publish"
    persist = getattr(owner_class, name)
    held, release = Event(), Event()
    publishing_pids = []

    def hold_before_commit(owner: Any, *args: Any, **kwargs: Any) -> Any:
        result = persist(owner, *args, **kwargs)
        assert result is not None
        publishing_pids.append(owner.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        held.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(owner_class, name, hold_before_commit)
    engine = state.journey.database.engines[
        "identity" if namespace == "identity" else "requirement"
    ]
    started, pids, observe = observe_owner_wait(engine, namespace, held)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            publishing = pool.submit(publish, state, state.b, *pending_publication)
            try:
                assert held.wait(5)
                rebasing = pool.submit(apply, state.a, target, key="stale-observation", status=409)
                assert started.wait(5)
                assert_postgres_blocked_by(
                    cast(IsolatedAgentDatabase, state.journey.database),
                    waiting_pid=pids[0],
                    blocking_pid=publishing_pids[0],
                )
            finally:
                release.set()
            assert publishing.result(timeout=10).json()["version"] == 3
            assert rebasing.result(timeout=10).status_code == 409
    finally:
        sql_event.remove(engine, "before_cursor_execute", observe)
    latest = state.a.client.get(target.path).json()
    assert latest["baseVersion"] == 1 and latest["revision"] == target.draft.json()["revision"]
    assert records(state, namespace) == []


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "fault,status", [("authorization", 503), ("history", 500), ("audit", 500), ("receipt", 500)]
)
def test_real_rebase_fault_rolls_back_target_history_audit_and_sealed_result(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, fault: str, status: int
) -> None:
    state = actors
    target = setup_stale(state, namespace)
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
            elif fault in {"history", "receipt"}:
                name = "record_rebase" if fault == "history" else "complete_idempotency"
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
        authorizer.check.side_effect = RuntimeError("synthetic authorization outage")
    before = all_facts(state)
    with injected_client(
        state.a, replace(runtime, owners=owners, draft_authorization=authorizer)
    ) as client:
        _write(
            client,
            target.path + "/rebase",
            target.body,
            etag=target.draft.headers["etag"],
            status=status,
        )
    assert connections and all(db.closed for db in connections)
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_custom_cross_field_or_archive_cutoff_failure_cannot_persist_rebase(
    actors: Any, namespace: str
) -> None:
    state = actors
    if namespace == "identity":
        values = comparison_state(namespace)
        target_values, current_values = (
            deepcopy(values.draft.content),
            deepcopy(values.current.values),
        )
        target_values["identity.login_backoff"]["maximumDelaySeconds"] = 1200
        current_values["identity.login_backoff"]["initialDelaySeconds"] = 60
        path, _draft = saved_draft(state, namespace, target_values)
        publishing_path, publishing_draft = saved_draft(
            state, namespace, current_values, actor=state.b
        )
        publish(state, state.b, publishing_path, publishing_draft)
        draft = state.a.client.get(path)
        comparison = read_comparison(state.a, path, draft)
        body = body_for(comparison)
        body["resolutions"]["identity.login_backoff"] = {
            "choice": "CUSTOM",
            "value": {
                "failureThreshold": 5,
                "initialDelaySeconds": 901,
                "maximumDelaySeconds": 900,
                "resetAfterHours": 24,
            },
        }
        target = SimpleNamespace(path=path, draft=draft, body=body)
    else:
        target = setup_stale(state, namespace)
        target.body = body_for(target.comparison, "CUSTOM", MAX_ARCHIVE_DAYS)
    before = all_facts(state)
    apply(state.a, target, status=422)
    assert state.b.client.get(target.path).json() == target.draft.json()
    after = all_facts(state)
    mutable = (
        "identity.configuration_idempotency_record"
        if namespace == "identity"
        else "requirement.gate_policy_idempotency"
    )
    assert {key: value for key, value in after.items() if key != mutable} == {
        key: value for key, value in before.items() if key != mutable
    }
    assert records(state, namespace) == []
