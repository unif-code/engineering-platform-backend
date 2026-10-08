from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.configuration import PolicySnapshotUnavailable
from control_plane.app.modules.configuration.api.routes import (
    ConfigurationHttpRuntime,
    create_configuration_router,
)
from tests.configuration.test_draft_directory import ACTOR, OTHER, directory_state, readonly


@pytest.fixture(params=["identity", "requirement.gate"])
def http_directory(request: pytest.FixtureRequest) -> Any:
    h = directory_state(request.param)
    h.exits = 0

    @contextmanager
    def transaction() -> Any:
        try:
            yield h.lifecycle
        finally:
            h.exits += 1

    h.owners, h.guard, h.secrets, h.check = Mock(), Mock(), Mock(), Mock()
    h.owners.resolve.return_value = SimpleNamespace(transaction=transaction)
    h.secrets.load.side_effect = AssertionError("read must not load write keys")
    h.check.check.side_effect = AssertionError("read must not run commit authorization")
    runtime = ConfigurationHttpRuntime(h.owners, h.dependencies, h.secrets, h.check)
    app = FastAPI()
    app.include_router(
        create_configuration_router(
            lambda: runtime, lambda: SimpleNamespace(account_id=ACTOR), h.guard
        )
    )
    h.app = app
    h.path = f"/api/v1/admin/policies/{h.namespace}/drafts"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        h.client = client
        yield h


def get(h: Any, params: Any = None) -> Any:
    return h.client.get(h.path, params=params, headers={"Origin": "https://unrelated.example"})


def test_directory_default_and_mine_use_no_write_preflight_or_collection_etag(
    http_directory: Any,
) -> None:
    h = http_directory
    response = get(h)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["currentVersion"] == 2 and body["view"] == body["owner"] == "ALL"
    assert len(body["items"]) == 6 and body["nextCursor"] is None
    assert response.headers["cache-control"] == "no-store" and "etag" not in response.headers
    mine = get(h, {"owner": "MINE", "ownerId": OTHER}).json()
    assert mine["owner"] == "MINE" and all(row["ownerId"] == ACTOR for row in mine["items"])
    assert "content" not in body["items"][0] and "stale" not in body["items"][0]
    h.secrets.load.assert_not_called()
    h.check.check.assert_not_called()
    readonly(h)


def test_directory_binds_continuation_current_and_accepts_empty_changed_collection(
    http_directory: Any,
) -> None:
    h = http_directory
    first = get(h, {"limit": 1}).json()
    assert first["nextCursor"] == h.rows[0]["id"]
    assert get(h, {"limit": 1, "cursor": first["nextCursor"]}).status_code == 422
    assert (
        get(h, {"limit": 1, "cursor": first["nextCursor"], "current_version": 1}).status_code == 409
    )
    h.rows.clear()
    empty = get(h, {"limit": 1, "cursor": first["nextCursor"], "current_version": 2})
    assert (
        empty.status_code == 200
        and empty.json()["items"] == []
        and empty.json()["nextCursor"] is None
    )
    readonly(h)


@pytest.mark.parametrize(
    "params",
    [
        {"view": "active"},
        {"view": "UNKNOWN"},
        {"owner": "actor"},
        {"owner": "mine"},
        {"limit": 0},
        {"limit": 101},
        {"limit": "1.5"},
        {"cursor": "not-uuid", "current_version": 2},
        {"cursor": "81000000-0000-4000-8AAA-000000000001", "current_version": 2},
        {"cursor": "81000000000040008000000000000001", "current_version": 2},
        {"current_version": 0},
        {"current_version": "bad"},
    ],
)
def test_invalid_query_is_422_and_never_reads_owner(http_directory: Any, params: Any) -> None:
    h = http_directory
    response = get(h, params)
    assert response.status_code == 422, response.text
    assert h.owner.mock_calls == []


@pytest.mark.parametrize("status", [401, 403, 503])
def test_current_qualification_refuses_before_owner_or_summary_read(
    http_directory: Any, status: int
) -> None:
    h = http_directory
    h.guard.side_effect = HTTPException(status)
    assert get(h).status_code == status
    h.owners.resolve.assert_not_called()
    assert h.owner.mock_calls == []


@pytest.mark.parametrize(
    "case,status",
    [("missing-owner", 503), ("metadata", 503), ("dependency", 503), ("publication", 409)],
)
def test_bad_facts_or_current_race_return_controlled_error_without_partial_items(
    http_directory: Any, case: str, status: int
) -> None:
    h = http_directory
    if case == "missing-owner":
        h.owners.resolve.side_effect = PolicySnapshotUnavailable("private owner details")
    if case == "metadata":
        h.rows[0]["content_hash"] = "private-bad-hash"
    if case == "dependency":
        h.owner.list_draft_summaries.side_effect = RuntimeError("private SQL and content")
    if case == "publication":
        h.owner.active_snapshot.side_effect = [
            h.current,
            h.current.model_copy(update={"version": 3}),
        ]
    response = get(h)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert "private" not in response.text and "items" not in response.json()
    assert h.exits == (0 if case == "missing-owner" else 1)
    readonly(h)


def test_directory_openapi_is_one_get_with_two_strict_metadata_models(http_directory: Any) -> None:
    schema = http_directory.app.openapi()
    path = schema["paths"]["/api/v1/admin/policies/{namespace}/drafts"]
    assert "get" in path, "draft directory GET missing"
    operation = path["get"]
    assert operation["operationId"] == "draft_list" and "post" in path
    assert "requestBody" not in operation
    params = {p["name"]: p for p in operation["parameters"]}
    assert set(params) == {"namespace", "view", "owner", "cursor", "current_version", "limit"}
    assert (
        params["limit"]["schema"]["default"] == 50 and params["limit"]["schema"]["maximum"] == 100
    )
    assert (
        params["view"]["schema"]["default"] == "ALL"
        and params["owner"]["schema"]["default"] == "ALL"
    )
    assert "ETag" not in operation["responses"]["200"]["headers"]
    models = schema["components"]["schemas"]
    for name in ["DraftListItemDto", "DraftListResponseDto"]:
        model = models[name]
        assert model["additionalProperties"] is False
        assert set(model["required"]) == set(model["properties"])
    assert set(models["DraftListItemDto"]["properties"]) == {
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
