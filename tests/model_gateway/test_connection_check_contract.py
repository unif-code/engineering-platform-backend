import pytest
from pydantic import ValidationError

from control_plane.app.modules.model_gateway.api.dto import CreateConnectionCheckRequestDto
from control_plane.app.modules.model_gateway.domain.connections import ConnectionDefinition


def test_connection_check_has_no_browser_controlled_probe_or_target() -> None:
    assert CreateConnectionCheckRequestDto.model_validate({}).model_dump() == {}
    for body in ({"prompt": "custom"}, {"endpoint": "https://example.invalid"}, {"revision": 1}):
        with pytest.raises(ValidationError):
            CreateConnectionCheckRequestDto.model_validate(body)


def test_connection_manifest_derives_only_approved_workspace_https_target() -> None:
    value = {
        "reference": "model-connection:trial",
        "version": "config-1",
        "materialVersion": "material-1",
        "providerKind": "BAILIAN_COMPATIBLE_MODE",
        "region": "cn-beijing",
        "workspaceId": "test-workspace",
        "allowedModelIds": ["unverified-model"],
        "secretRef": "secret-ref:trial-key",
    }
    connection = ConnectionDefinition.model_validate(value)
    assert connection.hostname == "test-workspace.cn-beijing.maas.aliyuncs.com"
    for change in (
        {"region": "private"},
        {"workspaceId": "127.0.0.1"},
        {"endpoint": "http://127.0.0.1"},
        {"materialVersion": ""},
    ):
        with pytest.raises(ValidationError):
            ConnectionDefinition.model_validate(value | change)


def test_default_openapi_separates_candidate_write_version_and_check_identity() -> None:
    from control_plane.app.bootstrap.app import create_app

    schema = create_app().openapi()
    base = "/api/v1/admin/model-deployments/{deploymentId}/connection-checks"
    operations = schema["paths"][base]
    assert operations["post"]["operationId"] == "model_connection_checks_create"
    assert "202" in operations["post"]["responses"]
    assert operations["get"]["operationId"] == "model_connection_checks_list"
    assert (
        schema["paths"][base + "/{checkId}"]["get"]["operationId"] == "model_connection_checks_get"
    )
    assert {
        value["name"] for value in operations["post"]["parameters"] if value["in"] == "header"
    } == {"If-Match", "Idempotency-Key"}
    body = schema["components"]["schemas"]["CreateConnectionCheckRequestDto"]
    assert body["additionalProperties"] is False and body["properties"] == {}
    fields = schema["components"]["schemas"]["ConnectionCheckDto"]["properties"]
    assert {"revision", "input", "currentness", "currentnessReasons", "usage"} <= fields.keys()
    assert not {"executionToken", "secretRef", "value", "prompt", "responseBody"} & fields.keys()
