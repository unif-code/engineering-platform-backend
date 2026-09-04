from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.app.modules.agent.domain import (
    ALLOWED_TRANSITIONS,
    AgentAttempt,
    AgentDefinition,
    AgentDomainError,
    AttemptNotResumable,
    AttemptState,
    CanonicalEventInput,
    CheckpointInput,
    CheckpointReplacementForbidden,
    CheckpointRequired,
    ExecutionBinding,
    ExecutionBindingSource,
    IdempotencyCompletion,
    IllegalAttemptTransition,
    InvalidFencingToken,
    RepositoryWriteForbidden,
    ResumeGenerationRequired,
    WorkflowCommandState,
    WorkflowDispatchMutation,
    resume_generation,
    transition_attempt,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
CHECKPOINT = CheckpointInput(
    id="00000000-0000-0000-0000-000000000001",
    artifact_id="artifact-1",
    artifact_version="1",
    content_sha256="sha256:" + "a" * 64,
    schema_version="v1",
    adapter_version="dev-v1",
    classification="INTERNAL",
)

EXPECTED_TRANSITIONS = {
    AttemptState.CREATED: {AttemptState.BINDING, AttemptState.CANCELING},
    AttemptState.BINDING: {
        AttemptState.QUEUED,
        AttemptState.FAILED,
        AttemptState.CANCELING,
    },
    AttemptState.QUEUED: {AttemptState.PROVISIONING, AttemptState.CANCELING},
    AttemptState.PROVISIONING: {
        AttemptState.RUNNING,
        AttemptState.FAILED,
        AttemptState.CANCELING,
    },
    AttemptState.RUNNING: {
        AttemptState.WAITING_INPUT,
        AttemptState.FINALIZING,
        AttemptState.CANCELING,
    },
    AttemptState.WAITING_INPUT: {AttemptState.QUEUED, AttemptState.CANCELING},
    AttemptState.FINALIZING: {AttemptState.SUCCEEDED, AttemptState.FAILED},
    AttemptState.CANCELING: {AttemptState.CANCELED, AttemptState.TIMED_OUT},
    AttemptState.SUCCEEDED: set(),
    AttemptState.FAILED: set(),
    AttemptState.CANCELED: set(),
    AttemptState.TIMED_OUT: set(),
}

APPROVED_TRANSITION_PAIRS = (
    (AttemptState.CREATED, AttemptState.BINDING),
    (AttemptState.CREATED, AttemptState.CANCELING),
    (AttemptState.BINDING, AttemptState.QUEUED),
    (AttemptState.BINDING, AttemptState.FAILED),
    (AttemptState.BINDING, AttemptState.CANCELING),
    (AttemptState.QUEUED, AttemptState.PROVISIONING),
    (AttemptState.QUEUED, AttemptState.CANCELING),
    (AttemptState.PROVISIONING, AttemptState.RUNNING),
    (AttemptState.PROVISIONING, AttemptState.FAILED),
    (AttemptState.PROVISIONING, AttemptState.CANCELING),
    (AttemptState.RUNNING, AttemptState.WAITING_INPUT),
    (AttemptState.RUNNING, AttemptState.FINALIZING),
    (AttemptState.RUNNING, AttemptState.CANCELING),
    (AttemptState.WAITING_INPUT, AttemptState.QUEUED),
    (AttemptState.WAITING_INPUT, AttemptState.CANCELING),
    (AttemptState.FINALIZING, AttemptState.SUCCEEDED),
    (AttemptState.FINALIZING, AttemptState.FAILED),
    (AttemptState.CANCELING, AttemptState.CANCELED),
    (AttemptState.CANCELING, AttemptState.TIMED_OUT),
)

GENERIC_TRANSITION_PAIRS = tuple(
    pair
    for pair in APPROVED_TRANSITION_PAIRS
    if pair != (AttemptState.WAITING_INPUT, AttemptState.QUEUED)
)


def execution_binding(
    *,
    runtime_permissions: tuple[str, ...] = ("context.read",),
    skill_refs: tuple[str, ...] = ("skill:plan",),
    source: ExecutionBindingSource = ExecutionBindingSource.DEV_FAKE,
) -> ExecutionBinding:
    return ExecutionBinding(
        id="00000000-0000-0000-0000-000000000002",
        source=source,
        runtime_ref="DEV_FAKE:runtime:v1",
        model_route_ref="DEV_FAKE:model-route:v1",
        capability_bundle_ref="DEV_FAKE:capability-bundle:v1",
        skill_refs=skill_refs,
        runtime_permissions=runtime_permissions,
        context_policy_ref="DEV_FAKE:context-policy:v1",
        network_policy_ref="DEV_FAKE:network-policy:v1",
    )


def attempt(state: AttemptState = AttemptState.RUNNING) -> AgentAttempt:
    binding = execution_binding()
    return AgentAttempt(
        id="00000000-0000-0000-0000-000000000003",
        run_id="00000000-0000-0000-0000-000000000004",
        number=1,
        state=state,
        binding_id=binding.id,
        binding_digest=binding.digest,
        runner_generation=1,
        fencing_token="fence-1",
        checkpoint=CHECKPOINT if state is AttemptState.WAITING_INPUT else None,
        revision=4,
        created_at=NOW,
        updated_at=NOW,
    )


def test_transition_table_matches_the_approved_state_machine() -> None:
    assert ALLOWED_TRANSITIONS == EXPECTED_TRANSITIONS


@pytest.mark.parametrize(("source", "target"), GENERIC_TRANSITION_PAIRS)
def test_transition_attempt_allows_each_supported_generic_transition(
    source: AttemptState,
    target: AttemptState,
) -> None:
    transitioned = transition_attempt(
        attempt(source),
        target,
        checkpoint=CHECKPOINT if target is AttemptState.WAITING_INPUT else None,
        now=NOW,
    )

    assert transitioned.state is target
    assert transitioned.updated_at == NOW


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in AttemptState
        for target in AttemptState
        if target not in EXPECTED_TRANSITIONS[source]
    ],
)
def test_transition_attempt_rejects_the_complementary_illegal_matrix(
    source: AttemptState,
    target: AttemptState,
) -> None:
    with pytest.raises(IllegalAttemptTransition, match=f"{source.value}->{target.value}"):
        transition_attempt(attempt(source), target, checkpoint=None, now=NOW)


def test_waiting_input_requires_checkpoint_and_resume_keeps_binding() -> None:
    running = attempt()

    with pytest.raises(CheckpointRequired):
        transition_attempt(running, AttemptState.WAITING_INPUT, checkpoint=None, now=NOW)

    waiting = transition_attempt(
        running,
        AttemptState.WAITING_INPUT,
        checkpoint=CHECKPOINT,
        now=NOW,
    )
    resumed = resume_generation(waiting, fencing_token="fence-2", now=NOW)

    assert resumed.binding_id == "00000000-0000-0000-0000-000000000002"
    assert resumed.binding_digest == running.binding_digest
    assert resumed.runner_generation == 2
    assert resumed.fencing_token == "fence-2"
    assert resumed.state is AttemptState.QUEUED
    assert resumed.checkpoint == CHECKPOINT


def test_generic_transition_cannot_bypass_resume_generation() -> None:
    waiting = attempt(AttemptState.WAITING_INPUT)

    with pytest.raises(ResumeGenerationRequired):
        transition_attempt(waiting, AttemptState.QUEUED, checkpoint=None, now=NOW)


def test_terminal_attempt_cannot_resume() -> None:
    terminal = attempt(AttemptState.SUCCEEDED)

    with pytest.raises(AttemptNotResumable):
        resume_generation(terminal, fencing_token="fence-2", now=NOW)


@pytest.mark.parametrize("fencing_token", ("", "  ", "fence-1"))
def test_resume_generation_rejects_blank_or_unchanged_fencing_token(
    fencing_token: str,
) -> None:
    waiting = transition_attempt(
        attempt(),
        AttemptState.WAITING_INPUT,
        checkpoint=CHECKPOINT,
        now=NOW,
    )

    with pytest.raises(InvalidFencingToken):
        resume_generation(waiting, fencing_token=fencing_token, now=NOW)


@pytest.mark.parametrize(
    "permission",
    ("repository.write", "git.push", "source_control.write", "merge"),
)
def test_binding_rejects_repository_write_permissions(permission: str) -> None:
    with pytest.raises(RepositoryWriteForbidden):
        execution_binding(runtime_permissions=("context.read", permission))


def test_binding_digest_is_canonical_and_binding_is_frozen() -> None:
    first = execution_binding(skill_refs=("skill:plan", "skill:review"))
    second = execution_binding(skill_refs=("skill:plan", "skill:review"))
    changed = execution_binding(skill_refs=("skill:review", "skill:plan"))
    changed_source = execution_binding(source=ExecutionBindingSource.CONFIGURATION)

    assert first.digest == second.digest
    assert first.digest != changed.digest
    assert first.digest != changed_source.digest
    with pytest.raises(ValidationError):
        first.runtime_ref = "DEV_FAKE:runtime:v2"


def test_binding_copy_revalidates_permissions_and_recomputes_digest() -> None:
    binding = execution_binding()

    with pytest.raises(RepositoryWriteForbidden):
        binding.model_copy(update={"runtime_permissions": ("context.read", "git.push")})

    changed = binding.model_copy(update={"runtime_ref": "DEV_FAKE:runtime:v2"})

    assert changed.digest != binding.digest


def test_waiting_input_cannot_exist_without_a_checkpoint() -> None:
    original = attempt()
    payload = original.model_dump()
    payload.update({"state": AttemptState.WAITING_INPUT, "checkpoint": None})

    with pytest.raises(ValidationError):
        AgentAttempt.model_validate(payload)


def test_checkpoint_required_remains_an_agent_domain_error() -> None:
    assert issubclass(CheckpointRequired, AgentDomainError)


def test_transition_rejects_checkpoint_replacement_outside_waiting_input() -> None:
    with pytest.raises(CheckpointReplacementForbidden):
        transition_attempt(
            attempt(),
            AttemptState.FINALIZING,
            checkpoint=CHECKPOINT,
            now=NOW,
        )


def test_transition_preserves_checkpoint_after_waiting_input() -> None:
    waiting = transition_attempt(
        attempt(),
        AttemptState.WAITING_INPUT,
        checkpoint=CHECKPOINT,
        now=NOW,
    )

    canceled = transition_attempt(
        waiting,
        AttemptState.CANCELING,
        checkpoint=None,
        now=NOW,
    )

    assert canceled.checkpoint == CHECKPOINT


def test_platform_json_evidence_is_deeply_immutable_and_json_serializable() -> None:
    definition = AgentDefinition(
        id="00000000-0000-0000-0000-000000000005",
        version=1,
        name="Agent",
        capability_declarations=("agent.run.execute",),
        skill_declarations=("skill:plan",),
        runtime_permissions=("context.read",),
        input_schema={"nested": {"items": ["one"]}},
        created_at=NOW,
    )
    event = CanonicalEventInput(
        id="00000000-0000-0000-0000-000000000006",
        event_type="WAITING_INPUT",
        attempt_id="00000000-0000-0000-0000-000000000003",
        generation=1,
        sequence=1,
        correlation_id="correlation-1",
        causation_id=None,
        trace_id="trace-1",
        span_id="span-1",
        summary="running",
        data={"checkpoint": CHECKPOINT.model_dump(), "waitingDeadline": NOW.isoformat()},
    )

    with pytest.raises(TypeError):
        definition.input_schema["nested"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        event.data["checkpoint"] = "changed"  # type: ignore[index]
    nested_schema = definition.input_schema["nested"]
    nested_event_data = event.data["checkpoint"]
    assert isinstance(nested_schema, Mapping)
    assert isinstance(nested_event_data, Mapping)
    with pytest.raises(TypeError):
        nested_schema["items"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        nested_event_data["artifact_id"] = "changed"  # type: ignore[index]
    assert definition.model_dump(mode="json")["input_schema"] == {"nested": {"items": ["one"]}}
    assert event.model_dump(mode="json")["data"]["checkpoint"]["artifact_id"] == "artifact-1"


def test_repository_mutation_dtos_deep_freeze_json_inputs() -> None:
    completion = IdempotencyCompletion(
        http_status=202,
        result_metadata={"run": {"id": "00000000-0000-0000-0000-000000000004"}},
        sealed_response=b"sealed",
        now=NOW,
    )
    dispatch = WorkflowDispatchMutation(
        state=WorkflowCommandState.DISPATCHED,
        receipt={"provider": {"receipt": "receipt-1"}},
        error_code=None,
        now=NOW,
    )

    with pytest.raises(TypeError):
        completion.result_metadata["run"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        dispatch.receipt["provider"] = "changed"  # type: ignore[index]
    nested_metadata = completion.result_metadata["run"]
    assert dispatch.receipt is not None
    nested_receipt = dispatch.receipt["provider"]
    assert isinstance(nested_metadata, Mapping)
    assert isinstance(nested_receipt, Mapping)
    with pytest.raises(TypeError):
        nested_metadata["id"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        nested_receipt["receipt"] = "changed"  # type: ignore[index]


def test_canonical_event_sequence_starts_at_one() -> None:
    with pytest.raises(ValidationError):
        CanonicalEventInput(
            id="00000000-0000-0000-0000-000000000006",
            event_type="ATTEMPT_RUNNING",
            attempt_id="00000000-0000-0000-0000-000000000003",
            generation=1,
            sequence=0,
            correlation_id="correlation-1",
            causation_id=None,
            trace_id="trace-1",
            span_id="span-1",
            summary="running",
            data={},
        )


@pytest.mark.parametrize(
    "non_finite",
    (float("nan"), float("inf"), float("-inf")),
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_platform_json_evidence_rejects_non_finite_floats(non_finite: float) -> None:
    with pytest.raises(ValidationError):
        AgentDefinition(
            id="00000000-0000-0000-0000-000000000005",
            version=1,
            name="Agent",
            capability_declarations=("agent.run.execute",),
            skill_declarations=("skill:plan",),
            runtime_permissions=("context.read",),
            input_schema={"value": non_finite},
            created_at=NOW,
        )
    with pytest.raises(ValidationError):
        CanonicalEventInput(
            id="00000000-0000-0000-0000-000000000006",
            event_type="ATTEMPT_RUNNING",
            attempt_id="00000000-0000-0000-0000-000000000003",
            generation=1,
            sequence=1,
            correlation_id="correlation-1",
            causation_id=None,
            trace_id="trace-1",
            span_id="span-1",
            summary="running",
            data={"value": non_finite},
        )
