from typing import Any

import pytest
from pydantic import ValidationError

from control_plane.app.modules.model_gateway.api import dto


def request_type() -> Any:
    assert hasattr(dto, "CreateMaterialSourceCheckRequestDto"), "source check input is missing"
    return dto.CreateMaterialSourceCheckRequestDto


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"materialIndex": -1},
        {"materialIndex": 32},
        {"materialIndex": True},
        {"materialIndex": 1.0},
        {"materialIndex": "0"},
        {"materialIndex": 0, "relativePath": "copy"},
        {"materialIndex": 0, "result": "MATCHED"},
        {"materialIndex": 0, "observedSha256": "a" * 64},
        {"materialIndex": 0, "sourceReference": "https://example.org"},
    ],
)
def test_browser_only_selects_a_material_index(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request_type().model_validate(body)


def test_source_check_default_contract_has_three_typed_operations() -> None:
    from control_plane.app.bootstrap.app import create_app

    assert request_type().model_validate({"materialIndex": 0}).material_index == 0
    schema = create_app().openapi()
    base = (
        "/api/v1/admin/model-deployments/{deploymentId}"
        "/validation-dossiers/{dossierId}/source-checks"
    )
    assert base in schema["paths"]
    assert set(schema["paths"][base]) == {"post", "get"}
    assert schema["paths"][base]["post"]["operationId"] == "model_material_source_checks_create"
    assert "201" in schema["paths"][base]["post"]["responses"]
    assert schema["paths"][base]["get"]["operationId"] == "model_material_source_checks_list"
    assert (
        schema["paths"][base + "/{sourceCheckId}"]["get"]["operationId"]
        == "model_material_source_checks_get"
    )
    request = schema["components"]["schemas"]["CreateMaterialSourceCheckRequestDto"]
    assert request["additionalProperties"] is False
    assert set(request["properties"]) == {"materialIndex"}
    assert request["properties"]["materialIndex"]["maximum"] == 31
    assert schema["components"]["schemas"]["SourceCheckResult"]["enum"] == [
        "MATCHED",
        "MISMATCH",
        "BLOCKED",
    ]
