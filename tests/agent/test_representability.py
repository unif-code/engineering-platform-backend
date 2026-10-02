"""Public-bound inventory: immutable facts are rejected before they can break reads."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from control_plane.app.modules.agent import accept_workflow_event, register_definition
from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentRepository
from control_plane.app.modules.agent.api.dto import (
    AgentDefinitionResponseDto,
    AgentRunResponseDto,
    CanonicalEventSummaryResponseDto,
    CheckpointSummaryResponseDto,
)
from control_plane.app.modules.agent.application.definitions import RegisterDefinitionCommand
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AttemptMutation,
    CanonicalEventInput,
    CheckpointInput,
)
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_api import (
    AgentApiHarness,
    _advance_to_waiting,
    _start,
    _write_headers,
)
from tests.agent.test_api import (
    agent_api as agent_api,
)
from tests.agent.test_events import event, waiting_event
from tests.agent.test_final_integrity import facts
from tests.agent.test_repository import ATTEMPT, BINDING, CHECKPOINT, DEFINITION, EVENT, RUN

BOUNDED_FIELDS = [
    (DEFINITION, "name", 200, False, AgentDefinitionResponseDto),
    (DEFINITION, "capability_declarations", 200, True, AgentDefinitionResponseDto),
    (DEFINITION, "skill_declarations", 200, True, AgentDefinitionResponseDto),
    (DEFINITION, "runtime_permissions", 200, True, AgentDefinitionResponseDto),
    (RUN, "goal_ref", 2048, False, AgentRunResponseDto),
    (RUN, "created_by", 2048, False, AgentRunResponseDto),
    (CHECKPOINT, "artifact_id", 2048, False, CheckpointSummaryResponseDto),
    (CHECKPOINT, "artifact_version", 200, False, CheckpointSummaryResponseDto),
    (CHECKPOINT, "schema_version", 200, False, CheckpointSummaryResponseDto),
    (CHECKPOINT, "adapter_version", 200, False, CheckpointSummaryResponseDto),
    (CHECKPOINT, "classification", 200, False, CheckpointSummaryResponseDto),
    (EVENT, "correlation_id", 2048, False, CanonicalEventSummaryResponseDto),
    (EVENT, "causation_id", 2048, False, CanonicalEventSummaryResponseDto),
    (EVENT, "trace_id", 2048, False, CanonicalEventSummaryResponseDto),
    (EVENT, "span_id", 2048, False, CanonicalEventSummaryResponseDto),
    (EVENT, "summary", 10000, False, CanonicalEventSummaryResponseDto),
]


@pytest.mark.parametrize("model,field,limit,many,projection", BOUNDED_FIELDS)
@pytest.mark.parametrize("boundary", ["empty", "limit", "over"])
def test_every_bounded_projected_field_rejects_outside_public_contract(
    model: BaseModel,
    field: str,
    limit: int,
    many: bool,
    projection: Any,
    boundary: str,
) -> None:
    length = {"empty": 0, "limit": limit, "over": limit + 1}[boundary]
    value = "x" * length
    payload = model.model_dump(mode="python")
    payload[field] = [value] if many else value
    if boundary == "over" or (boundary == "empty" and field != "summary"):
        with pytest.raises(ValidationError):
            type(model).model_validate(payload)
    else:
        valid = type(model).model_validate(payload)
        projected = projection.from_domain(valid)
        assert getattr(projected, field) == ([value] if many else value)


@pytest.mark.parametrize(
    "model,fields",
    [
        (DEFINITION, ("id",)),
        (RUN, ("id", "workspace_id", "definition_id", "latest_attempt_id")),
        (ATTEMPT, ("id", "run_id", "binding_id")),
        (BINDING, ("id",)),
        (CHECKPOINT, ("id",)),
        (EVENT, ("id", "attempt_id")),
    ],
)
def test_all_projected_uuid_fields_reject_non_uuid(
    model: BaseModel, fields: tuple[str, ...]
) -> None:
    for field in fields:
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), field: "not-a-uuid"})


@pytest.mark.parametrize("field", ["requirementId", "workItemId", "assignmentId"])
def test_public_business_source_rejects_non_uuid_identities(field: str) -> None:
    value = AgentRunResponseDto.from_domain(RUN).model_dump(mode="json", by_alias=True)
    value["businessContext"][field] = "not-a-uuid"
    with pytest.raises(ValidationError):
        AgentRunResponseDto.model_validate(value)


@pytest.mark.parametrize(
    "model,fields",
    [
        (DEFINITION, ("version",)),
        (RUN, ("definition_version", "revision")),
        (ATTEMPT, ("number", "runner_generation", "revision")),
        (EVENT, ("generation", "sequence")),
    ],
)
def test_projected_positive_numbers_cannot_accept_zero(
    model: BaseModel, fields: tuple[str, ...]
) -> None:
    for field in fields:
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), field: 0})


@pytest.mark.parametrize("digest", ["a" * 64, "sha256:" + "a" * 63, "sha256:" + "A" * 64])
def test_checkpoint_digest_has_public_shape(digest: str) -> None:
    with pytest.raises(ValidationError):
        CheckpointInput.model_validate({**CHECKPOINT.model_dump(), "content_sha256": digest})


@pytest.mark.parametrize(
    "model,field,invalid,method",
    [
        (DEFINITION, "name", "x" * 201, "insert_definition"),
        (RUN, "goal_ref", "x" * 2049, "insert_run"),
        (EVENT, "correlation_id", "x" * 2049, "append_event"),
    ],
)
def test_repository_revalidates_bypassed_models_before_any_sql_fact(
    isolated_agent_database: IsolatedAgentDatabase,
    model: BaseModel,
    field: str,
    invalid: object,
    method: str,
) -> None:
    forged = type(model).model_construct(**{**dict(model), field: invalid})
    before = facts(isolated_agent_database)
    with isolated_agent_database.runtime.begin() as db:
        with pytest.raises(ValidationError):
            getattr(SqlAlchemyAgentRepository(db), method)(forged)
    assert facts(isolated_agent_database) == before


def test_repository_revalidates_nested_checkpoint_in_attempt_and_mutation(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    checkpoint = CheckpointInput.model_construct(
        **{
            **CHECKPOINT.model_dump(),
            "artifact_version": "x" * 201,
        }
    )
    attempt = AgentAttempt.model_construct(**{**ATTEMPT.model_dump(), "checkpoint": checkpoint})
    mutation = AttemptMutation.model_construct(
        state=ATTEMPT.state,
        runner_generation=1,
        fencing_token="fence",
        checkpoint=checkpoint,
        event_sequence=1,
        waiting_deadline=None,
        terminal_evidence=None,
        now=ATTEMPT.updated_at,
    )
    before = facts(isolated_agent_database)
    with isolated_agent_database.runtime.begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        with pytest.raises(ValidationError):
            repository.insert_attempt(attempt)
        with pytest.raises(ValidationError):
            repository.compare_and_set_attempt(ATTEMPT.id, expected_revision=1, mutation=mutation)
        with pytest.raises(ValidationError):
            repository.insert_checkpoint(ATTEMPT.id, checkpoint)
    assert facts(isolated_agent_database) == before


@pytest.mark.parametrize("kind", ["event", "checkpoint", "definition"])
def test_rejected_bypassed_ingress_cannot_break_http_reads_or_control(
    agent_api: AgentApiHarness,
    kind: str,
) -> None:
    agent = agent_api
    started = _start(agent).json()
    attempt_id = started["attempt"]["id"]
    run_id = started["run"]["id"]
    before = facts(agent.database)
    with pytest.raises(ValidationError):
        if kind == "definition":
            command = RegisterDefinitionCommand.model_construct(
                id=str(uuid4()),
                version=1,
                name="x" * 201,
                capability_declarations=(),
                skill_declarations=(),
                runtime_permissions=(),
                input_schema={},
                actor="employee-901",
                correlation_id="valid",
            )
            register_definition(None, command=command, dependencies=agent.runtime.dependencies)
        else:
            source = (
                waiting_event(attempt_id)
                if kind == "checkpoint"
                else event(
                    attempt_id, event_id=str(uuid4()), event_type="ATTEMPT_PROVISIONING", sequence=2
                )
            )
            payload = source.model_dump(mode="json")
            if kind == "checkpoint":
                payload["data"]["checkpoint"]["artifact_version"] = "x" * 201
            else:
                payload["correlation_id"] = "x" * 2049
            accept_workflow_event(
                None,
                event=CanonicalEventInput.model_construct(**payload),
                dependencies=agent.runtime.dependencies,
            )
    assert facts(agent.database) == before
    for path in (
        "/api/v1/agent-definitions",
        f"/api/v1/agent-runs/{run_id}",
        f"/api/v1/agent-runs/{run_id}/events",
    ):
        assert agent.client.get(path).status_code == 200
    revision = _advance_to_waiting(agent, attempt_id)
    response = agent.client.post(
        f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/resume",
        json={},
        headers=_write_headers("valid-resume", etag=f'"v{revision}"'),
    )
    assert response.status_code == 202, response.text
