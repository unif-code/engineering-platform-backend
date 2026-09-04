import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from control_plane.app.modules.agent.domain.errors import RepositoryWriteForbidden
from control_plane.app.modules.agent.domain.types import (
    ContentDigest,
    PlatformName,
    PlatformReference,
    PlatformSummary,
    PlatformUUID,
)

_FORBIDDEN_PERMISSION_PARTS = (
    "repository.write",
    "git.push",
    "source_control.write",
    "merge",
)


def _freeze_json(value: object) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite platform JSON float")
        return value
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"Unsupported platform JSON value: {type(value).__name__}")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class AttemptState(StrEnum):
    CREATED = "CREATED"
    BINDING = "BINDING"
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    FINALIZING = "FINALIZING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELING = "CANCELING"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"


class RunState(StrEnum):
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"


class WorkflowCommandKind(StrEnum):
    START = "START"
    CANCEL = "CANCEL"
    RESUME = "RESUME"


class WorkflowCommandState(StrEnum):
    PLANNED = "PLANNED"
    DISPATCHED = "DISPATCHED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class WorkflowClaimMode(StrEnum):
    DISPATCH = "DISPATCH"
    RECONCILE = "RECONCILE"


class ExecutionBindingSource(StrEnum):
    DEV_FAKE = "DEV_FAKE"
    CONFIGURATION = "CONFIGURATION"


class FrozenPlatformModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


class AgentDefinition(FrozenPlatformModel):
    id: PlatformUUID
    version: int = Field(ge=1)
    name: PlatformName
    capability_declarations: tuple[PlatformName, ...]
    skill_declarations: tuple[PlatformName, ...]
    runtime_permissions: tuple[PlatformName, ...]
    input_schema: Mapping[str, object]
    created_at: datetime

    def model_post_init(self, __context: Any) -> None:
        object.__setattr__(self, "input_schema", _freeze_json(self.input_schema))

    @field_serializer("input_schema")
    def serialize_input_schema(self, value: Mapping[str, object]) -> object:
        return _thaw_json(value)


class ExecutionBinding(FrozenPlatformModel):
    """Immutable platform snapshot bound to exactly one Attempt."""

    id: PlatformUUID
    source: ExecutionBindingSource
    runtime_ref: str
    model_route_ref: str
    capability_bundle_ref: str
    skill_refs: tuple[str, ...]
    runtime_permissions: tuple[str, ...]
    context_policy_ref: str
    network_policy_ref: str
    digest: str = ""

    def model_post_init(self, __context: Any) -> None:
        forbidden = next(
            (
                permission
                for permission in self.runtime_permissions
                if any(part in permission.casefold() for part in _FORBIDDEN_PERMISSION_PARTS)
            ),
            None,
        )
        if forbidden is not None:
            raise RepositoryWriteForbidden(forbidden)

        canonical = {
            "capabilityBundleRef": self.capability_bundle_ref,
            "contextPolicyRef": self.context_policy_ref,
            "modelRouteRef": self.model_route_ref,
            "networkPolicyRef": self.network_policy_ref,
            "runtimePermissions": list(self.runtime_permissions),
            "runtimeRef": self.runtime_ref,
            "skillRefs": list(self.skill_refs),
            "source": self.source.value,
        }
        payload = json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        object.__setattr__(self, "digest", hashlib.sha256(payload).hexdigest())


class CheckpointInput(FrozenPlatformModel):
    id: PlatformUUID
    artifact_id: PlatformReference
    artifact_version: PlatformName
    content_sha256: ContentDigest
    schema_version: PlatformName
    adapter_version: PlatformName
    classification: PlatformName


class AgentRun(FrozenPlatformModel):
    id: PlatformUUID
    workspace_id: PlatformUUID
    goal_ref: PlatformReference
    created_by: PlatformReference
    definition_id: PlatformUUID
    definition_version: int = Field(ge=1)
    latest_attempt_id: PlatformUUID
    state: RunState
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class AgentAttempt(FrozenPlatformModel):
    id: PlatformUUID
    run_id: PlatformUUID
    number: int = Field(ge=1)
    state: AttemptState
    binding_id: PlatformUUID
    binding_digest: str
    runner_generation: int = Field(ge=1)
    fencing_token: str
    checkpoint: CheckpointInput | None
    event_sequence: int = Field(default=0, ge=0)
    waiting_deadline: datetime | None = None
    terminal_evidence: Mapping[str, object] | None = None
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    def model_post_init(self, __context: Any) -> None:
        if self.state is AttemptState.WAITING_INPUT and self.checkpoint is None:
            raise ValueError(f"Checkpoint required for waiting Attempt: {self.id}")
        if self.terminal_evidence is not None:
            object.__setattr__(self, "terminal_evidence", _freeze_json(self.terminal_evidence))

    @field_serializer("terminal_evidence")
    def serialize_terminal_evidence(self, value: Mapping[str, object] | None) -> object:
        return _thaw_json(value) if value is not None else None


class AttemptMutation(FrozenPlatformModel):
    """The complete mutable Attempt projection guarded by one revision CAS."""

    state: AttemptState
    runner_generation: int = Field(ge=1)
    fencing_token: str
    checkpoint: CheckpointInput | None
    event_sequence: int = Field(ge=0)
    waiting_deadline: datetime | None
    terminal_evidence: Mapping[str, object] | None
    now: datetime

    def model_post_init(self, __context: Any) -> None:
        if self.state is AttemptState.WAITING_INPUT and self.checkpoint is None:
            raise ValueError("Checkpoint required for a waiting Attempt mutation")
        if self.terminal_evidence is not None:
            object.__setattr__(self, "terminal_evidence", _freeze_json(self.terminal_evidence))

    @field_serializer("terminal_evidence")
    def serialize_terminal_evidence(self, value: Mapping[str, object] | None) -> object:
        return _thaw_json(value) if value is not None else None


class RunMutation(FrozenPlatformModel):
    state: RunState
    latest_attempt_id: PlatformUUID
    now: datetime


class CanonicalEventInput(FrozenPlatformModel):
    id: PlatformUUID
    event_type: PlatformName
    attempt_id: PlatformUUID
    generation: int = Field(ge=1)
    sequence: int = Field(ge=1)
    correlation_id: PlatformReference
    causation_id: PlatformReference | None
    trace_id: PlatformReference
    span_id: PlatformReference
    summary: PlatformSummary
    data: Mapping[str, object]

    def model_post_init(self, __context: Any) -> None:
        if self.event_type == "ATTEMPT_QUEUED":
            if set(self.data) != {"bindingSource", "bindingDigest"}:
                raise ValueError("ATTEMPT_QUEUED requires only bindingSource and bindingDigest")
            source = self.data["bindingSource"]
            if not isinstance(source, str):
                raise ValueError("bindingSource must be a platform source")
            ExecutionBindingSource(source)
            digest = self.data["bindingDigest"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("ATTEMPT_QUEUED requires a canonical binding digest")
        elif self.event_type == "WAITING_INPUT":
            if set(self.data) != {"checkpoint", "waitingDeadline"}:
                raise ValueError("WAITING_INPUT requires only checkpoint and waitingDeadline")
            CheckpointInput.model_validate(self.data["checkpoint"])
            deadline = self.data["waitingDeadline"]
            if not isinstance(deadline, str) or datetime.fromisoformat(deadline).tzinfo is None:
                raise ValueError("waitingDeadline must include an offset")
        elif self.event_type in {
            "ATTEMPT_PROVISIONING",
            "ATTEMPT_RUNNING",
            "ATTEMPT_FINALIZING",
            "ATTEMPT_SUCCEEDED",
            "ATTEMPT_FAILED",
            "ATTEMPT_CANCELED",
            "ATTEMPT_TIMED_OUT",
        }:
            if self.data:
                raise ValueError("Lifecycle events have no platform data fields")
        else:
            raise ValueError("Unknown platform canonical event type")
        object.__setattr__(self, "data", _freeze_json(self.data))

    @field_serializer("data")
    def serialize_data(self, value: Mapping[str, object]) -> object:
        return _thaw_json(value)


class EventAcceptanceReceipt(FrozenPlatformModel):
    """Original accepted platform snapshots, independent of subsequent workflow state."""

    event_id: PlatformUUID
    schema_version: int = Field(default=1, ge=1, le=1)
    attempt: AgentAttempt
    checkpoint: CheckpointInput | None


class WorkflowCommand(FrozenPlatformModel):
    id: str
    command_key: str
    kind: WorkflowCommandKind
    attempt_id: str
    generation: int = Field(ge=1)
    state: WorkflowCommandState
    dispatch_attempts: int = Field(default=0, ge=0)
    receipt: Mapping[str, object] | None = None
    last_error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    dispatched_at: datetime | None = None
    claim_owner: str | None = None
    claim_token: str | None = None
    claim_lease_until: datetime | None = None
    claim_mode: WorkflowClaimMode | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.receipt is not None:
            object.__setattr__(self, "receipt", _freeze_json(self.receipt))
        claim_values = (
            self.claim_owner,
            self.claim_token,
            self.claim_lease_until,
            self.claim_mode,
        )
        if any(value is not None for value in claim_values) and any(
            value is None for value in claim_values
        ):
            raise ValueError("workflow claim fields must be present together")

    @field_serializer("receipt")
    def serialize_receipt(self, value: Mapping[str, object] | None) -> object:
        return _thaw_json(value) if value is not None else None


class IdempotencyState(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class AgentIdempotencyRecord(FrozenPlatformModel):
    id: str
    actor: str
    operation: str
    key: str
    request_fingerprint: str
    state: IdempotencyState
    http_status: int | None
    result_metadata: Mapping[str, object] | None
    sealed_response: bytes | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    def model_post_init(self, __context: Any) -> None:
        if self.result_metadata is not None:
            object.__setattr__(self, "result_metadata", _freeze_json(self.result_metadata))

    @field_serializer("result_metadata")
    def serialize_result_metadata(self, value: Mapping[str, object] | None) -> object:
        return _thaw_json(value) if value is not None else None


class IdempotencyReservation(FrozenPlatformModel):
    record: AgentIdempotencyRecord
    created: bool


class IdempotencyCompletion(FrozenPlatformModel):
    http_status: int = Field(ge=100, le=599)
    result_metadata: Mapping[str, object]
    sealed_response: bytes
    now: datetime

    def model_post_init(self, __context: Any) -> None:
        object.__setattr__(self, "result_metadata", _freeze_json(self.result_metadata))

    @field_serializer("result_metadata")
    def serialize_result_metadata(self, value: Mapping[str, object]) -> object:
        return _thaw_json(value)


class WorkflowDispatchMutation(FrozenPlatformModel):
    state: WorkflowCommandState
    receipt: Mapping[str, object] | None
    error_code: str | None
    now: datetime

    def model_post_init(self, __context: Any) -> None:
        if self.receipt is not None:
            object.__setattr__(self, "receipt", _freeze_json(self.receipt))

    @field_serializer("receipt")
    def serialize_receipt(self, value: Mapping[str, object] | None) -> object:
        return _thaw_json(value) if value is not None else None


class AgentAuditAppend(FrozenPlatformModel):
    id: str
    occurred_at: datetime
    actor: str
    actor_type: str
    action: str
    target_type: str
    target_id: str
    result: str
    reason: str
    correlation_id: str
    schema_version: int = Field(ge=1)
