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
from tests.configuration.test_draft_manual_archive import archive_state
from tests.configuration.test_draft_rebase_api import ScopedMemory


@pytest.fixture(params=["identity", "requirement.gate"])
def http_archive(request: pytest.FixtureRequest) -> Any:
    h = archive_state(request.param)
    h.memory = ScopedMemory()
    for name in ("claim_idempotency", "idempotency_by_scope", "complete_idempotency"):
        setattr(h.owner, name, getattr(h.memory, name))
    h.exits = 0

    @contextmanager
    def transaction() -> Any:
        before = deepcopy((h.memory.rows, h.draft))
        try:
            yield h.lifecycle
        except Exception:
            h.memory.rows, h.draft = before
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
    h.path = f"/api/v1/admin/policies/{h.namespace}/drafts/known-draft/archive"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        client.cookies.set("ep_session", "private-original-session")
        h.client, h.app = client, app
        yield h


def headers(**values: str) -> dict[str, str]:
    return {
        "If-Match": '"v7"',
        "Idempotency-Key": "manual-archive-key",
        "Origin": "https://testserver",
        **values,
    }


def post(h: Any, **values: str) -> Any:
    return h.client.post(h.path, json={}, headers=headers(**values))


def test_archive_receipt_and_historical_replay_preserve_time_etag_and_one_fact(
    http_archive: Any,
) -> None:
    h = http_archive
    before = deepcopy(h.draft)
    response = post(h)
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {"draftId", "namespace", "scope", "draftRevision", "status", "archivedAt"}
    assert data["draftId"] == before.id and data["draftRevision"] == before.revision
    assert data["namespace"] == h.namespace and data["scope"] == "PLATFORM"
    assert data["status"] == "ARCHIVED" and data["archivedAt"]
    assert response.headers["etag"] == '"v7"' and response.headers["cache-control"] == "no-store"
    assert h.draft.revision == before.revision and h.draft.status == "ARCHIVED"
    h.authorization.check.assert_called_once_with(
        raw_session="private-original-session", actor_id="current-admin"
    )
    h.runtime = replace(h.runtime, draft_authorization=None)
    replay = post(h)
    assert replay.status_code == 200 and replay.content == response.content
    assert replay.headers["etag"] == '"v7"' and replay.headers["cache-control"] == "no-store"
    h.owner.draft.assert_called_once()
    h.owner.archive_draft.assert_called_once()
    h.audit.append_in_transaction.assert_called_once()
    assert post(h, **{"If-Match": '"v8"'}).status_code == 409
    h.runtime = replace(h.runtime, draft_authorization=h.authorization)
    assert post(h, **{"Idempotency-Key": "another-key"}).status_code == 409
    h.guard.side_effect = HTTPException(403)
    count = h.owners.resolve.call_count
    assert post(h).status_code == 403 and h.owners.resolve.call_count == count


@pytest.mark.parametrize(
    "body",
    [
        [],
        "{}",
        0,
        True,
        {"reason": "archive"},
        {"totpCode": "123456"},
        {"source": "MANUAL"},
        {"archivedAt": "2026-10-15"},
    ],
)
def test_only_an_explicit_empty_object_is_accepted(http_archive: Any, body: Any) -> None:
    h = http_archive
    assert h.client.post(h.path, json=body, headers=headers()).status_code == 422
    h.owners.resolve.assert_not_called()


@pytest.mark.parametrize("body", [b"", b"null"])
def test_missing_or_null_body_is_not_an_empty_command(http_archive: Any, body: bytes) -> None:
    h = http_archive
    response = h.client.post(
        h.path, content=body, headers={**headers(), "Content-Type": "application/json"}
    )
    assert response.status_code == 422
    h.owners.resolve.assert_not_called()


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("If-Match", None, 422),
        ("If-Match", 'W/"v7"', 422),
        ("If-Match", '"v0"', 422),
        ("Idempotency-Key", None, 422),
        ("Idempotency-Key", "", 422),
        ("Sec-Fetch-Site", "cross-site", 403),
        ("Origin", "https://other.example", 403),
    ],
)
def test_required_write_headers_fail_before_owner(
    http_archive: Any, field: str, value: str | None, status: int
) -> None:
    h = http_archive
    request_headers = headers()
    if value is None:
        request_headers.pop(field)
    else:
        request_headers[field] = value
    assert h.client.post(h.path, json={}, headers=request_headers).status_code == status
    h.owners.resolve.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 503])
def test_current_qualification_is_checked_initially_and_after_lock(
    http_archive: Any, status: int
) -> None:
    h = http_archive
    h.guard.side_effect = HTTPException(status)
    assert post(h).status_code == status
    h.owners.resolve.assert_not_called()
    h.guard.side_effect = None
    h.authorization.check.side_effect = (
        PolicySnapshotUnavailable("private authority")
        if status == 503
        else DraftAuthorizationDenied(401 if status == 401 else 403)
    )
    assert post(h).status_code == status
    h.owner.archive_draft.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()
    assert h.exits == 1


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", 404),
        ("namespace", 404),
        ("revision", 409),
        ("owner", 403),
        ("archived", 409),
        ("unwired", 503),
    ],
)
def test_controlled_target_failures_do_not_create_archive_facts(
    http_archive: Any, case: str, status: int
) -> None:
    h = http_archive
    if case == "missing":
        h.owner.draft.side_effect = lambda *_, **__: None
    elif case == "unwired":
        h.runtime = replace(h.runtime, draft_authorization=None)
    else:
        updates = {
            "namespace": {"namespace": "other"},
            "revision": {"revision": 8},
            "owner": {"owner_id": "other"},
            "archived": {"status": "ARCHIVED", "archived_at": h.now},
        }
        h.draft = h.draft.model_copy(update=updates[case])
    response = post(h)
    assert response.status_code == status, response.text
    assert "archivedAt" not in response.json() and "private" not in response.text
    h.owner.archive_draft.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()


def test_public_command_has_one_strict_receipt_and_reuses_existing_empty_dto(
    http_archive: Any,
) -> None:
    schema = http_archive.app.openapi()
    path = "/api/v1/admin/policies/{namespace}/drafts/{draft_id}/archive"
    assert path in schema["paths"], "manual archive route is missing"
    operation = schema["paths"][path]["post"]
    assert operation["operationId"] == "draft_archive"
    assert operation["requestBody"]["required"] is True
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/CloneDraftRequestDto"
    )
    response = schema["components"]["schemas"]["DraftArchiveResponseDto"]
    assert response["additionalProperties"] is False
    assert (
        set(response["required"])
        == set(response["properties"])
        == {"draftId", "namespace", "scope", "draftRevision", "status", "archivedAt"}
    )
