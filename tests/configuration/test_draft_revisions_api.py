from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.configuration.api.routes import (
    ConfigurationHttpRuntime,
    create_configuration_router,
)
from tests.configuration.test_draft_revisions import record, state


@pytest.fixture(params=["identity", "requirement.gate"])
def history_api(request: pytest.FixtureRequest) -> Any:
    h = state([record(r, namespace=request.param) for r in (3, 2, 1)])
    h.target["namespace"] = request.param
    h.exits = 0

    @contextmanager
    def transaction() -> Any:
        try:
            yield h.lifecycle
        finally:
            h.exits += 1

    h.owners, h.guard, h.secrets, h.authorization = Mock(), Mock(), Mock(), Mock()
    h.owners.resolve.return_value = SimpleNamespace(transaction=transaction)
    h.secrets.load.side_effect = AssertionError("read must not load write material")
    h.authorization.check.side_effect = AssertionError("read must not use write authorization")
    runtime = ConfigurationHttpRuntime(h.owners, h.dependencies, h.secrets, h.authorization)
    app = FastAPI()
    app.include_router(
        create_configuration_router(
            lambda: runtime, lambda: SimpleNamespace(account_id="reader"), h.guard
        )
    )
    h.path = f"/api/v1/admin/policies/{request.param}/drafts/{h.target['id']}/revisions"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        h.client = client
        yield h


def get(h: Any, etag: str | None = '"v3"', **params: Any) -> Any:
    return h.client.get(h.path, headers={"If-Match": etag} if etag else {}, params=params)


def test_history_wire_has_required_nulls_exact_microseconds_and_no_store(history_api: Any) -> None:
    h = history_api
    response = get(h)
    assert response.status_code == 200
    assert response.headers["etag"] == '"v3"'
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["coverage"] == "COMPLETE" and body["recordedCount"] == 3
    assert body["items"][0]["recordedAt"] == "2026-10-18T08:00:00.123456Z"
    assert body["items"][0]["validation"] is None
    assert body["items"][0]["takeover"] is None
    assert body["items"][0]["governanceRecordId"] is None
    assert body["items"][0]["rollbackFromVersion"] is None
    assert "9007199254740993" in body["items"][0]["contentJson"]
    assert "content" not in body["items"][0] and "status" not in body
    assert h.exits == 1
    h.secrets.load.assert_not_called()
    h.authorization.check.assert_not_called()
    h.clock.now.assert_not_called()
    assert h.db.mock_calls == []


@pytest.mark.parametrize(
    "etag,status", [(None, 422), ('W/"v3"', 422), ('"v0"', 422), ('"v03"', 422), ('"v2"', 409)]
)
def test_history_requires_strong_current_revision(
    history_api: Any, etag: str | None, status: int
) -> None:
    assert get(history_api, etag).status_code == status
    history_api.owner.draft_revision_rows.assert_not_called()


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"cursor": "0"},
        {"cursor": "04"},
        {"cursor": "٤"},
        {"cursor": "4"},
    ],
)
def test_history_rejects_bad_pagination_without_reading_rows(
    history_api: Any, params: dict[str, Any]
) -> None:
    assert get(history_api, **params).status_code == 422
    history_api.owner.draft_revision_rows.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 503])
def test_history_authority_precedes_owner(history_api: Any, status: int) -> None:
    h = history_api
    h.guard.side_effect = HTTPException(status)
    assert get(h).status_code == status
    h.owners.resolve.assert_not_called()
    assert h.owner.mock_calls == []


@pytest.mark.parametrize(
    "fault,status", [("missing", 404), ("revision", 409), ("corrupt", 503), ("dependency", 503)]
)
def test_history_failures_are_not_empty_success(history_api: Any, fault: str, status: int) -> None:
    h = history_api
    if fault == "missing":
        h.owner.draft_revision_target.return_value = None
    elif fault == "revision":
        h.owner.draft_revision_target.side_effect = [h.target, {**h.target, "revision": 4}]
    elif fault == "corrupt":
        h.owner.draft_revision_rows.return_value[0]["revision_snapshot_hash"] = "0" * 64
    else:
        h.owner.draft_revision_stats.side_effect = RuntimeError("private persistence detail")
    response = get(h)
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert "private persistence detail" not in response.text
