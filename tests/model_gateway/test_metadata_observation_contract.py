"""The default HTTP entry is real; these checks never resolve a DB or Provider secret."""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.authorization import AuthorizationPrincipal
from control_plane.app.modules.model_gateway.adapters.metadata_sources import (
    FileMetadataSourceDirectory,
    MetadataSourceSettings,
)
from control_plane.app.modules.model_gateway.domain.metadata import MetadataBlocked, MetadataReason
from tests.model_gateway.test_metadata_adapter import SOURCE

BASE = "/api/v1/admin/model-deployments/{deploymentId}/metadata-observations"


def test_metadata_route_applies_portal_authorization_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied() -> AuthorizationPrincipal:
        raise HTTPException(status_code=401, detail="Synthetic missing Session")

    monkeypatch.setattr(bootstrap, "current_principal", lambda _runtime: denied)
    with TestClient(bootstrap.create_app(), raise_server_exceptions=False) as client:
        response = client.get(
            BASE.replace("{deploymentId}", "10000000-0000-4000-8000-000000000001")
        )
    print("METADATA37_ENTRY", response.status_code)
    assert response.status_code == 401


def test_metadata_contract_has_dedicated_receipt_history_and_current_admission() -> None:
    schema = bootstrap.create_app().openapi()
    assert BASE in schema["paths"]
    operations = schema["paths"][BASE]
    assert operations["post"]["operationId"] == "model_metadata_observations_create"
    assert "202" in operations["post"]["responses"]
    assert operations["get"]["operationId"] == "model_metadata_observations_list"
    assert schema["paths"][BASE + "/{observationId}"]["get"]["operationId"] == (
        "model_metadata_observations_get"
    )
    request = schema["components"]["schemas"]["CreateMetadataObservationRequestDto"]
    assert request["additionalProperties"] is False
    assert request["required"] == ["sourceFingerprint"]
    assert set(request["properties"]) == {"sourceFingerprint"}
    assert {
        item["name"] for item in operations["post"]["parameters"] if item["in"] == "header"
    } == {
        "If-Match",
        "Idempotency-Key",
    }


def test_metadata_source_directory_defaults_to_unavailable_and_never_uses_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    missing = FileMetadataSourceDirectory(
        MetadataSourceSettings(environment="TEST", metadata_sources_path=None)
    )
    with pytest.raises(MetadataBlocked) as caught:
        missing.resolve("model-connection:trial")
    assert caught.value.reason is MetadataReason.SOURCE_UNCONFIGURED
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"schemaVersion": 1, "environment": "TEST", "sources": [SOURCE]}))
    directory = FileMetadataSourceDirectory(
        MetadataSourceSettings(environment="TEST", metadata_sources_path=path)
    )
    environment, source = directory.resolve("model-connection:trial")
    assert environment == "TEST" and source.purpose == "MODEL_METADATA_OBSERVATION"
    with pytest.raises(MetadataBlocked, match="SOURCE_NOT_APPROVED"):
        directory.resolve("model-connection:other")
    for declaration in [
        {"schemaVersion": True, "environment": "TEST", "sources": [SOURCE]},
        {"schemaVersion": 1, "environment": "OTHER", "sources": [SOURCE]},
        {"schemaVersion": 1, "environment": "TEST", "sources": [SOURCE, SOURCE]},
        {"schemaVersion": 1, "environment": "TEST", "sources": [SOURCE | {"purpose": "INFERENCE"}]},
        {
            "schemaVersion": 1,
            "environment": "TEST",
            "sources": [SOURCE | {"endpoint": "https://unapproved.invalid"}],
        },
    ]:
        path.write_text(json.dumps(declaration))
        with pytest.raises(MetadataBlocked):
            directory.resolve("model-connection:trial")
    path.write_bytes(b" " * 65537)
    with pytest.raises(MetadataBlocked, match="SOURCE_UNAVAILABLE"):
        directory.resolve("model-connection:trial")
