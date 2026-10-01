import pytest
from pydantic import ValidationError

from control_plane.app.modules.model_gateway.api.dto import (
    CreateModelDeploymentRequestDto,
    PatchModelDeploymentRequestDto,
)

PAYLOAD = {
    "deploymentKey": "candidate-one",
    "displayName": "Candidate one",
    "providerKind": "BAILIAN_COMPATIBLE_MODE",
    "providerModelId": "unverified-model-id",
    "declaredCapabilities": ["chat", "coding"],
}


def test_candidate_declarations_are_strict_and_patch_preserves_omission() -> None:
    request = CreateModelDeploymentRequestDto.model_validate(PAYLOAD)
    assert request.connection_ref is None
    assert request.declared_context_window is None
    patch = PatchModelDeploymentRequestDto.model_validate({"connectionRef": None})
    assert patch.model_dump(exclude_unset=True) == {"connection_ref": None}
    for change in (
        {"apiKey": "sk-sensitive"},
        {"state": "ACTIVE"},
        {"connectionRef": "https://example.invalid"},
        {"connectionRef": "sk-sensitive"},
        {"declaredCapabilities": ["chat", "chat"]},
        {"declaredContextWindow": 0},
        {"declaredMaxOutputTokens": True},
        {"providerKind": "OTHER"},
    ):
        with pytest.raises(ValidationError):
            CreateModelDeploymentRequestDto.model_validate(PAYLOAD | change)
    for invalid_patch in ({}, {"deploymentKey": "new-key"}, {"displayName": None}):
        with pytest.raises(ValidationError):
            PatchModelDeploymentRequestDto.model_validate(invalid_patch)


def test_default_app_contract_exposes_only_catalog_commands() -> None:
    from control_plane.app.bootstrap.app import create_app

    schema = create_app().openapi()
    paths = {
        path: methods
        for path, methods in schema["paths"].items()
        if path
        in {
            "/api/v1/admin/model-deployments",
            "/api/v1/admin/model-deployments/{deploymentId}",
            "/api/v1/admin/model-deployments/{deploymentId}:archive",
        }
    }
    operations = {
        operation["operationId"] for methods in paths.values() for operation in methods.values()
    }
    assert operations == {
        "model_deployments_list",
        "model_deployments_get",
        "model_deployments_create",
        "model_deployments_patch",
        "model_deployments_archive",
    }
    for path, methods in paths.items():
        for method, operation in methods.items():
            headers = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "header"
            }
            if method in {"post", "patch"}:
                assert "Idempotency-Key" in headers
                if "{deploymentId}" in path:
                    assert "If-Match" in headers
            if method != "get" or "{deploymentId}" in path:
                status = "201" if operation["operationId"] == "model_deployments_create" else "200"
                assert "ETag" in operation["responses"][status]["headers"]
    patch = schema["components"]["schemas"]["PatchModelDeploymentRequestDto"]
    assert patch["additionalProperties"] is False
    assert "deploymentKey" not in patch["properties"]
    assert patch["properties"]["displayName"]["type"] == "string"
    assert {entry.get("type") for entry in patch["properties"]["connectionRef"]["anyOf"]} == {
        "string",
        "null",
    }
