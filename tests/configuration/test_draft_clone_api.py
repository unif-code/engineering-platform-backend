from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.configuration import (
    DraftAuthorizationDenied,
    PolicySnapshotUnavailable,
)
from control_plane.app.modules.configuration.api.routes import (
    ConfigurationHttpRuntime,
    create_configuration_router,
)
from tests.configuration.test_api import _Secrets
from tests.configuration.test_draft_clone import clone_state
from tests.configuration.test_draft_rebase_api import ScopedMemory


@pytest.fixture(params=["identity", "requirement.gate"])
def http_clone(request: pytest.FixtureRequest) -> Any:
    h = clone_state(request.param)
    h.memory = ScopedMemory()
    for name in ("claim_idempotency", "idempotency_by_scope", "complete_idempotency"):
        setattr(h.owner, name, getattr(h.memory, name))
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
            lambda: h.runtime, lambda: SimpleNamespace(account_id="current-admin"), h.guard
        )
    )
    h.path = f"/api/v1/admin/policies/{h.namespace}/drafts/{h.draft.id}/clone"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        client.cookies.set("ep_session", "private-original-session")
        h.client, h.app = client, app
        yield h


def post(
    h: Any,
    *,
    body: Any = None,
    etag: str = '"v7"',
    key: str = "clone-original-key",
    origin: str = "https://testserver",
) -> Any:
    return h.client.post(
        h.path,
        json={} if body is None else body,
        headers={
            "If-Match": etag,
            "Idempotency-Key": key,
            "Origin": origin,
        },
    )


def test_clone_201_replay_keeps_original_creation_without_locks_or_commit_check(
    http_clone: Any,
) -> None:
    h = http_clone
    accepted = post(h)
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert set(body) == {"source", "currentVersionAtClone", "draft"}
    assert body["source"]["draftId"] == h.draft.id
    assert body["source"]["ownerId"] == h.draft.owner_id
    assert body["draft"]["id"] != h.draft.id and body["draft"]["ownerId"] == "current-admin"
    assert body["draft"]["baseVersion"] == 1 and body["draft"]["stale"] is True
    assert body["draft"]["revision"] == 1 and accepted.headers["etag"] == '"v1"'
    h.authorization.check.assert_called_once_with(
        raw_session="private-original-session", actor_id="current-admin"
    )
    locked = h.owner.locked_active_snapshot.call_count
    normalized = h.owner.normalize_candidate.call_count
    h.owner.draft.return_value = None
    h.runtime = replace(h.runtime, draft_authorization=None)
    replay = post(h)
    assert replay.status_code == 201 and replay.content == accepted.content
    assert replay.headers["etag"] == '"v1"'
    assert h.owner.locked_active_snapshot.call_count == locked
    assert h.owner.normalize_candidate.call_count == normalized
    h.owner.record_clone.assert_called_once()
    h.owner.create_draft.assert_called_once()
    h.audit.append_in_transaction.assert_called_once()
    assert "private-original-session" not in str(h.owner.record_clone.call_args)
    assert post(h, etag='"v8"').status_code == 409
    h.path = h.path.replace(h.draft.id, "another-draft")
    assert post(h).status_code == 409
    h.guard.side_effect = HTTPException(403)
    count = h.owners.resolve.call_count
    assert post(h).status_code == 403
    assert h.owners.resolve.call_count == count


def test_different_keys_from_same_source_revision_create_independent_copies(
    http_clone: Any,
) -> None:
    h = http_clone
    first, second = post(h), post(h, key="clone-second-key")
    assert first.status_code == second.status_code == 201
    assert first.json()["draft"]["id"] != second.json()["draft"]["id"]
    assert first.json()["source"] == second.json()["source"]
    assert h.owner.record_clone.call_count == 2 and h.owner.create_draft.call_count == 2


@pytest.mark.parametrize("status", [401, 403, 503])
def test_initial_authorization_and_write_time_check_both_fail_closed(
    http_clone: Any, status: int
) -> None:
    h = http_clone
    h.guard.side_effect = HTTPException(status)
    assert post(h).status_code == status
    h.owners.resolve.assert_not_called()
    h.guard.side_effect = None
    h.authorization.check.side_effect = (
        PolicySnapshotUnavailable("unavailable")
        if status == 503
        else DraftAuthorizationDenied(401 if status == 401 else 403)
    )
    assert post(h).status_code == status
    h.owner.create_draft.assert_not_called()
    h.owner.record_clone.assert_not_called()
    assert h.exits == 1


@pytest.mark.parametrize(
    "body",
    [
        [],
        "{}",
        0,
        True,
        {"values": {}},
        {"content": {}},
        {"ownerId": "chosen"},
        {"baseVersion": 1},
        {"schemaRevision": 1},
        {"source": {}},
        {"reason": "no reason"},
        {"totpCode": "123456"},
        {"expectedRevision": 7},
    ],
)
def test_clone_accepts_only_an_explicit_empty_object(http_clone: Any, body: Any) -> None:
    h = http_clone
    assert post(h, body=body).status_code == 422
    h.owners.resolve.assert_not_called()


def test_missing_null_body_headers_cross_origin_and_unwired_check(http_clone: Any) -> None:
    h = http_clone
    headers = {
        "If-Match": '"v7"',
        "Idempotency-Key": "clone-original-key",
        "Origin": "https://testserver",
    }
    assert h.client.post(h.path, headers=headers).status_code == 422
    assert (
        h.client.post(
            h.path, content="null", headers={**headers, "Content-Type": "application/json"}
        ).status_code
        == 422
    )
    assert post(h, origin="https://cross.example").status_code == 403
    assert post(h, etag='W/"v7"').status_code == 422
    assert post(h, key="short").status_code == 422
    for missing in ("If-Match", "Idempotency-Key"):
        assert (
            h.client.post(
                h.path, json={}, headers={k: v for k, v in headers.items() if k != missing}
            ).status_code
            == 422
        )
    h.owners.resolve.assert_not_called()
    h.runtime = replace(h.runtime, draft_authorization=None)
    assert post(h).status_code == 503
    h.owner.create_draft.assert_not_called()


def test_clone_openapi_has_three_strict_dtos_and_old_nested_draft(http_clone: Any) -> None:
    schema = http_clone.app.openapi()
    path = "/api/v1/admin/policies/{namespace}/drafts/{draft_id}/clone"
    assert path in schema["paths"], "clone endpoint is missing"
    operation = schema["paths"][path]["post"]
    assert operation["operationId"] == "draft_clone"
    assert operation["requestBody"]["required"] is True
    assert operation["responses"]["201"]["headers"]["ETag"]
    models = schema["components"]["schemas"]
    for name in ("CloneDraftRequestDto", "DraftCloneSourceDto", "DraftCloneResponseDto"):
        assert models[name]["additionalProperties"] is False
    assert models["CloneDraftRequestDto"]["properties"] == {}
    assert set(models["DraftCloneResponseDto"]["required"]) == {
        "source",
        "currentVersionAtClone",
        "draft",
    }
    assert models["DraftCloneResponseDto"]["properties"]["draft"]["$ref"].endswith(
        "/DraftResponseDto"
    )
    assert set(models["DraftCloneSourceDto"]["required"]) == {
        "draftId",
        "revision",
        "ownerId",
        "status",
        "baseVersion",
        "schemaRevision",
        "contentHash",
        "rollbackFromVersion",
        "clonedFromArchivedDraftId",
    }
