from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.model_gateway.domain.connections import (
    CheckKind,
    ConnectionDefinition,
)
from control_plane.app.modules.model_gateway.domain.dossiers import CreateValidationDossier
from tests.model_gateway.test_probe import CONNECTION


def connection(**changes: object) -> ConnectionDefinition:
    return ConnectionDefinition.model_validate(
        CONNECTION
        | {
            "allowedModelIds": ["synthetic-model", "synthetic-other"],
            "responsesSearchModelIds": ["synthetic-model"],
        }
        | changes
    )


def test_tool_purpose_is_empty_by_default_and_not_inherited_from_search() -> None:
    value = connection()
    assert value.tool_call_model_ids == ()
    assert value.responses_search_model_ids == ("synthetic-model",)


@pytest.mark.parametrize(
    "models",
    [["unapproved-model"], ["synthetic-model", "synthetic-model"], None, "synthetic-model"],
)
def test_tool_purpose_requires_a_unique_explicit_subset(models: object) -> None:
    with pytest.raises(ValidationError):
        connection(toolCallModelIds=models)


@pytest.mark.parametrize(
    "models", [[], ["synthetic-model"], ["synthetic-other"], ["synthetic-other", "synthetic-model"]]
)
def test_unrelated_new_purpose_values_never_change_old_47a_fingerprints(models: list[str]) -> None:
    value = connection(toolCallModelIds=models)
    # Captured from fixed47a's real ConnectionDefinition with the same original input.
    old = {
        CheckKind.BASIC_TEXT: "7af30476b63efc303425f7796a385dfbf4eb2dd2991f27a706ef4b62a728b552",
        CheckKind.STREAM_TEXT: "7af30476b63efc303425f7796a385dfbf4eb2dd2991f27a706ef4b62a728b552",
        CheckKind.STREAM_STOP: "7af30476b63efc303425f7796a385dfbf4eb2dd2991f27a706ef4b62a728b552",
        CheckKind.THINKING: "7af30476b63efc303425f7796a385dfbf4eb2dd2991f27a706ef4b62a728b552",
        CheckKind.SEARCH_SOURCES: (
            "bc24f0652b9143a5d7e9f894ddfc718239937d2b24332e79aa7ca0f3c1fc9d12"
        ),
    }
    assert {kind: value.fingerprint_for(kind) for kind in old} == old


def test_tool_fingerprint_binds_its_approval_but_not_list_order() -> None:
    empty = connection()
    full = connection(toolCallModelIds=["synthetic-model", "synthetic-other"])
    reordered = connection(toolCallModelIds=["synthetic-other", "synthetic-model"])
    assert empty.fingerprint_for(CheckKind.TOOL_CALL) != full.fingerprint_for(CheckKind.TOOL_CALL)
    assert full.fingerprint_for(CheckKind.TOOL_CALL) == reordered.fingerprint_for(
        CheckKind.TOOL_CALL
    )
    changed = full.model_copy(update={"version": "config-2"})
    for kind in CheckKind:
        assert full.fingerprint_for(kind) != changed.fingerprint_for(kind)


def test_public_tool_observation_does_not_invent_text_or_execution_facts() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    assert "TOOL_CALL" in schemas["CheckKind"]["enum"]
    assert {"TOOL_CALL_SIGNAL_MISSING", "TOOL_CALL_MODEL_NOT_ALLOWED"} <= set(
        schemas["CheckReason"]["enum"]
    )
    schema = schemas["ToolCallObservationDto"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "kind",
        "consumedBytes",
        "matchingCallObserved",
        "toolCallCompletionObserved",
        "localResponseClosed",
    }
    assert set(schema["required"]) == set(schema["properties"])
    request = schemas["CreateConnectionCheckRequestDto"]
    assert set(request["properties"]) == {"checkKind"}
    assert request["additionalProperties"] is False
    assert schemas["CreateValidationDossierRequestDto"]["properties"]["checkIds"]["maxItems"] == 6
    assert schemas["ValidationDossierSnapshotDto"]["properties"]["checks"]["maxItems"] == 6
    coverage = schemas["ValidationDossierDetailDto"]["properties"]["checkCoverage"]
    assert coverage["minItems"] == coverage["maxItems"] == 6


def test_six_distinct_dossier_references_fit_but_seven_and_duplicates_do_not() -> None:
    values = {"check_ids": [str(uuid4()) for _ in range(6)]}
    assert len(CreateValidationDossier.model_validate(values).check_ids) == 6
    for extra in [str(uuid4()), values["check_ids"][0]]:
        wrong = deepcopy(values)
        wrong["check_ids"].append(extra)
        with pytest.raises(ValidationError):
            CreateValidationDossier.model_validate(wrong)
