from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.app.modules.model_gateway.api import dto

MATERIAL = {
    "category": "CONTEXT_LIMITS",
    "title": " Provider context specification ",
    "sourceReference": "https://docs.example.org/model%20spec?v=2#limits",
    "externalVersion": "2026-10",
    "declaredContentSha256": "a" * 64,
    "expiresAt": "2026-10-02T10:00:00+08:00",
    "note": "Recorded source only",
}
CHECK_ID = "20000000-0000-4000-8000-000000000001"


def request_type() -> type[dto.CreateValidationDossierRequestDto]:
    assert hasattr(dto, "CreateValidationDossierRequestDto"), "typed dossier input is missing"
    return dto.CreateValidationDossierRequestDto


def test_declared_material_normalization_preserves_path_without_executing_a_source() -> None:
    value = request_type().model_validate({"materials": [MATERIAL], "checkIds": [CHECK_ID]})
    material = value.materials[0]
    assert material.title == "Provider context specification"
    assert material.source_reference == "https://docs.example.org/model%20spec"
    assert material.expires_at == datetime(2026, 10, 2, 2, tzinfo=UTC)
    assert material.declared_content_sha256 == "a" * 64
    assert request_type().model_validate({"materials": [], "checkIds": [CHECK_ID]})
    assert request_type().model_validate(
        {"materials": [MATERIAL | {"sourceReference": "urn:provider:spec:2026-10"}]}
    )


@pytest.mark.parametrize(
    "change",
    [
        {"category": "VERIFIED"},
        {"title": ""},
        {"title": "x" * 121},
        {"title": "Title\n"},
        {"note": "Bearer sensitive"},
        {"externalVersion": "api_key=private"},
        {"sourceReference": "file:///tmp/material"},
        {"sourceReference": "http://example.org"},
        {"sourceReference": "https://user:pass@example.org/spec"},
        {"sourceReference": "https://example.org/%0aheader"},
        {"sourceReference": "https://example.org/spec?api_key=private"},
        {"sourceReference": "urn:spec:sk-sensitive"},
        {"sourceReference": "https://example.org/" + "a" * 2048},
        {"declaredContentSha256": "fake"},
        {"expiresAt": "2026-10-02T02:00:00"},
        {"expiresAt": "0001-01-01T00:00:00+08:00"},
        {"expiresAt": "9999-12-31T23:59:59-08:00"},
        {"expiresAt": 1790906400},
        {"provenance": "DECLARED"},
        {"body": "raw source"},
    ],
)
def test_material_rejects_sensitive_unbounded_and_client_asserted_evidence(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        request_type().model_validate({"materials": [MATERIAL | change]})


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"materials": [], "checkIds": []},
        {"materials": [MATERIAL] * 33},
        {"checkIds": [CHECK_ID] * 2},
        {"checkIds": [True]},
        {"checkIds": [CHECK_ID], "snapshotHash": "a" * 64},
        {"checkIds": [CHECK_ID], "checks": [{"state": "SUCCEEDED"}]},
        {"checkIds": [CHECK_ID], "state": "VERIFIED"},
    ],
)
def test_dossier_request_only_accepts_bounded_materials_and_explicit_check_ids(
    body: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        request_type().model_validate(body)


def test_default_app_publishes_three_dossier_operations_and_explicit_bounds() -> None:
    from control_plane.app.bootstrap.app import create_app

    schema = create_app().openapi()
    base = "/api/v1/admin/model-deployments/{deploymentId}/validation-dossiers"
    assert base in schema["paths"], "default dossier routes are missing"
    assert set(schema["paths"][base]) == {"get", "post"}
    assert set(schema["paths"][base + "/{dossierId}"]) == {"get"}
    assert schema["paths"][base]["post"]["operationId"] == "model_validation_dossiers_create"
    assert "201" in schema["paths"][base]["post"]["responses"]
    assert "413" in schema["paths"][base]["post"]["responses"]
    fields = schema["components"]["schemas"]["CreateValidationDossierRequestDto"]
    assert set(fields["properties"]) == {"materials", "checkIds"}
    assert fields["properties"]["materials"]["maxItems"] == 32
    assert fields["properties"]["checkIds"]["maxItems"] == 5
    assert fields["x-maxBodyBytes"] == 65536
    assert fields["additionalProperties"] is False


def test_actual_request_bytes_are_bounded_before_json_or_runtime_access() -> None:
    from fastapi.testclient import TestClient

    from control_plane.app.bootstrap.app import create_app

    with TestClient(create_app(), base_url="https://testserver") as client:
        path = f"/api/v1/admin/model-deployments/{CHECK_ID}/validation-dossiers"
        for content in (b" " * 65536 + b"{}", iter([b" " * 32768, b" " * 32768, b"{}"])):
            result = client.post(
                path, content=content, headers={"Content-Type": "application/json"}
            )
            assert result.status_code == 413
            assert result.headers["content-type"].startswith("application/problem+json")
