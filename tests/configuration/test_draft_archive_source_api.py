import json
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
from tests.configuration.test_draft_archive_source import readonly, source_state


@pytest.fixture(params=["identity", "requirement.gate"])
def http_source(request: pytest.FixtureRequest) -> Any:
    h = source_state(request.param)
    h.exits = 0

    @contextmanager
    def transaction() -> Any:
        try:
            yield h.lifecycle
        finally:
            h.exits += 1

    h.owners, h.guard, h.secrets, h.authorization = Mock(), Mock(), Mock(), Mock()
    h.owners.resolve.return_value = SimpleNamespace(transaction=transaction)
    h.secrets.load.side_effect = AssertionError("no write material")
    h.authorization.check.side_effect = AssertionError("no write authorization")
    runtime = ConfigurationHttpRuntime(h.owners, h.dependencies, h.secrets, h.authorization)
    app = FastAPI()
    app.include_router(
        create_configuration_router(
            lambda: runtime, lambda: SimpleNamespace(account_id="another-viewer"), h.guard
        )
    )
    h.path = f"/api/v1/admin/policies/{h.namespace}/drafts/{h.row['id']}/archive-source"
    h.app = app
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        h.client = client
        yield h


def get(h: Any, etag: str | None = '"v1"') -> Any:
    return h.client.get(
        h.path, headers={"Origin": "https://other.example", **({"If-Match": etag} if etag else {})}
    )


@pytest.mark.parametrize("kind", ["MANUAL", "INACTIVITY", "UNKNOWN", "NOT_ARCHIVED"])
def test_success_has_exact_target_etag_no_store_and_only_real_fields(
    http_source: Any, kind: str
) -> None:
    h = http_source
    if kind == "INACTIVITY":
        h.records = source_state(h.namespace, kind).records
    elif kind in {"UNKNOWN", "NOT_ARCHIVED"}:
        h.records = []
        if kind == "NOT_ARCHIVED":
            h.row.update(status="DRAFT", archived_at=None)
    response = get(h)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "draftId",
        "namespace",
        "scope",
        "draftRevision",
        "status",
        "archivedAt",
        "recordStatus",
        "record",
    }
    assert body["draftId"] == h.row["id"] and body["draftRevision"] == h.row["revision"]
    assert response.headers["etag"] == '"v1"' and response.headers["cache-control"] == "no-store"
    if kind in {"UNKNOWN", "NOT_ARCHIVED"}:
        assert body["recordStatus"] == kind and body["record"] is None
    else:
        assert body["recordStatus"] == "RECORDED" and body["record"]["source"] == kind
        assert (
            "actorId" in body["record"] if kind == "MANUAL" else "policyVersion" in body["record"]
        )
    assert "payload" not in response.text and "deliveredAt" not in response.text
    h.secrets.load.assert_not_called()
    h.authorization.check.assert_not_called()
    assert h.exits == 1
    readonly(h)


@pytest.mark.parametrize("etag", [None, 'W/"v1"', '"v0"', '"v01"', "1", '"v2"'])
def test_required_strong_revision_preflight(http_source: Any, etag: str | None) -> None:
    h = http_source
    assert get(h, etag).status_code == (409 if etag == '"v2"' else 422)
    h.owner.archive_records.assert_not_called()
    readonly(h)


@pytest.mark.parametrize("status", [401, 403, 503])
def test_current_read_authority_precedes_owner_access(http_source: Any, status: int) -> None:
    h = http_source
    h.guard.side_effect = HTTPException(status)
    assert get(h).status_code == status
    h.owners.resolve.assert_not_called()
    assert h.owner.mock_calls == []


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", 404),
        ("namespace", 404),
        ("changed", 409),
        ("duplicate", 503),
        ("record", 503),
        ("dependency", 503),
        ("unwired", 503),
    ],
)
def test_controlled_failures_never_expose_partial_source(
    http_source: Any, case: str, status: int
) -> None:
    h = http_source
    if case == "missing":
        h.owner.draft_summary.side_effect = lambda *_: None
    elif case == "namespace":
        h.owner.draft_summary.side_effect = lambda *_: {**h.row, "namespace": "other"}
    elif case == "changed":
        h.owner.draft_summary.side_effect = [h.row, {**h.row, "owner_id": "changed"}]
    elif case == "duplicate":
        h.records *= 2
    elif case == "record":
        h.records[0]["payload"]["source"] = "private-bad-source"
    elif case == "dependency":
        h.owner.archive_records.side_effect = RuntimeError("private SQL failure")
    else:
        h.owners.resolve.side_effect = PolicySnapshotUnavailable("private owner unavailable")
    response = get(h)
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert "recordStatus" not in response.json() and "private" not in response.text
    assert h.exits == (0 if case == "unwired" else 1)
    readonly(h)


def test_strict_required_three_dtos_expose_discriminated_sources_without_query_inputs(
    http_source: Any,
) -> None:
    schema = http_source.app.openapi()
    path = "/api/v1/admin/policies/{namespace}/drafts/{draft_id}/archive-source"
    assert path in schema["paths"], "source route is missing"
    operation = schema["paths"][path]["get"]
    assert operation["operationId"] == "draft_archive_source" and "requestBody" not in operation
    assert {p["name"] for p in operation["parameters"]} == {"namespace", "draft_id", "If-Match"}
    models = schema["components"]["schemas"]
    for name in (
        "DraftArchiveSourceResponseDto",
        "DraftManualArchiveRecordDto",
        "DraftInactivityArchiveRecordDto",
    ):
        model = models[name]
        assert model["additionalProperties"] is False and set(model["required"]) == set(
            model["properties"]
        )
    record = json.dumps(models["DraftArchiveSourceResponseDto"]["properties"]["record"])
    assert (
        '"discriminator"' in record
        and '"MANUAL"' in record
        and '"INACTIVITY"' in record
        and '"null"' in record
    )
