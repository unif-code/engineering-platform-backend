from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.configuration import PolicyRuntimeRegistry
from control_plane.app.modules.configuration.application.drafts import _content_hash
from control_plane.app.modules.identity.adapters.configuration_policy import (
    SqlAlchemyIdentityPolicyOwnerRepository,
)
from control_plane.app.modules.requirement.adapters.gate_policy import (
    SqlAlchemyGatePolicyRepository,
)
from control_plane.app.modules.requirement.domain.gate_policy import (
    ACCEPTANCE_KEY,
    ARCHIVE_KEY,
    FORMAL_KEY,
)
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


def saved_draft(
    state: Any, namespace: str, values: dict[str, Any], *, actor: Any = None
) -> tuple[str, Any]:
    actor = state.a if actor is None else actor
    created = _write(
        actor.client, f"/api/v1/admin/policies/{namespace}/drafts", {"values": values}, status=201
    )
    path = f"/api/v1/admin/policies/{namespace}/drafts/{created.json()['id']}"
    validated = _write(actor.client, path + "/validate", {}, etag=created.headers["etag"])
    assert validated.json()["valid"] is True
    preview = actor.client.get(path + "/preview", headers={"If-Match": validated.headers["etag"]})
    assert preview.status_code == 200, preview.text
    result = actor.client.get(path)
    assert result.status_code == 200, result.text
    return path, result


def publish(state: Any, actor: Any, path: str, draft: Any) -> Any:
    return _write(
        actor.client,
        path + "/publish",
        {"reason": "Observe an actual new Active version", "totpCode": otp(state, actor.secret)},
        etag=draft.headers["etag"],
        status=201,
    )


def read_comparison(actor: Any, path: str, draft: Any) -> Any:
    response = actor.client.get(
        path + "/base-comparison", headers={"If-Match": draft.headers["etag"]}
    )
    assert response.status_code == 200, response.text
    assert response.headers["etag"] == draft.headers["etag"]
    assert response.headers["cache-control"] == "no-store"
    return response


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_owner_versions_stale_archived_and_disabled_owner_are_observed_without_policy_writes(
    actors: Any,
    namespace: str,
) -> None:
    state = actors
    target_values = (
        {
            "identity.password_max_age": 90,
            "identity.session_cap": 5,
            "identity.session_idle_timeout": 45,
        }
        if namespace == "identity"
        else {ACCEPTANCE_KEY: ["code.change"], FORMAL_KEY: ["code.change"], ARCHIVE_KEY: 10}
    )
    current_values = (
        {
            "identity.temp_credential_ttl": 48,
            "identity.password_max_age": 90,
            "identity.session_cap": 4,
        }
        if namespace == "identity"
        else {ACCEPTANCE_KEY: ["code.change"], ARCHIVE_KEY: 20}
    )
    path, target = saved_draft(state, namespace, target_values)
    before = policy_facts(state)
    initial = read_comparison(state.a, path, target).json()
    assert read_comparison(state.b, path, target).json() == initial
    assert initial["baseVersion"] == initial["currentVersion"] == 1
    assert initial["baseSnapshotHash"] == initial["currentSnapshotHash"]
    assert policy_facts(state) == before

    publishing_path, publishing_draft = saved_draft(state, namespace, current_values, actor=state.b)
    assert publish(state, state.b, publishing_path, publishing_draft).json()["version"] == 2
    stale = state.b.client.get(path)
    assert stale.status_code == 200 and stale.json()["stale"] is True
    base = state.b.client.get(f"/api/v1/admin/policies/{namespace}/versions/1").json()
    current = state.b.client.get(f"/api/v1/admin/policies/{namespace}/active").json()
    before = policy_facts(state)
    observed = read_comparison(state.b, path, stale).json()
    assert observed["ownerId"] == state.a.id
    assert observed["draftRevision"] == stale.json()["revision"]
    assert observed["baseVersion"] == 1 and observed["currentVersion"] == 2
    assert observed["baseSnapshotHash"] == base["snapshotHash"]
    assert observed["currentSnapshotHash"] == current["snapshotHash"]
    assert observed["draftContentHash"] == stale.json()["contentHash"]
    assert {item["key"]: item["baseValue"] for item in observed["items"]} == base["values"]
    assert {item["key"]: item["currentValue"] for item in observed["items"]} == current["values"]
    assert {item["key"]: item["draftValue"] for item in observed["items"]} == stale.json()[
        "content"
    ]
    changes = {item["key"]: item["change"] for item in observed["items"]}
    if namespace == "identity":
        assert set(changes.values()) == {
            "UNCHANGED",
            "CURRENT_ONLY",
            "DRAFT_ONLY",
            "SAME_CHANGE",
            "CONFLICT",
        }
    else:
        assert changes == {
            ACCEPTANCE_KEY: "SAME_CHANGE",
            FORMAL_KEY: "DRAFT_ONLY",
            ARCHIVE_KEY: "CONFLICT",
        }
    assert policy_facts(state) == before

    next_values = (
        {"identity.temp_credential_ttl": 72} if namespace == "identity" else {ARCHIVE_KEY: 15}
    )
    next_path, next_draft = saved_draft(state, namespace, next_values, actor=state.b)
    assert publish(state, state.b, next_path, next_draft).json()["version"] == 3
    before = policy_facts(state)
    later = read_comparison(state.b, path, state.b.client.get(path)).json()
    assert (
        later["currentVersion"] == 3 and later["baseSnapshotHash"] == observed["baseSnapshotHash"]
    )
    assert later["currentSnapshotHash"] != observed["currentSnapshotHash"]
    assert observed["currentVersion"] == 2
    assert policy_facts(state) == before

    _write(
        state.b.client,
        f"/api/v1/admin/accounts/{state.a.id}/disable",
        {"reason": "Original policy owner departed"},
        etag=account_etag(state.b.client, state.a.id),
        status=204,
    )
    before = policy_facts(state)
    disabled_owner = read_comparison(state.b, path, state.b.client.get(path)).json()
    assert disabled_owner["status"] == "DRAFT" and disabled_owner["ownerId"] == state.a.id
    assert policy_facts(state) == before
    runtime = bootstrap.configuration_http_runtime().owners.resolve(namespace)
    assert runtime.archive(now=state.clock.value + timedelta(days=31)) >= 1
    archived = state.b.client.get(path)
    assert archived.json()["status"] == "ARCHIVED"
    before = policy_facts(state)
    result = read_comparison(state.b, path, archived).json()
    assert result["status"] == "ARCHIVED" and result["ownerId"] == state.a.id
    assert result["baseVersion"] == 1 and result["currentVersion"] == 3
    assert policy_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_default_session_reserved_permission_and_takeover_revision_are_enforced_before_reading(
    actors: Any,
    namespace: str,
) -> None:
    state = actors
    path, original = saved_draft(state, namespace, {})
    changed = takeover(state, state.b, path, original.headers["etag"])
    before = policy_facts(state)
    missing = state.a.client.get(
        f"/api/v1/admin/policies/{namespace}/drafts/{uuid4()}/base-comparison",
        headers={"If-Match": '"v1"'},
    )
    assert missing.status_code == 404, missing.text
    stale = state.a.client.get(
        path + "/base-comparison", headers={"If-Match": original.headers["etag"]}
    )
    assert stale.status_code == 409, stale.text
    read_comparison(state.a, path, changed)
    assert policy_facts(state) == before

    runtime = bootstrap.configuration_http_runtime()
    owners = Mock(spec=PolicyRuntimeRegistry)
    owners.resolve.side_effect = AssertionError("unauthorized comparison must not access owner")
    trap = replace(runtime, owners=owners)
    for scope in (None, state.journey.workspace_id):
        _grant(state.a.client, state.journey.member_id, "platform.configuration.manage", scope)
    with injected_client(SimpleNamespace(client=state.journey.member), trap) as client:
        assert (
            client.get(
                path + "/base-comparison", headers={"If-Match": changed.headers["etag"]}
            ).status_code
            == 403
        )
        client.cookies.clear()
        assert (
            client.get(
                path + "/base-comparison", headers={"If-Match": changed.headers["etag"]}
            ).status_code
            == 401
        )
    owners.resolve.assert_not_called()

    _write(
        state.c.client,
        f"/api/v1/admin/super-admins/{state.b.id}",
        {"reason": "Comparison qualification removed", "totpCode": otp(state, state.c.secret)},
        etag=account_etag(state.c.client, state.b.id),
        method="DELETE",
    )
    state.b.client.cookies.clear()
    challenge = _write(
        state.b.client,
        "/api/v1/auth/login",
        {"employeeNo": state.b.employee_no, "password": state.b.password},
    )
    assert challenge.json()["state"] == "TOTP_REQUIRED"
    _write(
        state.b.client,
        "/api/v1/auth/totp",
        {"challengeToken": challenge.json()["challengeToken"], "code": otp(state, state.b.secret)},
    )
    assert state.b.client.get("/api/v1/me").json()["isSuperAdmin"] is False
    with injected_client(state.b, trap) as client:
        assert (
            client.get(
                path + "/base-comparison", headers={"If-Match": changed.headers["etag"]}
            ).status_code
            == 403
        )
    owners.resolve.assert_not_called()


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "fault,status",
    [("hash", 503), ("schema", 503), ("candidate", 422), ("read", 503), ("base", 503)],
)
def test_controlled_owner_read_faults_release_real_connection_and_leave_every_policy_fact_unchanged(
    actors: Any,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    fault: str,
    status: int,
) -> None:
    state = actors
    path, original = saved_draft(state, namespace, {})
    runtime = bootstrap.configuration_http_runtime()
    delegate = runtime.owners.resolve(namespace)
    connections = []

    @contextmanager
    def transaction() -> Iterator[Any]:
        with delegate.transaction() as lifecycle:
            connections.append(lifecycle.db)
            if fault in {"hash", "schema", "candidate"}:
                original_read = lifecycle.owner.draft

                def damaged_read(*args: Any, **kwargs: Any) -> Any:
                    draft = original_read(*args, **kwargs)
                    assert draft is not None
                    if fault == "hash":
                        return draft.model_copy(update={"content_hash": "0" * 64})
                    if fault == "schema":
                        return draft.model_copy(update={"schema_revision": 99})
                    values = {
                        **draft.content,
                        "identity.session_cap" if namespace == "identity" else ARCHIVE_KEY: True,
                    }
                    return draft.model_copy(
                        update={"content": values, "content_hash": _content_hash(values)}
                    )

                monkeypatch.setattr(lifecycle.owner, "draft", damaged_read)
            elif fault == "base":
                monkeypatch.setattr(lifecycle.owner, "version_snapshot", lambda *_args: None)
            else:
                monkeypatch.setattr(
                    lifecycle.owner,
                    "active_snapshot",
                    Mock(side_effect=RuntimeError("private-read-fault-sentinel")),
                )
            yield lifecycle

    wrapped = Mock(wraps=delegate)
    wrapped.transaction.side_effect = transaction
    owners = replace(
        runtime.owners,
        **({"identity": wrapped} if namespace == "identity" else {"requirement_gate": wrapped}),
    )
    before = policy_facts(state)
    with injected_client(state.b, replace(runtime, owners=owners)) as client:
        response = client.get(
            path + "/base-comparison", headers={"If-Match": original.headers["etag"]}
        )
    assert response.status_code == status, response.text
    assert "private-read-fault-sentinel" not in response.text
    assert connections and all(db.closed for db in connections)
    assert policy_facts(state) == before


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_comparison_returns_while_real_publication_holds_active_and_draft_locks(
    actors: Any,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
) -> None:
    state = actors
    path, target = saved_draft(state, namespace, {})
    values = {"identity.session_cap": 4} if namespace == "identity" else {ARCHIVE_KEY: 7}
    publishing_path, publishing_draft = saved_draft(state, namespace, values, actor=state.b)
    owner_class: Any = (
        SqlAlchemyIdentityPolicyOwnerRepository
        if namespace == "identity"
        else SqlAlchemyGatePolicyRepository
    )
    name = "publish_version" if namespace == "identity" else "publish"
    persist = getattr(owner_class, name)
    held, release = Event(), Event()

    def hold_before_commit(owner: Any, *args: Any, **kwargs: Any) -> Any:
        result = persist(owner, *args, **kwargs)
        assert result is not None
        held.set()
        assert release.wait(10), "test did not release actual publication transaction"
        return result

    monkeypatch.setattr(owner_class, name, hold_before_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(publish, state, state.b, publishing_path, publishing_draft)
        try:
            assert held.wait(5), "publication did not reach actual owner persistence"
            before = policy_facts(state)
            reading = pool.submit(read_comparison, state.c, path, target)
            observed = reading.result(timeout=5).json()
            assert observed["currentVersion"] == observed["baseVersion"] == 1
            assert policy_facts(state) == before
        finally:
            release.set()
        assert publication.result(timeout=10).json()["version"] == 2
    later = read_comparison(state.c, path, state.c.client.get(path)).json()
    assert later["currentVersion"] == 2 and observed["currentVersion"] == 1
