from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.authorization import (
    PLATFORM_CONFIGURATION_MANAGE,
    DecisionCode,
    Scope,
)
from control_plane.app.modules.configuration import (
    DraftAuthorizationDenied,
    PolicySnapshotUnavailable,
)
from control_plane.app.modules.configuration.adapters import draft_authorization as auth_adapter
from control_plane.app.modules.configuration.api.routes import (
    ConfigurationHttpRuntime,
    create_configuration_router,
)
from tests.configuration.test_api import _Secrets
from tests.configuration.test_draft_rebase import rebase_state
from tests.shared.test_idempotency_contract import MemoryRepository


class ScopedMemory:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], MemoryRepository] = {}

    def claim_idempotency(self, **values: Any) -> bool:
        scope = (values["actor"], values["operation"], values["idempotency_key"])
        return self.rows.setdefault(scope, MemoryRepository()).claim_idempotency(**values)

    def idempotency_by_scope(self, actor: str, operation: str, key: str, **values: Any) -> Any:
        return self.rows[(actor, operation, key)].idempotency_by_scope(
            actor, operation, key, **values
        )

    def complete_idempotency(self, record_id: str, **values: Any) -> bool:
        for repo in self.rows.values():
            if repo.row is not None and repo.row["id"] == record_id:
                return repo.complete_idempotency(record_id, **values)
        raise AssertionError("Unknown idempotent record")


@pytest.fixture(params=["identity", "requirement.gate"])
def http_rebase(request: pytest.FixtureRequest) -> Any:
    h = rebase_state(request.param)
    h.memory = ScopedMemory()
    for name in ("claim_idempotency", "idempotency_by_scope", "complete_idempotency"):
        setattr(h.owner, name, getattr(h.memory, name))
    persist = h.owner.rebase_draft.side_effect

    def update(*args: Any, **values: Any) -> Any:
        result = persist(*args, **values)
        h.owner.draft.return_value = result
        return result

    h.owner.rebase_draft.side_effect = update
    h.exits = 0

    @contextmanager
    def transaction() -> Any:
        before = deepcopy(h.memory.rows)
        try:
            yield h.lifecycle
        except Exception:
            h.memory.rows = before
            raise
        finally:
            h.exits += 1

    h.owners, h.guard = Mock(), Mock()
    h.owners.resolve.return_value = SimpleNamespace(transaction=transaction)
    h.runtime = ConfigurationHttpRuntime(h.owners, h.dependencies, _Secrets(), h.authorization)
    app = FastAPI()
    app.include_router(
        create_configuration_router(
            lambda: h.runtime, lambda: SimpleNamespace(account_id=h.draft.owner_id), h.guard
        )
    )
    h.path = f"/api/v1/admin/policies/{h.namespace}/drafts/{h.draft.id}/rebase"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        client.cookies.set("ep_session", "private-original-session")
        h.client, h.app = client, app
        yield h


def post(
    h: Any,
    *,
    body: Any = None,
    etag: str = '"v7"',
    key: str = "rebase-original-key",
    origin: str = "https://testserver",
) -> Any:
    return h.client.post(
        h.path,
        json=h.request if body is None else body,
        headers={"If-Match": etag, "Idempotency-Key": key, "Origin": origin},
    )


def test_rebase_replay_skips_locks_normalization_history_and_commit_authorization(
    http_rebase: Any,
) -> None:
    h = http_rebase
    accepted = post(h)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["baseVersion"] == 2 and accepted.json()["revision"] == 8
    assert accepted.headers["etag"] == '"v8"'
    h.authorization.check.assert_called_once_with(
        raw_session="private-original-session", actor_id=h.draft.owner_id
    )
    h.owner.draft.return_value = h.owner.draft.return_value.model_copy(
        update={"owner_id": "third-admin", "revision": 20, "status": "ARCHIVED", "base_version": 5}
    )
    h.runtime = replace(h.runtime, draft_authorization=None)
    locked = h.owner.locked_active_snapshot.call_count
    normalized = h.owner.normalize_candidate.call_count
    replay = post(h)
    assert (
        replay.status_code == 200
        and replay.content == accepted.content
        and replay.headers["etag"] == accepted.headers["etag"]
    )
    assert h.owner.locked_active_snapshot.call_count == locked
    assert h.owner.normalize_candidate.call_count == normalized
    h.owner.record_rebase.assert_called_once()
    h.owner.rebase_draft.assert_called_once()
    assert "private-original-session" not in str(h.owner.record_rebase.call_args)
    changed = deepcopy(h.request)
    changed["resolutions"][next(iter(changed["resolutions"]))] = {"choice": "DRAFT"}
    assert post(h, body=changed).status_code == 409
    assert post(h, etag='"v8"').status_code == 409
    h.guard.side_effect = HTTPException(403)
    count = h.owners.resolve.call_count
    assert post(h).status_code == 403
    assert h.owners.resolve.call_count == count


@pytest.mark.parametrize("status", [401, 403, 503])
def test_rebase_initial_authorization_and_commit_denial_are_distinct(
    http_rebase: Any, status: int
) -> None:
    h = http_rebase
    h.guard.side_effect = HTTPException(status)
    assert post(h).status_code == status
    h.owners.resolve.assert_not_called()
    h.guard.side_effect = None
    h.authorization.check.side_effect = (
        PolicySnapshotUnavailable("unavailable")
        if status == 503
        else DraftAuthorizationDenied(401 if status == 401 else 403)
    )
    response = post(h)
    assert response.status_code == status, response.text
    h.owner.rebase_draft.assert_not_called()
    h.owner.record_rebase.assert_not_called()
    assert h.exits == 1


@pytest.mark.parametrize(
    "change",
    [
        {"baseVersion": True},
        {"currentVersion": "2"},
        {"schemaRevision": 1.5},
        {"baseVersion": 0},
        {"baseSnapshotHash": "A" * 64},
        {"currentSnapshotHash": "bad"},
        {"draftContentHash": None},
        {"reason": "not-an-input"},
        {"ownerId": "chosen-owner"},
        {"totpCode": "123456"},
        {"expectedRevision": 7},
        {"resolutions": []},
        {"resolutions": {"key": {"choice": "CURRENT", "value": 1}}},
        {"resolutions": {"key": {"choice": "CUSTOM"}}},
        {"resolutions": {"key": {"choice": "UNKNOWN"}}},
    ],
)
def test_rebase_request_is_strict_and_does_not_start_owner_work(
    http_rebase: Any, change: dict[str, Any]
) -> None:
    h = http_rebase
    assert post(h, body={**h.request, **change}).status_code == 422
    h.owners.resolve.assert_not_called()


def test_rebase_preflight_and_missing_default_current_check_fail_closed(http_rebase: Any) -> None:
    h = http_rebase
    assert post(h, origin="https://cross.example").status_code == 403
    assert post(h, etag='W/"v7"').status_code == 422
    assert post(h, key="short").status_code == 422
    h.owners.resolve.assert_not_called()
    h.runtime = replace(h.runtime, draft_authorization=None)
    assert post(h).status_code == 503
    h.owner.rebase_draft.assert_not_called()
    h.owner.record_rebase.assert_not_called()


def test_rebase_openapi_adds_only_three_request_schemas_and_reuses_draft_response(
    http_rebase: Any,
) -> None:
    schema = http_rebase.app.openapi()
    operation = schema["paths"]["/api/v1/admin/policies/{namespace}/drafts/{draft_id}/rebase"][
        "post"
    ]
    assert operation["operationId"] == "draft_rebase_apply"
    params = {item["name"]: item for item in operation["parameters"]}
    assert params["If-Match"]["required"] and params["Idempotency-Key"]["required"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/DraftResponseDto"
    )
    for name in (
        "ApplyDraftRebaseRequestDto",
        "RebaseSideResolutionDto",
        "RebaseCustomResolutionDto",
    ):
        assert schema["components"]["schemas"][name]["additionalProperties"] is False
    request = schema["components"]["schemas"]["ApplyDraftRebaseRequestDto"]
    assert set(request["required"]) == {
        "baseVersion",
        "currentVersion",
        "schemaRevision",
        "baseSnapshotHash",
        "currentSnapshotHash",
        "draftContentHash",
        "resolutions",
    }
    assert (
        request["properties"]["resolutions"]["additionalProperties"]["discriminator"][
            "propertyName"
        ]
        == "choice"
    )


@pytest.mark.parametrize(
    "case",
    [
        "allow",
        "anonymous",
        "denied",
        "unavailable",
        "actor",
        "not-admin",
        "dirty",
        "fence",
        "version",
        "failure",
    ],
)
def test_current_authorization_adapter_uses_public_fresh_session_decision_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    db = Mock()

    @contextmanager
    def transaction() -> Any:
        yield db

    engine = Mock()
    engine.begin.side_effect = transaction
    dependencies, decisions = Mock(), Mock()
    before = SimpleNamespace(version=3, fence_generation=4, dirty_generation=None)
    after = SimpleNamespace(version=3, fence_generation=4, dirty_generation=None)
    principal = SimpleNamespace(account_id="actor", is_super_admin=True, authorization_version=3)
    decision = SimpleNamespace(allowed=True, code=DecisionCode.ALLOW, principal=principal)
    status = None
    if case in {"anonymous", "denied", "unavailable"}:
        decision.allowed = False
        decision.code = {
            "anonymous": DecisionCode.UNAUTHENTICATED,
            "denied": DecisionCode.DENIED,
            "unavailable": DecisionCode.UNAVAILABLE,
        }[case]
        status = {"anonymous": 401, "denied": 403, "unavailable": 503}[case]
    if case == "actor":
        principal.account_id = "other"
        status = 403
    if case == "not-admin":
        principal.is_super_admin = False
        status = 403
    if case == "dirty":
        after.dirty_generation = 1
        status = 503
    if case == "fence":
        after.fence_generation = 5
        status = 503
    if case == "version":
        principal.authorization_version = 2
        status = 503
    versions = Mock(side_effect=[before, after])
    authorize = Mock(return_value=decision)
    if case == "failure":
        authorize.side_effect = RuntimeError("private-original-session")
        status = 503
    monkeypatch.setattr(auth_adapter, "principal_version", versions)
    monkeypatch.setattr(auth_adapter, "authorize", authorize)
    adapter = auth_adapter.CurrentDraftAuthorization(engine, dependencies, decisions)
    if status == 503:
        with pytest.raises(PolicySnapshotUnavailable, match="Current rebase authorization"):
            adapter.check(raw_session="private-original-session", actor_id="actor")
    elif status is not None:
        with pytest.raises(DraftAuthorizationDenied) as denied:
            adapter.check(raw_session="private-original-session", actor_id="actor")
        assert denied.value.status_code == status
    else:
        adapter.check(raw_session="private-original-session", actor_id="actor")
    authorize.assert_called_once_with(
        db,
        raw_token="private-original-session",
        capability=PLATFORM_CONFIGURATION_MANAGE,
        scope=Scope.platform(),
        dependencies=dependencies,
        decision_dependencies=decisions,
    )
