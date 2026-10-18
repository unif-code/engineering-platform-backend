import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.configuration import PolicyLifecycle, PolicySnapshotUnavailable
from tests.configuration.test_draft_archive_timing_e2e import configured_reader
from tests.configuration.test_draft_clone_e2e import all_facts, clone, copied_path, tables
from tests.configuration.test_draft_manual_archive_e2e import archive
from tests.configuration.test_draft_rebase_e2e import apply, setup_stale
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import injected_client, otp, takeover
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _write

pytestmark = pytest.mark.integration


def table(namespace: str) -> str:
    return (
        "identity.draft_revision" if namespace == "identity" else "requirement.gate_policy_revision"
    )


def facts(state: Any) -> Any:
    result = all_facts(state)
    with state.journey.database.owner.connect() as db:
        for namespace in ("identity", "requirement.gate"):
            result[table(namespace)] = list(
                db.execute(
                    text(f"SELECT to_jsonb(t) FROM {table(namespace)} t ORDER BY draft_id,revision")
                ).scalars()
            )
    return result


def query(actor: Any, path: str, target: Any, *, status: int = 200, **params: Any) -> Any:
    response = actor.client.get(
        path + "/revisions", headers={"If-Match": target.headers["etag"]}, params=params
    )
    assert response.status_code == status, response.text
    if status == 200:
        assert response.headers["etag"] == target.headers["etag"]
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        for item in body["items"]:
            envelope = {
                "draftId": body["draftId"],
                "namespace": body["namespace"],
                "scope": body["scope"],
                **item,
            }
            digest = envelope.pop("revisionSnapshotHash")
            canonical = json.dumps(
                envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            assert hashlib.sha256(canonical.encode()).hexdigest() == digest
            assert (
                hashlib.sha256(item["contentJson"].encode()).hexdigest() == item["contentHash"]
            ) is item["contentHashMatchesContent"]
    return response


def create(state: Any, namespace: str, values: dict[str, Any] | None = None) -> tuple[str, Any]:
    response = _write(
        state.a.client,
        f"/api/v1/admin/policies/{namespace}/drafts",
        {"values": values or {}},
        status=201,
    )
    return f"/api/v1/admin/policies/{namespace}/drafts/{response.json()['id']}", response


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_capture_update_validate_takeover_paging_and_same_revision_archive(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, target = create(state, namespace)
    assert query(state.c, path, target).json()["items"][0]["operation"] == "CREATE"
    target = _write(
        state.a.client, path, {"values": {}}, method="PATCH", etag=target.headers["etag"]
    )
    target = _write(state.a.client, path + "/validate", {}, etag=target.headers["etag"])
    target = takeover(
        state, state.b, path, target.headers["etag"], reason="  actual original reason  "
    )
    before = facts(state)
    with (
        configured_reader(state, namespace, lambda lifecycle: lifecycle) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        page = query(SimpleNamespace(client=client), path, target, limit=2).json()
        older = query(
            SimpleNamespace(client=client), path, target, limit=2, cursor=page["nextCursor"]
        ).json()
    assert facts(state) == before
    assert page["coverage"] == older["coverage"] == "COMPLETE"
    assert page["recordedCount"] == older["recordedCount"] == 4
    assert [r["operation"] for r in page["items"] + older["items"]] == [
        "TAKEOVER",
        "VALIDATE",
        "UPDATE",
        "CREATE",
    ]
    assert page["items"][0]["takeover"] == {
        "previousOwnerId": state.a.id,
        "reason": "  actual original reason  ",
    }
    assert page["items"][1]["validation"] == {"valid": True, "issues": []}
    archive(state.b, path, target)
    assert query(state.c, path, target, limit=2).json() == page


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_rebase_clone_and_both_rollback_paths_bind_original_records(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = setup_stale(state, namespace)
    changed = apply(state.a, target)
    item = query(state.c, target.path, changed).json()["items"][0]
    assert item["operation"] == "REBASE" and item["baseVersion"] == 2 and item["governanceRecordId"]
    receipt = clone(state.b, target.path, changed)
    new_path = copied_path(namespace, receipt)
    copy = state.b.client.get(new_path)
    copied = query(state.c, new_path, copy).json()
    assert copied["recordedCount"] == 1 and copied["items"][0]["operation"] == "CLONE"
    assert copied["items"][0]["governanceRecordId"]
    rollback = _write(
        state.a.client,
        f"/api/v1/admin/policies/{namespace}/rollback",
        {
            "scope": "PLATFORM",
            "toVersion": 1,
            "reason": "Captured rollback",
            "totpCode": otp(state, state.a.secret),
        },
        etag='"v2"',
        status=201,
    )
    path = f"/api/v1/admin/policies/{namespace}/drafts/{rollback.json()['id']}"
    historical = query(state.c, path, rollback).json()["items"][0]
    assert historical["operation"] == "ROLLBACK" and historical["rollbackFromVersion"] == 1
    assert historical["contentHash"] == rollback.json()["contentHash"]
    if namespace == "requirement.gate":
        with state.journey.database.owner.connect() as db:
            binding = db.execute(
                text("SELECT binding FROM requirement.gate_policy_receipt WHERE draft_id=:id"),
                {"id": rollback.json()["id"]},
            ).scalar_one()
        assert binding["content_hash"] == historical["contentHash"]


@pytest.mark.parametrize("value", [1e20, 1e21, -0.0])
def test_old_mismatched_hash_is_preserved_by_validate_takeover_until_content_write(
    actors: Any, value: float
) -> None:
    state = actors
    path, created = create(state, "identity", {"identity.session_cap": value})
    original = {**created.json()["content"], "identity.session_cap": value}
    old_hash = hashlib.sha256(
        json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert old_hash != created.json()["contentHash"]
    with state.journey.database.owner.begin() as db:
        db.execute(
            text("DELETE FROM identity.draft_revision WHERE draft_id=:id"),
            {"id": created.json()["id"]},
        )
        db.execute(
            text("UPDATE identity.draft SET content_hash=:hash WHERE id=:id"),
            {"id": created.json()["id"], "hash": old_hash},
        )
    assert query(state.c, path, created).json()["coverage"] == "NONE"
    validated = _write(state.a.client, path + "/validate", {}, etag=created.headers["etag"])
    assert validated.json()["valid"] is False
    taken = takeover(
        state, state.b, path, validated.headers["etag"], reason="legacy hash remains a fact"
    )
    before = query(state.c, path, taken).json()
    assert before["coverage"] == "PARTIAL" and before["recordedCount"] == 2
    assert all(
        r["contentHash"] == old_hash and r["contentHashMatchesContent"] is False
        for r in before["items"]
    )
    updated = _write(
        state.b.client, path, {"values": {}}, method="PATCH", etag=taken.headers["etag"]
    )
    after = query(state.c, path, updated).json()
    assert after["items"][0]["contentHashMatchesContent"] is True
    assert after["items"][1:] == before["items"] and after["coverage"] == "PARTIAL"


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("fault", ["capture-before", "capture-after", "audit", "seal"])
def test_capture_failures_escape_business_problem_handlers_and_rollback_owner(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, fault: str
) -> None:
    state = actors
    path, target = create(state, namespace)
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []

    @contextmanager
    def transaction() -> Any:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            if fault == "audit":
                audit = Mock()
                audit.append_in_transaction.side_effect = RuntimeError("synthetic audit failure")
                lifecycle = PolicyLifecycle(
                    lifecycle.db, lifecycle.owner, replace(runtime.dependencies, audit=audit)
                )
            else:
                name = "complete_idempotency" if fault == "seal" else "record_revision"
                persist = getattr(lifecycle.owner, name)

                def fail(*args: Any, **kwargs: Any) -> Any:
                    if fault != "capture-before":
                        persist(*args, **kwargs)
                    if fault.startswith("capture"):
                        raise PolicySnapshotUnavailable("synthetic capture failure")
                    raise RuntimeError("synthetic seal failure")

                monkeypatch.setattr(lifecycle.owner, name, fail)
            yield lifecycle

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    before = facts(state)
    with injected_client(state.a, replace(runtime, owners=owners)) as client:
        _write(
            client,
            path,
            {"values": {}},
            method="PATCH",
            etag=target.headers["etag"],
            status=503 if fault.startswith("capture") else 500,
        )
    assert facts(state) == before
    assert connections and all(db.closed for db in connections)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_history_runtime_role_is_append_only_and_plain_reads_do_not_wait_on_draft_lock(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, target = create(state, namespace)
    reader = bootstrap.configuration_http_runtime().owners.resolve(namespace)
    with reader.transaction() as lifecycle:
        assert (
            lifecycle.db.execute(text(f"SELECT count(*) FROM {table(namespace)}")).scalar_one() == 1
        )
        for command in [
            f"UPDATE {table(namespace)} SET revision=revision",
            f"DELETE FROM {table(namespace)}",
        ]:
            with pytest.raises(ProgrammingError), lifecycle.db.begin_nested():
                lifecycle.db.execute(text(command))
    # Release the blocker before joining a reader that timed out.
    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        state.journey.database.owner.begin() as lock,
    ):
        lock.execute(
            text(f"SELECT id FROM {tables(namespace)[0]} WHERE id=:id FOR UPDATE"),
            {"id": target.json()["id"]},
        )
        result = pool.submit(query, state.c, path, target).result(timeout=5)
        assert result.json()["coverage"] == "COMPLETE"


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_same_key_replay_captures_exactly_one_revision(actors: Any, namespace: str) -> None:
    state = actors
    path, target = create(state, namespace)
    first = _write(
        state.a.client,
        path,
        {"values": {}},
        method="PATCH",
        etag=target.headers["etag"],
        key="revision-noop",
    )
    before = facts(state)
    replay = _write(
        state.a.client,
        path,
        {"values": {}},
        method="PATCH",
        etag=target.headers["etag"],
        key="revision-noop",
    )
    assert replay.json() == first.json() and facts(state) == before
    assert query(state.c, path, first).json()["recordedCount"] == 2


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "column,value",
    [
        ("content_json", "{}"),
        ("content_hash_matches_content", False),
        ("revision_snapshot_hash", "0" * 64),
    ],
)
def test_corrupt_stored_history_is_a_read_failure_without_mutating_facts(
    actors: Any, namespace: str, column: str, value: Any
) -> None:
    state = actors
    path, target = create(state, namespace)
    with state.journey.database.owner.begin() as db:
        db.execute(
            text(f"UPDATE {table(namespace)} SET {column}=:value WHERE draft_id=:id"),
            {"id": target.json()["id"], "value": value},
        )
    before = facts(state)
    query(state.c, path, target, status=503)
    assert facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("change", ["revision", "archive"])
def test_read_race_uses_revision_only_and_does_not_lock_the_target(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, change: str
) -> None:
    state = actors
    path, target = create(state, namespace)

    def configure(lifecycle: Any) -> Any:
        original = lifecycle.owner.draft_revision_rows

        def rows(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            if change == "revision":
                _write(
                    state.a.client,
                    path,
                    {"values": {}},
                    method="PATCH",
                    etag=target.headers["etag"],
                )
            else:
                archive(state.a, path, target)
            return value

        monkeypatch.setattr(lifecycle.owner, "draft_revision_rows", rows)
        return lifecycle

    with (
        configured_reader(state, namespace, configure) as runtime,
        injected_client(state.c, runtime) as client,
    ):
        response = query(
            SimpleNamespace(client=client),
            path,
            target,
            status=409 if change == "revision" else 200,
        )
    if change == "archive":
        assert response.json()["recordedCount"] == 1


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_session_and_reserved_authority_deny_before_owner_reads(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, target = create(state, namespace)
    runtime = bootstrap.configuration_http_runtime()
    owners = Mock()
    owners.resolve.side_effect = AssertionError("unauthorized history must not access owner")
    with injected_client(
        SimpleNamespace(client=state.journey.member), replace(runtime, owners=owners)
    ) as client:
        query(SimpleNamespace(client=client), path, target, status=403)
        client.cookies.clear()
        query(SimpleNamespace(client=client), path, target, status=401)
    owners.resolve.assert_not_called()


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_rollback_capture_failure_rolls_back_owner_but_keeps_independent_identity_consumption(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str
) -> None:
    from control_plane.app.modules.identity.adapters.configuration_policy import (
        SqlAlchemyIdentityPolicyOwnerRepository,
    )
    from control_plane.app.modules.requirement.adapters.gate_policy import (
        SqlAlchemyGatePolicyRepository,
    )

    state = actors
    repository = (
        SqlAlchemyIdentityPolicyOwnerRepository
        if namespace == "identity"
        else SqlAlchemyGatePolicyRepository
    )
    persist = repository.record_revision

    def fail(self: Any, **values: Any) -> None:
        persist(self, **values)
        raise PolicySnapshotUnavailable("synthetic rollback capture failure")

    monkeypatch.setattr(repository, "record_revision", fail)
    before = facts(state)
    code = otp(state, state.a.secret)
    with injected_client(state.a, bootstrap.configuration_http_runtime()) as client:
        _write(
            client,
            f"/api/v1/admin/policies/{namespace}/rollback",
            {"scope": "PLATFORM", "toVersion": 1, "reason": "Capture failure", "totpCode": code},
            etag='"v1"',
            status=503,
        )
    after = facts(state)
    if namespace == "requirement.gate":
        assert (
            len(after["identity.policy_reauth_consumption"])
            == len(before["identity.policy_reauth_consumption"]) + 1
        )
        after.pop("identity.policy_reauth_consumption")
        before.pop("identity.policy_reauth_consumption")
        after.pop("totp")
        before.pop("totp")
    assert after == before
