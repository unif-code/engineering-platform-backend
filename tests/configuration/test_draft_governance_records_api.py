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
from tests.configuration.test_draft_governance_records import assert_read_only, governance_state


@pytest.fixture(params=["identity", "requirement.gate"])
def http_records(request: pytest.FixtureRequest) -> Any:
    h = governance_state(request.param)
    h.exits = 0

    @contextmanager
    def transaction() -> Any:
        try:
            yield h.lifecycle
        finally:
            h.exits += 1

    h.owners, h.guard, h.secrets = Mock(), Mock(), Mock()
    h.owners.resolve.return_value = SimpleNamespace(transaction=transaction)
    h.secrets.load.side_effect = AssertionError("read must not load idempotency material")
    runtime = ConfigurationHttpRuntime(h.owners, h.dependencies, h.secrets, h.authorization)
    app = FastAPI()
    app.include_router(
        create_configuration_router(
            lambda: runtime, lambda: SimpleNamespace(account_id="current"), h.guard
        )
    )
    h.app = app
    h.path = f"/api/v1/admin/policies/{h.namespace}/drafts/{h.draft.id}/governance-records"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        h.client = client
        yield h


def get(h: Any, **kwargs: Any) -> Any:
    return h.client.get(
        h.path, headers={"If-Match": '"v4"', "Origin": "https://cross.example"}, **kwargs
    )


def test_governance_read_uses_revision_no_store_and_read_only_owner_pages(
    http_records: Any,
) -> None:
    h = http_records
    first = get(h, params={"limit": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["draftId"] == h.draft.id and body["draftRevision"] == 4
    assert first.headers["etag"] == '"v4"' and first.headers["cache-control"] == "no-store"
    assert [r["afterRevision"] for r in body["rebases"]] == [4, 3] and body["nextCursor"] == "3"
    second = get(h, params={"limit": 2, "cursor": "3"})
    assert second.status_code == 200 and second.json()["cloneRecord"] == body["cloneRecord"]
    assert [r["afterRevision"] for r in second.json()["rebases"]] == [2]
    assert second.json()["nextCursor"] is None
    assert h.exits == 2
    h.secrets.load.assert_not_called()
    assert_read_only(h)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": "bad"},
        {"cursor": ""},
        {"cursor": "0"},
        {"cursor": "01"},
        {"cursor": "+2"},
        {"cursor": " 2"},
        {"cursor": "２"},
        {"cursor": "5"},
    ],
)
def test_invalid_paging_is_422(http_records: Any, params: Any) -> None:
    h = http_records
    assert get(h, params=params).status_code == 422
    assert h.owner.mock_calls == []


@pytest.mark.parametrize("etag", [None, 'W/"v4"', '"v0"', '"v04"', "4", '"v3"'])
def test_missing_invalid_and_stale_revision_is_controlled(
    http_records: Any, etag: str | None
) -> None:
    h = http_records
    result = h.client.get(h.path, headers={} if etag is None else {"If-Match": etag})
    assert result.status_code == (409 if etag == '"v3"' else 422)
    h.owner.clone_record.assert_not_called()
    assert_read_only(h)


@pytest.mark.parametrize("status", [401, 403, 503])
def test_denied_request_does_not_enter_owner_transaction(http_records: Any, status: int) -> None:
    h = http_records
    h.guard.side_effect = HTTPException(status)
    assert get(h).status_code == status
    h.owners.resolve.assert_not_called()
    assert h.owner.mock_calls == [] and h.exits == 0


@pytest.mark.parametrize(
    "case,status", [("missing", 404), ("changed", 409), ("bad-record", 503), ("dependency", 503)]
)
def test_unavailable_history_or_changed_target_returns_no_partial_page(
    http_records: Any, case: str, status: int
) -> None:
    h = http_records
    if case == "missing":
        h.owner.draft.side_effect = lambda *_a, **_k: None
    if case == "changed":
        h.owner.draft.side_effect = [h.draft, h.draft.model_copy(update={"revision": 5})]
    if case == "bad-record":
        h.clone_record["source_content_hash"] = "bad"
    if case == "dependency":
        h.owner.rebase_records.side_effect = RuntimeError("private sql and content")
    result = get(h)
    assert result.status_code == status, result.text
    assert result.headers["content-type"] == "application/problem+json"
    assert "private" not in result.text and "cloneRecord" not in result.text
    assert h.exits == 1
    assert_read_only(h)


def test_new_get_has_four_strict_models_and_no_write_inputs(http_records: Any) -> None:
    schema = http_records.app.openapi()
    path = "/api/v1/admin/policies/{namespace}/drafts/{draft_id}/governance-records"
    assert path in schema["paths"], "governance query missing"
    operation = schema["paths"][path]["get"]
    assert operation["operationId"] == "draft_governance_records" and "requestBody" not in operation
    params = {p["name"]: p for p in operation["parameters"]}
    assert params["If-Match"]["required"] and "Idempotency-Key" not in params
    assert (
        params["limit"]["schema"]["default"] == 50 and params["limit"]["schema"]["maximum"] == 100
    )
    models = schema["components"]["schemas"]
    for name in [
        "DraftGovernanceRecordsResponseDto",
        "DraftCloneRecordDto",
        "DraftRebaseRecordDto",
        "DraftRebaseSelectionDto",
    ]:
        model = models[name]
        assert model["additionalProperties"] is False
        assert set(model["required"]) == set(model["properties"])
    assert models["DraftCloneRecordDto"]["properties"]["source"]["$ref"].endswith(
        "/DraftCloneSourceDto"
    )
    assert models["DraftCloneRecordDto"]["properties"]["baseSnapshot"]["$ref"].endswith(
        "/PolicySnapshotDto"
    )
