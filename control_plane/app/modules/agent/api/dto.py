from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from pydantic import ConfigDict, Field

from control_plane.app.modules.agent.application.control import AttemptControlResult
from control_plane.app.modules.agent.application.errors import AgentBusinessContextReason
from control_plane.app.modules.agent.application.queries import (
    AgentBusinessContextCurrentness,
    AgentRunBusinessContextStatus,
    AgentRunPage,
    AgentRunView,
    CanonicalEventPage,
)
from control_plane.app.modules.agent.application.runs import StartRunResult
from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentDefinition,
    AgentRun,
    AgentRunListItem,
    AttemptState,
    CanonicalEventInput,
    CheckpointInput,
    ExecutionBinding,
    ExecutionBindingSource,
    RunState,
)
from control_plane.app.shared.api.camel import CamelModel

PublicReference = Annotated[str, Field(min_length=1, max_length=2048)]
PublicName = Annotated[str, Field(min_length=1, max_length=200)]
PositiveInteger = Annotated[int, Field(ge=1)]
ContentDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class StrictCamelModel(CamelModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=False,
        validate_by_alias=True,
        validate_by_name=False,
    )


class StrictResponseCamelModel(CamelModel):
    model_config = ConfigDict(extra="forbid")


class StartAgentRunRequestDto(StrictCamelModel):
    workspace_id: UUID
    requirement_id: UUID
    work_item_id: UUID
    definition_id: UUID
    definition_version: int = Field(strict=True, ge=1)
    goal: str = Field(min_length=1, max_length=10_000)


class AttemptControlRequestDto(StrictCamelModel):
    """Control has no client-supplied actor or command payload."""


class AgentDefinitionResponseDto(StrictResponseCamelModel):
    id: UUID
    version: PositiveInteger
    name: PublicName
    capability_declarations: list[PublicName]
    skill_declarations: list[PublicName]
    runtime_permissions: list[PublicName]
    input_schema: dict[str, object]
    created_at: datetime

    @classmethod
    def from_domain(cls, definition: AgentDefinition) -> "AgentDefinitionResponseDto":
        serialized = definition.model_dump(mode="json")
        return cls(
            id=UUID(definition.id),
            version=definition.version,
            name=definition.name,
            capability_declarations=list(definition.capability_declarations),
            skill_declarations=list(definition.skill_declarations),
            runtime_permissions=list(definition.runtime_permissions),
            input_schema=cast(dict[str, object], serialized["input_schema"]),
            created_at=definition.created_at,
        )


class AgentDefinitionListResponseDto(StrictResponseCamelModel):
    items: list[AgentDefinitionResponseDto]

    @classmethod
    def from_domain(
        cls, definitions: tuple[AgentDefinition, ...]
    ) -> "AgentDefinitionListResponseDto":
        return cls(items=[AgentDefinitionResponseDto.from_domain(item) for item in definitions])


class AgentRunBusinessContextResponseDto(StrictResponseCamelModel):
    requirement_id: UUID
    work_item_id: UUID
    assignment_id: UUID


class AgentRunBusinessContextStatusResponseDto(StrictResponseCamelModel):
    run_id: UUID
    workspace_id: UUID
    checked_at: datetime
    currentness: AgentBusinessContextCurrentness
    reasons: list[AgentBusinessContextReason]

    @classmethod
    def from_domain(
        cls, value: AgentRunBusinessContextStatus
    ) -> "AgentRunBusinessContextStatusResponseDto":
        return cls(
            run_id=UUID(value.run_id),
            workspace_id=UUID(value.workspace_id),
            checked_at=value.checked_at,
            currentness=value.currentness,
            reasons=list(value.reasons),
        )


class AgentRunResponseDto(StrictResponseCamelModel):
    id: UUID
    workspace_id: UUID
    business_context: AgentRunBusinessContextResponseDto | None
    goal_ref: PublicReference
    created_by: PublicReference
    definition_id: UUID
    definition_version: PositiveInteger
    latest_attempt_id: UUID
    state: RunState
    revision: PositiveInteger
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, run: AgentRun) -> "AgentRunResponseDto":
        source = run.business_context
        return cls(
            id=UUID(run.id),
            workspace_id=UUID(run.workspace_id),
            business_context=(
                AgentRunBusinessContextResponseDto(
                    requirement_id=UUID(source.requirement_id),
                    work_item_id=UUID(source.work_item_id),
                    assignment_id=UUID(source.assignment_id),
                )
                if source is not None
                else None
            ),
            goal_ref=run.goal_ref,
            created_by=run.created_by,
            definition_id=UUID(run.definition_id),
            definition_version=run.definition_version,
            latest_attempt_id=UUID(run.latest_attempt_id),
            state=run.state,
            revision=run.revision,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


class CheckpointSummaryResponseDto(StrictResponseCamelModel):
    id: UUID
    artifact_id: PublicReference
    artifact_version: PublicName
    content_sha256: ContentDigest
    schema_version: PublicName
    adapter_version: PublicName
    classification: PublicName

    @classmethod
    def from_domain(cls, checkpoint: CheckpointInput) -> "CheckpointSummaryResponseDto":
        return cls(
            id=UUID(checkpoint.id),
            artifact_id=checkpoint.artifact_id,
            artifact_version=checkpoint.artifact_version,
            content_sha256=checkpoint.content_sha256,
            schema_version=checkpoint.schema_version,
            adapter_version=checkpoint.adapter_version,
            classification=checkpoint.classification,
        )


class AgentAttemptResponseDto(StrictResponseCamelModel):
    id: UUID
    run_id: UUID
    number: PositiveInteger
    state: AttemptState
    binding_id: UUID
    runner_generation: PositiveInteger
    checkpoint: CheckpointSummaryResponseDto | None
    waiting_deadline: datetime | None
    revision: PositiveInteger
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, attempt: AgentAttempt) -> "AgentAttemptResponseDto":
        return cls(
            id=UUID(attempt.id),
            run_id=UUID(attempt.run_id),
            number=attempt.number,
            state=attempt.state,
            binding_id=UUID(attempt.binding_id),
            runner_generation=attempt.runner_generation,
            checkpoint=(
                CheckpointSummaryResponseDto.from_domain(attempt.checkpoint)
                if attempt.checkpoint is not None
                else None
            ),
            waiting_deadline=attempt.waiting_deadline,
            revision=attempt.revision,
            created_at=attempt.created_at,
            updated_at=attempt.updated_at,
        )


class ExecutionBindingSummaryResponseDto(StrictResponseCamelModel):
    id: UUID
    source: ExecutionBindingSource
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_domain(cls, binding: ExecutionBinding) -> "ExecutionBindingSummaryResponseDto":
        return cls(id=UUID(binding.id), source=binding.source, digest=binding.digest)


class StartAgentRunResponseDto(StrictResponseCamelModel):
    run: AgentRunResponseDto
    attempt: AgentAttemptResponseDto
    binding: ExecutionBindingSummaryResponseDto

    @classmethod
    def from_domain(cls, result: StartRunResult) -> "StartAgentRunResponseDto":
        return cls(
            run=AgentRunResponseDto.from_domain(result.run),
            attempt=AgentAttemptResponseDto.from_domain(result.attempt),
            binding=ExecutionBindingSummaryResponseDto.from_domain(result.binding),
        )


class AgentRunDetailsResponseDto(StrictResponseCamelModel):
    run: AgentRunResponseDto
    attempts: list[AgentAttemptResponseDto]
    bindings: list[ExecutionBindingSummaryResponseDto]

    @classmethod
    def from_domain(cls, view: AgentRunView) -> "AgentRunDetailsResponseDto":
        return cls(
            run=AgentRunResponseDto.from_domain(view.run),
            attempts=[AgentAttemptResponseDto.from_domain(item) for item in view.attempts],
            bindings=[
                ExecutionBindingSummaryResponseDto.from_domain(item) for item in view.bindings
            ],
        )


class AgentRunListItemResponseDto(StrictResponseCamelModel):
    """Actual persisted latest execution association, not a runtime-health claim.

    DEV_FAKE is a development simulation; CONFIGURATION only identifies the binding source.
    """

    run: AgentRunResponseDto
    latest_attempt: AgentAttemptResponseDto
    binding: ExecutionBindingSummaryResponseDto

    @classmethod
    def from_domain(cls, value: AgentRunListItem) -> "AgentRunListItemResponseDto":
        return cls(
            run=AgentRunResponseDto.from_domain(value.run),
            latest_attempt=AgentAttemptResponseDto.from_domain(value.latest_attempt),
            binding=ExecutionBindingSummaryResponseDto.from_domain(value.binding),
        )


class AgentRunListResponseDto(StrictResponseCamelModel):
    """Workspace/state-scoped, descending createdAt/id page. No execution side effects."""

    items: list[AgentRunListItemResponseDto]
    next_cursor: str | None = Field(max_length=2048)

    @classmethod
    def from_domain(cls, page: AgentRunPage) -> "AgentRunListResponseDto":
        return cls(
            items=[AgentRunListItemResponseDto.from_domain(value) for value in page.items],
            next_cursor=page.next_cursor,
        )


class CanonicalEventSummaryResponseDto(StrictResponseCamelModel):
    id: UUID
    event_type: PublicName
    attempt_id: UUID
    generation: PositiveInteger
    sequence: PositiveInteger
    correlation_id: PublicReference
    causation_id: PublicReference | None
    trace_id: PublicReference
    span_id: PublicReference
    summary: str = Field(max_length=10_000)

    @classmethod
    def from_domain(cls, event: CanonicalEventInput) -> "CanonicalEventSummaryResponseDto":
        return cls(
            id=UUID(event.id),
            event_type=event.event_type,
            attempt_id=UUID(event.attempt_id),
            generation=event.generation,
            sequence=event.sequence,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            trace_id=event.trace_id,
            span_id=event.span_id,
            summary=event.summary,
        )


class CanonicalEventPageResponseDto(StrictResponseCamelModel):
    items: list[CanonicalEventSummaryResponseDto]
    next_cursor: str | None = Field(max_length=2048)

    @classmethod
    def from_domain(cls, page: CanonicalEventPage) -> "CanonicalEventPageResponseDto":
        return cls(
            items=[CanonicalEventSummaryResponseDto.from_domain(item) for item in page.items],
            next_cursor=page.next_cursor,
        )


class AttemptControlResponseDto(StrictResponseCamelModel):
    attempt: AgentAttemptResponseDto

    @classmethod
    def from_domain(cls, result: AttemptControlResult) -> "AttemptControlResponseDto":
        return cls(attempt=AgentAttemptResponseDto.from_domain(result.attempt))


def json_content(dto: CamelModel) -> dict[str, Any]:
    return dto.model_dump(mode="json", by_alias=True)
