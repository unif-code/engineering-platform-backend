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
from tests.configuration.test_draft_archive_timing import readonly, timing_state


@pytest.fixture(params=["identity", "requirement.gate"])
def http_timing(request: pytest.FixtureRequest) -> Any:
    h = timing_state(request.param)
    h.exits = 0

    @contextmanager
    def transaction() -> Any:
        try:
            yield h.lifecycle
        finally:
            h.exits += 1

    h.owners, h.guard, h.secrets, h.check = Mock(), Mock(), Mock(), Mock()
    h.owners.resolve.return_value = SimpleNamespace(transaction=transaction)
    h.secrets.load.side_effect = AssertionError("no write material")
    h.check.check.side_effect = AssertionError("no commit authorization")
    runtime = ConfigurationHttpRuntime(h.owners, h.dependencies, h.secrets, h.check)
    app = FastAPI()
    app.include_router(
        create_configuration_router(
            lambda: runtime, lambda: SimpleNamespace(account_id="another-viewer"), h.guard
        )
    )
    h.app = app
    h.path = f"/api/v1/admin/policies/{h.namespace}/drafts/{h.row['id']}/archive-timing"
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        h.client = client
        yield h


def get(h: Any, etag: str | None = '"v1"') -> Any:
    return h.client.get(
        h.path,
        headers={
            "Origin": "https://other.example",
            **({"If-Match": etag} if etag is not None else {}),
        },
    )


def test_timing_reads_other_owner_with_one_clock_no_write_inputs_and_strong_target_etag(
    http_timing: Any,
) -> None:
    h = http_timing
    response = get(h)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "draft",
        "current",
        "archiveAfterDays",
        "observedAt",
        "expectedArchiveAt",
        "inactivityElapsed",
    }
    assert body["draft"]["id"] == h.row["id"] and body["draft"]["revision"] == 1
    assert body["archiveAfterDays"] == 30 and body["inactivityElapsed"] is True
    assert response.headers["etag"] == '"v1"' and response.headers["cache-control"] == "no-store"
    assert "content" not in body["draft"] and "validationEvidence" not in body["draft"]
    h.clock.now.assert_called_once_with()
    h.secrets.load.assert_not_called()
    h.check.check.assert_not_called()
    readonly(h)


def test_archived_computations_are_required_nulls(http_timing: Any) -> None:
    h = http_timing
    h.row.update(status="ARCHIVED", archived_at=h.now)
    response = get(h)
    assert response.status_code == 200, response.text
    assert (
        response.json()["expectedArchiveAt"] is None
        and response.json()["inactivityElapsed"] is None
    )
    readonly(h)


@pytest.mark.parametrize("etag", [None, 'W/"v1"', '"v0"', '"v01"', "1", '"v2"'])
def test_etag_preflight_and_stale_target_status(http_timing: Any, etag: str | None) -> None:
    h = http_timing
    response = get(h, etag)
    assert response.status_code == (409 if etag == '"v2"' else 422), response.text
    h.clock.now.assert_not_called()
    readonly(h)


@pytest.mark.parametrize("status", [401, 403, 503])
def test_initial_authorization_does_not_enter_owner_transaction(
    http_timing: Any, status: int
) -> None:
    h = http_timing
    h.guard.side_effect = HTTPException(status)
    assert get(h).status_code == status
    h.owners.resolve.assert_not_called()
    assert h.owner.mock_calls == []


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", 404),
        ("namespace", 404),
        ("scope", 404),
        ("changed", 409),
        ("publication", 409),
        ("bad-period", 503),
        ("bad-clock", 503),
        ("bad-summary", 503),
        ("dependency", 503),
        ("unwired", 503),
    ],
)
def test_query_conflicts_and_corrupt_reads_have_no_partial_time_or_private_details(
    http_timing: Any, case: str, status: int
) -> None:
    h = http_timing
    if case == "missing":
        h.owner.draft_summary.side_effect = lambda *_: None
    elif case in {"namespace", "scope"}:
        h.owner.draft_summary.side_effect = lambda *_: {**h.row, case: "other"}
    elif case == "changed":
        h.owner.draft_summary.side_effect = [h.row, {**h.row, "owner_id": "changed"}]
    elif case == "publication":
        h.owner.read_archive_settings.side_effect = [
            (h.current, h.interval),
            (h.current.model_copy(update={"version": 3}), h.interval),
        ]
    elif case == "bad-period":
        h.interval = None
    elif case == "bad-clock":
        h.now = h.now.replace(tzinfo=None)
    elif case == "bad-summary":
        h.row["content_hash"] = "private-corrupt-summary"
    elif case == "dependency":
        h.owner.draft_summary.side_effect = RuntimeError("private sql failure")
    else:
        h.owners.resolve.side_effect = PolicySnapshotUnavailable("private owner unavailable")
    response = get(h)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert "private" not in response.text and "expectedArchiveAt" not in response.json()
    assert h.exits == (0 if case == "unwired" else 1)
    readonly(h)


def test_one_new_strict_dto_reuses_existing_summary_and_snapshot_without_query_or_writes(
    http_timing: Any,
) -> None:
    schema = http_timing.app.openapi()
    path = "/api/v1/admin/policies/{namespace}/drafts/{draft_id}/archive-timing"
    assert path in schema["paths"], "archive timing endpoint missing"
    op = schema["paths"][path]["get"]
    assert op["operationId"] == "draft_archive_timing" and "requestBody" not in op
    assert {p["name"] for p in op["parameters"]} == {"namespace", "draft_id", "If-Match"}
    assert op["responses"]["200"]["headers"]["ETag"]
    model = schema["components"]["schemas"]["DraftArchiveTimingResponseDto"]
    assert model["additionalProperties"] is False and set(model["required"]) == set(
        model["properties"]
    )
    assert set(model["properties"]) == {
        "draft",
        "current",
        "archiveAfterDays",
        "observedAt",
        "expectedArchiveAt",
        "inactivityElapsed",
    }
    assert model["properties"]["draft"]["$ref"].endswith("/DraftListItemDto")
    assert model["properties"]["current"]["$ref"].endswith("/PolicySnapshotDto")
