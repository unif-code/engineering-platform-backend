"""Platform facts, not vendor payloads, cross the Agent boundaries."""

from __future__ import annotations

import json
from typing import get_args, get_origin, get_type_hints

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from control_plane.app import __version__
from control_plane.app.bootstrap.app import create_app
from control_plane.app.modules.agent import accept_workflow_event
from control_plane.app.modules.agent.domain import AgentAuditAppend, CanonicalEventInput
from control_plane.app.modules.agent.domain import models as domain_models
from tests.agent.conftest import IsolatedAgentDatabase
from tests.agent.test_events import advance_to_running, dependencies, event, start, waiting_event


def test_domain_dto_annotations_are_platform_or_standard_library_types() -> None:
    allowed_modules = {
        "builtins",
        "collections.abc",
        "datetime",
        "typing",
        "types",
        "control_plane.app.modules.agent.domain.models",
    }

    def check(annotation: object) -> None:
        origin = get_origin(annotation)
        declared = origin or annotation
        assert getattr(declared, "__module__", "builtins") in allowed_modules
        for argument in get_args(annotation):
            if argument is not Ellipsis:
                check(argument)

    checked: set[str] = set()
    for value in vars(domain_models).values():
        if (
            isinstance(value, type)
            and issubclass(value, BaseModel)
            and value.__module__ == domain_models.__name__
        ):
            annotations = get_type_hints(value)
            for name in value.model_fields:
                check(annotations[name])
            checked.add(value.__name__)
    assert {
        "AgentRun",
        "AgentAttempt",
        "ExecutionBinding",
        "CheckpointInput",
        "CanonicalEventInput",
        "WorkflowCommand",
        "AgentAuditAppend",
    } <= checked


def test_v08_openapi_artifact_is_versioned_and_exposes_only_public_platform_fields() -> None:
    schema = create_app().openapi()
    assert schema["info"]["version"] == __version__
    schemas = schema["components"]["schemas"]
    expected = {
        "AgentAttemptResponseDto": (
            "id runId number state bindingId runnerGeneration checkpoint "
            "waitingDeadline revision createdAt updatedAt"
        ),
        "ExecutionBindingSummaryResponseDto": "id source digest",
        "CheckpointSummaryResponseDto": (
            "id artifactId artifactVersion contentSha256 schemaVersion "
            "adapterVersion classification"
        ),
        "CanonicalEventSummaryResponseDto": (
            "id eventType attemptId generation sequence correlationId "
            "causationId traceId spanId summary"
        ),
    }
    for name, fields in expected.items():
        assert set(schemas[name]["properties"]) == set(fields.split())
        assert schemas[name]["additionalProperties"] is False
    assert set(AgentAuditAppend.model_fields) == set(
        (
            "id occurred_at actor actor_type action target_type target_id "
            "result reason correlation_id schema_version"
        ).split()
    )
    assert set(CanonicalEventInput.model_fields) == set(
        (
            "id event_type attempt_id generation sequence correlation_id "
            "causation_id trace_id span_id summary data"
        ).split()
    )


@pytest.mark.parametrize(
    "data",
    [
        {"password": "not-a-real-secret"},
        {"prompt": "source text"},
        {"temporalHistory": []},
        {"exception": {"traceback": "private"}},
        {"platform": {"sdkState": {}}},
    ],
)
def test_lifecycle_data_rejects_nonplatform_payloads(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        event(
            "00000000-0000-0000-0000-000000000003",
            event_id="00000000-0000-0000-0000-000000000006",
            event_type="ATTEMPT_RUNNING",
            sequence=1,
            data=data,
        )


def test_waiting_data_rejects_extra_nested_checkpoint_payload() -> None:
    values = waiting_event("00000000-0000-0000-0000-000000000003").model_dump(mode="json")
    values["data"]["checkpoint"]["sdkState"] = {"credential": "not-a-real-secret"}
    with pytest.raises(ValidationError):
        CanonicalEventInput.model_validate(values)


def test_canonical_summary_cannot_exceed_the_http_summary_contract() -> None:
    incoming = event(
        "00000000-0000-0000-0000-000000000003",
        event_id="00000000-0000-0000-0000-000000000006",
        event_type="ATTEMPT_RUNNING",
        sequence=1,
    )
    with pytest.raises(ValidationError):
        incoming.model_copy(update={"summary": "a" * 10001})


@pytest.mark.parametrize(
    "kind,data",
    [
        ("ATTEMPT_QUEUED", {}),
        ("ATTEMPT_QUEUED", {"bindingSource": "TEMPORAL", "bindingDigest": "a" * 64}),
        ("WAITING_INPUT", {"checkpoint": {}, "waitingDeadline": "unknown"}),
    ],
)
def test_event_data_requires_the_exact_platform_contract(
    kind: str, data: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        event(
            "00000000-0000-0000-0000-000000000003",
            event_id="00000000-0000-0000-0000-000000000006",
            event_type=kind,
            sequence=1,
            data=data,
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "field,value",
    [
        ("data", {"temporalHistory": {"credential": "not-a-real-secret"}}),
        ("summary", "a" * 10001),
    ],
)
def test_construct_bypass_cannot_persist_worker_payload(
    isolated_agent_database: IsolatedAgentDatabase,
    field: str,
    value: object,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    values = event(
        started.attempt.id,
        event_id="10000000-0000-0000-0000-000000001391",
        event_type="ATTEMPT_PROVISIONING",
        sequence=2,
    ).model_dump()
    forged = CanonicalEventInput.model_construct(**values)
    object.__setattr__(forged, field, value)
    with pytest.raises(ValidationError):
        accept_workflow_event(None, event=forged, dependencies=deps)
    with isolated_agent_database.owner.connect() as db:
        assert db.execute(text("SELECT state, event_sequence FROM agent.agent_attempt")).one() == (
            "QUEUED",
            1,
        )
        assert db.execute(text("SELECT count(*) FROM agent.canonical_event")).scalar_one() == 1
        assert (
            db.execute(
                text("SELECT count(*) FROM audit.audit_event WHERE action LIKE 'agent.event.%'")
            ).scalar_one()
            == 0
        )


@pytest.mark.integration
def test_database_columns_are_exact_platform_facts(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    expected = {
        "agent_definition": (
            "id version name capability_declarations skill_declarations "
            "runtime_permissions input_schema created_at"
        ),
        "agent_run": (
            "id workspace_id goal_ref created_by definition_id definition_version "
            "latest_attempt_id state revision created_at updated_at"
        ),
        "agent_attempt": (
            "id run_id number state binding_id binding_digest runner_generation "
            "fencing_token checkpoint_id event_sequence waiting_deadline "
            "terminal_evidence revision created_at updated_at"
        ),
        "execution_binding": "id attempt_id source digest snapshot created_at",
        "event_acceptance_receipt": "event_id schema_version attempt checkpoint",
        "canonical_event": (
            "event_id event_type attempt_id runner_generation sequence correlation_id "
            "causation_id trace_id span_id summary data payload_digest created_at"
        ),
        "checkpoint": (
            "id attempt_id artifact_id artifact_version content_sha256 schema_version "
            "adapter_version classification created_at"
        ),
        "workflow_command": (
            "id command_key kind attempt_id generation state dispatch_attempts "
            "receipt last_error_code created_at updated_at dispatched_at "
            "claim_owner claim_token claim_lease_until claim_mode"
        ),
        "idempotency_key": (
            "id actor operation key request_fingerprint state http_status "
            "result_metadata sealed_response created_at updated_at completed_at"
        ),
    }
    with isolated_agent_database.owner.connect() as db:
        columns: dict[str, set[str]] = {}
        for table, column in db.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='agent'"
            )
        ):
            columns.setdefault(table, set()).add(column)
    assert columns == {table: set(fields.split()) for table, fields in expected.items()}


@pytest.mark.integration
def test_waiting_audit_is_digest_only_not_checkpoint_or_event_body(
    isolated_agent_database: IsolatedAgentDatabase,
) -> None:
    deps = dependencies(isolated_agent_database)
    started = start(deps)
    advance_to_running(deps, started.attempt.id)
    incoming = waiting_event(started.attempt.id).model_copy(
        update={
            "summary": "private-summary-sentinel",
            "correlation_id": "private-correlation-sentinel",
        }
    )
    accept_workflow_event(None, event=incoming, dependencies=deps)
    with isolated_agent_database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT to_jsonb(e) FROM audit.audit_event e "
                "WHERE action='agent.attempt.waiting_input'"
            )
        ).scalar_one()
    assert set(row) == set(
        (
            "id occurred_at actor actor_type action target_type target_id "
            "result reason correlation_id schema_version request_id"
        ).split()
    )
    serialized = json.dumps(row)
    for private in (
        incoming.summary,
        incoming.correlation_id,
        incoming.id,
        "artifact-901",
        "checkpoint",
        "data",
    ):
        assert private not in serialized
    assert row["target_id"] == started.attempt.id
    assert row["result"] == "WAITING_INPUT"
