from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.configuration import PolicyRuntimeRegistry
from control_plane.app.modules.configuration.adapters.identity import IdentityPolicyOwner
from control_plane.app.modules.requirement.adapters.gate_policy import (
    SqlAlchemyGatePolicyRepository,
)
from tests.configuration.test_draft_base_comparison_e2e import publish, read_comparison, saved_draft
from tests.configuration.test_draft_clone_e2e import all_facts, clone, copied_path
from tests.configuration.test_draft_rebase_e2e import apply, body_for
from tests.configuration.test_draft_takeover_e2e import account_etag, injected_client, otp, takeover
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _grant, _write

pytestmark = pytest.mark.integration


def query(actor: Any, path: str, draft: Any, *, status: int = 200, **params: Any) -> Any:
    response = actor.client.get(
        path + "/governance-records", headers={"If-Match": draft.headers["etag"]}, params=params
    )
    assert response.status_code == status, response.text
    if status == 200:
        assert response.headers["etag"] == draft.headers["etag"]
        assert response.headers["cache-control"] == "no-store"
    return response


def history(state: Any, namespace: str, count: int = 2) -> Any:
    source_values = (
        {
            "identity.password_max_age": 42,
            "identity.session_cap": 5,
            "identity.login_backoff": {
                "failureThreshold": 6,
                "initialDelaySeconds": 45,
                "maximumDelaySeconds": 1200,
                "resetAfterHours": 48,
            },
        }
        if namespace == "identity"
        else {
            "acceptance.additional_required_capabilities": ["code.change"],
            "formal_review.additional_required_capabilities": ["code.change"],
            "draft_archive_after_days": 10,
        }
    )
    source_path, source = saved_draft(state, namespace, source_values)
    copied = clone(state.a, source_path, source)
    path = copied_path(namespace, copied)
    for index in range(count):
        values = (
            {
                "identity.password_max_age": 180 if index == 0 else "NEVER",
                "identity.session_cap": 4,
                "identity.login_backoff": {
                    "failureThreshold": 5,
                    "initialDelaySeconds": 60 + index,
                    "maximumDelaySeconds": 900,
                    "resetAfterHours": 24,
                },
            }
            if namespace == "identity"
            else {
                "acceptance.additional_required_capabilities": [],
                "formal_review.additional_required_capabilities": [],
                "draft_archive_after_days": 20 + index,
            }
        )
        publication = saved_draft(state, namespace, values, actor=state.b)
        publish(state, state.b, *publication)
        draft = state.a.client.get(path)
        observation = read_comparison(state.a, path, draft)
        body = body_for(observation, "DRAFT")
        if namespace == "identity":
            for key, value in [
                ("identity.password_max_age", 42),
                ("identity.login_backoff", source_values["identity.login_backoff"]),
            ]:
                if key in body["resolutions"]:
                    body["resolutions"][key] = {"choice": "CUSTOM", "value": deepcopy(value)}
        target = SimpleNamespace(path=path, draft=draft, body=body)
        apply(state.a, target)
    return SimpleNamespace(
        path=path, draft=state.a.client.get(path), source=source, source_path=source_path
    )


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_empty_clone_chain_and_archived_other_disabled_owner_do_not_reconstruct_history(
    actors: Any, namespace: str
) -> None:
    state = actors
    path, source = saved_draft(state, namespace, {})
    before = all_facts(state)
    empty = query(state.b, path, source).json()
    assert empty["cloneRecord"] is None and empty["rebases"] == [] and empty["nextCursor"] is None
    assert all_facts(state) == before
    first = clone(state.b, path, source)
    first_path = copied_path(namespace, first)
    first_draft = state.b.client.get(first_path)
    second = clone(state.c, first_path, first_draft)
    second_path = copied_path(namespace, second)
    original = query(state.c, second_path, state.c.client.get(second_path)).json()["cloneRecord"]
    assert original["source"]["draftId"] == first.json()["draft"]["id"]
    changed = _write(
        state.b.client,
        first_path,
        {
            "values": {
                "identity.session_cap" if namespace == "identity" else "draft_archive_after_days": 6
            }
        },
        etag=first_draft.headers["etag"],
        method="PATCH",
    )
    assert changed.status_code == 200
    current = state.c.client.get(second_path)
    takeover(state, state.b, second_path, current.headers["etag"])
    _write(
        state.c.client,
        f"/api/v1/admin/accounts/{state.b.id}/disable",
        {"reason": "Historical owner departed"},
        etag=account_etag(state.c.client, state.b.id),
        status=204,
    )
    bootstrap.configuration_http_runtime().owners.resolve(namespace).archive(
        now=state.clock.value + timedelta(days=31)
    )
    archived = state.c.client.get(second_path)
    assert archived.json()["status"] == "ARCHIVED"
    before = all_facts(state)
    page = query(state.c, second_path, archived).json()
    assert page["cloneRecord"] == original and page["rebases"] == []
    assert page["cloneRecord"]["sourceContent"] == first_draft.json()["content"]
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_actual_multi_rebase_pages_keep_fixed_references_choices_and_revision_upper_bound(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = history(state, namespace, 3)
    before = all_facts(state)
    page = query(state.b, target.path, target.draft, limit=1).json()
    full = query(state.c, target.path, target.draft, limit=100).json()
    assert len(full["rebases"]) == 3 and full["nextCursor"] is None
    assert [r["afterRevision"] for r in full["rebases"]] == [4, 3, 2]
    assert page["rebases"] == full["rebases"][:1] and page["nextCursor"] == "4"
    revisions = [4]
    while page["nextCursor"]:
        page = query(state.c, target.path, target.draft, limit=1, cursor=page["nextCursor"]).json()
        assert page["cloneRecord"] == full["cloneRecord"]
        revisions.extend(record["afterRevision"] for record in page["rebases"])
    assert revisions == [4, 3, 2]
    assert query(state.c, target.path, target.draft, cursor="2").json()["rebases"] == []
    for record in full["rebases"]:
        base = state.c.client.get(
            f"/api/v1/admin/policies/{namespace}/versions/{record['baseSnapshot']['version']}"
        ).json()
        assert base == record["baseSnapshot"]
        assert record["afterRevision"] == record["beforeRevision"] + 1
        if namespace == "identity":
            assert record["afterContent"]["identity.password_max_age"] == 42
            assert len(record["afterContent"]["identity.login_backoff"]) == 4
        for selection in record["selections"].values():
            assert (selection["resolution"] is None) == (selection["change"] != "CONFLICT")
    invalid_pages: list[dict[str, Any]] = [
        {"cursor": "0"},
        {"cursor": "01"},
        {"cursor": " 2"},
        {"cursor": "５"},
        {"cursor": "5"},
        {"limit": 101},
    ]
    for params in invalid_pages:
        query(state.c, target.path, target.draft, status=422, **params)
    assert all_facts(state) == before
    latest = _write(
        state.a.client,
        target.path,
        {"values": {}},
        etag=target.draft.headers["etag"],
        method="PATCH",
    )
    before = all_facts(state)
    query(state.c, target.path, target.draft, status=409, cursor="4")
    renewed = query(state.c, target.path, latest).json()
    assert renewed["rebases"] == full["rebases"] and renewed["cloneRecord"] == full["cloneRecord"]
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_read_is_not_blocked_by_actual_rebase_active_and_draft_write_locks(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str
) -> None:
    state = actors
    target = history(state, namespace, 1)
    baseline = query(state.c, target.path, target.draft).json()
    publication = saved_draft(
        state,
        namespace,
        {"identity.session_cap" if namespace == "identity" else "draft_archive_after_days": 6},
        actor=state.b,
    )
    publish(state, state.b, *publication)
    source = state.a.client.get(target.path)
    comparison = read_comparison(state.a, target.path, source)
    command = SimpleNamespace(path=target.path, draft=source, body=body_for(comparison, "DRAFT"))
    owner_class = IdentityPolicyOwner if namespace == "identity" else SqlAlchemyGatePolicyRepository
    persist = owner_class.record_rebase
    held, release = Event(), Event()

    def hold(owner: Any, **values: Any) -> None:
        persist(owner, **values)
        held.set()
        assert release.wait(10)

    monkeypatch.setattr(owner_class, "record_rebase", hold)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(apply, state.a, command)
        try:
            assert held.wait(5)
            before = all_facts(state)
            reading = pool.submit(query, state.c, target.path, target.draft)
            observed = reading.result(timeout=5)
            assert observed.json() == baseline
            assert all_facts(state) == before
        finally:
            release.set()
        committed = writing.result(timeout=10)
    query(state.c, target.path, target.draft, status=409)
    updated = query(state.c, target.path, committed).json()
    assert [r["afterRevision"] for r in updated["rebases"]] == [3, 2]
    assert updated["rebases"][1:] == baseline["rebases"]


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_default_session_and_reserved_current_qualification_deny_before_owner_reads(
    actors: Any, namespace: str
) -> None:
    state = actors
    target = history(state, namespace, 1)
    for scope in (None, state.journey.workspace_id):
        _grant(state.a.client, state.journey.member_id, "platform.configuration.manage", scope)
    runtime = bootstrap.configuration_http_runtime()
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve.side_effect = AssertionError("no owner reads before authorization")
    with injected_client(
        SimpleNamespace(client=state.journey.member), replace(runtime, owners=owners)
    ) as client:
        assert (
            client.get(
                target.path + "/governance-records",
                headers={"If-Match": target.draft.headers["etag"]},
            ).status_code
            == 403
        )
        client.cookies.clear()
        assert (
            client.get(
                target.path + "/governance-records",
                headers={"If-Match": target.draft.headers["etag"]},
            ).status_code
            == 401
        )
    _write(
        state.c.client,
        f"/api/v1/admin/super-admins/{state.b.id}",
        {"reason": "History reader revoked", "totpCode": otp(state, state.c.secret)},
        etag=account_etag(state.c.client, state.b.id),
        method="DELETE",
    )
    with injected_client(state.b, replace(runtime, owners=owners)) as client:
        assert client.get(
            target.path + "/governance-records", headers={"If-Match": target.draft.headers["etag"]}
        ).status_code in {401, 403}
    stale_session = SimpleNamespace(client=SimpleNamespace(cookies=dict(state.a.client.cookies)))
    _write(state.a.client, "/api/v1/auth/logout", {})
    with injected_client(stale_session, replace(runtime, owners=owners)) as client:
        assert (
            client.get(
                target.path + "/governance-records",
                headers={"If-Match": target.draft.headers["etag"]},
            ).status_code
            == 401
        )
    owners.resolve.assert_not_called()


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "fault",
    ["clone-hash", "source", "rebase-hash", "selection", "missing-version", "schema", "dependency"],
)
def test_corrupt_or_unavailable_history_fails_whole_page_and_releases_without_any_write(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str, fault: str
) -> None:
    state = actors
    target = history(state, namespace, 1)
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []

    @contextmanager
    def transaction() -> Iterator[Any]:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            if fault in {"clone-hash", "source", "schema", "dependency"}:
                original = lifecycle.owner.clone_record

                def corrupted(*args: Any, **kwargs: Any) -> Any:
                    if fault == "dependency":
                        raise RuntimeError("private stored content and SQL")
                    record = deepcopy(original(*args, **kwargs))
                    assert record is not None
                    if fault == "clone-hash":
                        record["source_content_hash"] = "a" * 64
                    if fault == "source":
                        record["source_draft_id"] = record["draft_id"]
                    if fault == "schema":
                        record["schema_revision"] = 2
                    return record

                monkeypatch.setattr(lifecycle.owner, "clone_record", corrupted)
            elif fault == "missing-version":
                monkeypatch.setattr(lifecycle.owner, "version_snapshot", lambda *_: None)
            else:
                original_page = lifecycle.owner.rebase_records

                def corrupted_page(*args: Any, **kwargs: Any) -> Any:
                    page = deepcopy(original_page(*args, **kwargs))
                    if fault == "rebase-hash":
                        page[0]["after_content_hash"] = "a" * 64
                    else:
                        next(iter(page[0]["selections"].values()))["source"] = "INVALID"
                    return page

                monkeypatch.setattr(lifecycle.owner, "rebase_records", corrupted_page)
            yield lifecycle

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    before = all_facts(state)
    with injected_client(state.a, replace(runtime, owners=owners)) as client:
        response = query(SimpleNamespace(client=client), target.path, target.draft, status=503)
        assert "private" not in response.text and "cloneRecord" not in response.text
    assert connections and all(db.closed for db in connections)
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_historical_rollback_reference_is_read_after_later_source_changes(
    actors: Any, namespace: str
) -> None:
    state = actors
    history(state, namespace, 1)
    rollback = _write(
        state.a.client,
        f"/api/v1/admin/policies/{namespace}/rollback",
        {
            "toVersion": 1,
            "reason": "Recorded rollback source",
            "totpCode": otp(state, state.a.secret),
        },
        etag='"v2"',
        status=201,
    )
    path = f"/api/v1/admin/policies/{namespace}/drafts/{rollback.json()['id']}"
    source = state.a.client.get(path)
    receipt = clone(state.b, path, source)
    target = copied_path(namespace, receipt)
    before = all_facts(state)
    page = query(state.c, target, state.b.client.get(target)).json()
    assert page["cloneRecord"]["source"]["rollbackFromVersion"] == 1
    assert page["cloneRecord"]["baseSnapshot"]["version"] == 2
    assert page["cloneRecord"]["currentSnapshotAtOperation"]["version"] == 2
    assert all_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_target_changed_during_read_returns_409_and_does_not_write_history(
    actors: Any, monkeypatch: pytest.MonkeyPatch, namespace: str
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
            original = lifecycle.owner.rebase_records

            def change_after_read(*args: Any, **kwargs: Any) -> Any:
                rows = original(*args, **kwargs)
                _write(
                    state.a.client, path, {"values": {}}, etag=draft.headers["etag"], method="PATCH"
                )
                return rows

            monkeypatch.setattr(lifecycle.owner, "rebase_records", change_after_read)
            yield lifecycle

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    before = all_facts(state)
    with injected_client(state.c, replace(runtime, owners=owners)) as client:
        query(SimpleNamespace(client=client), path, draft, status=409)
    assert connections and all(db.closed for db in connections)
    current = state.c.client.get(path)
    assert current.json()["revision"] == draft.json()["revision"] + 1
    after = all_facts(state)
    for key in (
        "identity.draft_clone",
        "identity.draft_rebase",
        "requirement.gate_policy_clone",
        "requirement.gate_policy_rebase",
    ):
        assert after[key] == before[key]
    assert (
        len([row for row in after["audit"] if row["action"] == "configuration.draft.updated"])
        == len([row for row in before["audit"] if row["action"] == "configuration.draft.updated"])
        + 1
    )
