import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.model_gateway.domain import (
    ConnectionRef,
    ProviderKind,
    ProviderModelId,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    ADAPTER_VERSION,
    PROBE_VERSIONS,
    CheckKind,
    ConnectionDefinition,
    VersionLabel,
    digest,
    probe_body,
)


class CheckState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"


class CheckReason(StrEnum):
    CONNECTION_MISSING = "CONNECTION_MISSING"
    CONNECTION_UNAVAILABLE = "CONNECTION_UNAVAILABLE"
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    MATERIAL_UNAVAILABLE = "MATERIAL_UNAVAILABLE"
    MATERIAL_VERSION_CHANGED = "MATERIAL_VERSION_CHANGED"
    INPUT_CHANGED = "INPUT_CHANGED"
    CANDIDATE_ARCHIVED = "CANDIDATE_ARCHIVED"
    ACTOR_INELIGIBLE = "ACTOR_INELIGIBLE"
    AUTHORIZATION_UNAVAILABLE = "AUTHORIZATION_UNAVAILABLE"
    CONNECTION_BUSY = "CONNECTION_BUSY"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    TARGET_NOT_ALLOWED = "TARGET_NOT_ALLOWED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    REDIRECT_REJECTED = "REDIRECT_REJECTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    MODEL_IDENTITY_MISMATCH = "MODEL_IDENTITY_MISMATCH"
    RESPONSE_TRUNCATED = "RESPONSE_TRUNCATED"
    RESPONSE_REFUSED = "RESPONSE_REFUSED"
    REQUEST_OUTCOME_UNKNOWN = "REQUEST_OUTCOME_UNKNOWN"
    EXECUTION_EXPIRED = "EXECUTION_EXPIRED"
    INVALID_STREAM_RESPONSE = "INVALID_STREAM_RESPONSE"
    STREAM_IDENTITY_CHANGED = "STREAM_IDENTITY_CHANGED"
    STREAM_EVENT_TOO_LARGE = "STREAM_EVENT_TOO_LARGE"
    STREAM_EVENT_LIMIT = "STREAM_EVENT_LIMIT"
    STREAM_INTERRUPTED = "STREAM_INTERRUPTED"
    STREAM_CLOSE_FAILED = "STREAM_CLOSE_FAILED"


class InputCurrentness(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNVERIFIABLE = "UNVERIFIABLE"


class CurrentnessReason(StrEnum):
    CANDIDATE_CHANGED = "CANDIDATE_CHANGED"
    CANDIDATE_ARCHIVED = "CANDIDATE_ARCHIVED"
    CONNECTION_CHANGED = "CONNECTION_CHANGED"
    CONNECTION_UNAVAILABLE = "CONNECTION_UNAVAILABLE"
    MATERIAL_VERSION_CHANGED = "MATERIAL_VERSION_CHANGED"
    MATERIAL_UNVERIFIABLE = "MATERIAL_UNVERIFIABLE"
    PROBE_CHANGED = "PROBE_CHANGED"


class CheckInputSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    deployment_id: str
    deployment_revision: int = Field(ge=1)
    provider_kind: ProviderKind
    provider_model_id: ProviderModelId
    connection_ref: ConnectionRef | None
    environment: str | None
    region: str | None
    connection_version: VersionLabel | None
    material_version: VersionLabel | None
    connection_fingerprint: str | None
    adapter_version: str
    probe_version: str
    input_digest: str

    @classmethod
    def capture(
        cls,
        deployment: object,
        connection: ConnectionDefinition | None,
        environment: str | None,
        check_kind: CheckKind,
    ) -> "CheckInputSnapshot":
        from control_plane.app.modules.model_gateway.domain import Deployment

        assert isinstance(deployment, Deployment)
        values = {
            "deployment_id": deployment.id,
            "deployment_revision": deployment.revision,
            "provider_kind": deployment.provider_kind,
            "provider_model_id": deployment.provider_model_id,
            "connection_ref": deployment.connection_ref,
            "environment": environment,
            "region": connection.region if connection else None,
            "connection_version": connection.version if connection else None,
            "material_version": connection.material_version if connection else None,
            "connection_fingerprint": connection.fingerprint if connection else None,
            "adapter_version": ADAPTER_VERSION,
            "probe_version": PROBE_VERSIONS[check_kind],
        }
        return cls.model_validate(
            values
            | {
                "input_digest": digest(
                    {
                        "input": values,
                        "probe": probe_body(deployment.provider_model_id, check_kind),
                    }
                )
            }
        )


UsageCount = Annotated[int, Field(strict=True, ge=0, le=2147483647)]


class ProbeUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    prompt_tokens: UsageCount | None = None
    completion_tokens: UsageCount | None = None
    total_tokens: UsageCount | None = None


class BasicTextObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[CheckKind.BASIC_TEXT]
    consumed_bytes: int = Field(ge=0, le=65537)
    text_observed: bool
    normal_completion_observed: bool


class StreamObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[CheckKind.STREAM_TEXT, CheckKind.STREAM_STOP]
    consumed_bytes: int = Field(ge=0, le=65537)
    data_event_count: int = Field(ge=0, le=257)
    text_delta_count: int = Field(ge=0, le=256)
    text_bytes: int = Field(ge=0, le=65536)
    text_observed: bool
    normal_completion_observed: bool
    completion_marker_observed: bool
    local_stream_closed: bool
    provider_cancellation: Literal["UNCONFIRMED"] = "UNCONFIRMED"


ProbeObservation = Annotated[BasicTextObservation | StreamObservation, Field(discriminator="kind")]


class ProbeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    observation: ProbeObservation | None = None
    state: CheckState
    reason: CheckReason | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = None
    reported_model_id: str | None = None
    usage: ProbeUsage | None = None


class ConnectionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    check_kind: CheckKind
    observation: ProbeObservation | None = None
    id: str
    deployment_id: str
    revision: int = Field(ge=1)
    requested_by: str
    requested_at: datetime
    input: CheckInputSnapshot
    state: CheckState
    reason: CheckReason | None
    attempt: int = Field(ge=0, le=1)
    execution_token: str | None
    started_at: datetime | None
    deadline_at: datetime | None
    finished_at: datetime | None
    elapsed_ms: int | None
    provider_request_id: str | None
    reported_model_id: str | None
    usage: ProbeUsage | None
    material_currentness: InputCurrentness


class CheckBlocked(Exception):
    def __init__(self, reason: CheckReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def provider_request_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None
        or value.lower().startswith("sk-")
    ):
        raise ValueError("invalid provider identifier")
    return value
