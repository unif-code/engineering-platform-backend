import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from control_plane.app.modules.source_control.domain.models import NonEmptyStr, PositiveInt
from control_plane.app.modules.source_control.domain.transitions import SourceControlError

ExactCommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Sha256Digest = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]
ArtifactReference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


class AgentPushState(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    IN_FLIGHT = "IN_FLIGHT"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION = "RECONCILIATION"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FENCED = "FENCED"


class AgentDeliveryFactTopic(StrEnum):
    CONFIRMED = "source-control.agent-push-confirmed.v1"
    FENCED = "source-control.agent-push-fenced.v1"


class InvalidAgentPushTransition(SourceControlError):
    pass


class InvalidAgentPushGrant(SourceControlError):
    pass


class AgentPushBindingRejected(SourceControlError):
    pass


class AgentPushIdempotencyConflict(SourceControlError):
    pass


class AgentPushNotFound(SourceControlError):
    pass


class AgentPushRequestSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ]
    correlation_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ]
    attempt_id: NonEmptyStr
    attempt_generation: PositiveInt
    requirement_id: NonEmptyStr
    work_item_id: NonEmptyStr
    workspace_id: NonEmptyStr
    repository_id: NonEmptyStr
    branch_binding_id: NonEmptyStr
    branch_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ]
    expected_remote_head_sha: ExactCommitSha
    target_commit_sha: ExactCommitSha
    content_digest: Sha256Digest
    artifact_refs: tuple[ArtifactReference, ...] = Field(default=(), max_length=32)
    expires_at: AwareDatetime


class AgentExecutionBindingSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: NonEmptyStr
    attempt_generation: PositiveInt
    execution_binding_digest: Sha256Digest
    requirement_id: NonEmptyStr
    work_item_id: NonEmptyStr
    workspace_id: NonEmptyStr
    repository_id: NonEmptyStr
    branch_binding_id: NonEmptyStr
    branch_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ]
    active: bool
    fenced: bool

    def matches(self, spec: AgentPushRequestSpec) -> bool:
        return (
            self.active
            and not self.fenced
            and (
                self.attempt_id,
                self.attempt_generation,
                self.requirement_id,
                self.work_item_id,
                self.workspace_id,
                self.repository_id,
                self.branch_binding_id,
                self.branch_name,
            )
            == (
                spec.attempt_id,
                spec.attempt_generation,
                spec.requirement_id,
                spec.work_item_id,
                spec.workspace_id,
                spec.repository_id,
                spec.branch_binding_id,
                spec.branch_name,
            )
        )


class AgentDeliveryDto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyStr
    attempt_id: NonEmptyStr
    attempt_generation: PositiveInt
    requirement_id: NonEmptyStr
    work_item_id: NonEmptyStr
    workspace_id: NonEmptyStr
    repository_id: NonEmptyStr
    branch_binding_id: NonEmptyStr
    branch_name: NonEmptyStr
    expected_remote_head_sha: ExactCommitSha
    target_commit_sha: ExactCommitSha
    content_digest: Sha256Digest
    artifact_refs: tuple[ArtifactReference, ...] = Field(default=(), max_length=32)
    state: AgentPushState
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    consumed_at: AwareDatetime | None
    observed_at: AwareDatetime | None
    completed_at: AwareDatetime | None
    remote_head_sha: ExactCommitSha | None
    last_error_code: NonEmptyStr | None
    correlation_id: NonEmptyStr


@dataclass(frozen=True, slots=True)
class AgentPushGrantResult:
    delivery: AgentDeliveryDto
    raw_grant: str | None = field(repr=False)
    replayed: bool


class AgentDeliveryBatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claimed: int = Field(ge=0)
    processed: int = Field(ge=0)
    deliveries: tuple[AgentDeliveryDto, ...] = ()


class AgentFenceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: NonEmptyStr
    fenced_generation: PositiveInt
    revocation_state: Literal["PENDING", "UNKNOWN", "SUCCEEDED"]
    deliveries: tuple[AgentDeliveryDto, ...] = ()


class AgentRevocationBatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claimed: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    unknown: int = Field(ge=0)


class AgentDeliveryFactPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: NonEmptyStr
    attempt_generation: PositiveInt
    requirement_id: NonEmptyStr
    work_item_id: NonEmptyStr
    workspace_id: NonEmptyStr
    repository_id: NonEmptyStr
    branch_binding_id: NonEmptyStr
    branch_name: NonEmptyStr
    target_commit_sha: ExactCommitSha
    content_digest: Sha256Digest
    artifact_refs: tuple[ArtifactReference, ...] = Field(default=(), max_length=32)
    executor_type: Literal["AGENT"]
    correlation_id: NonEmptyStr
    observed_at: AwareDatetime
    reason_code: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        ]
        | None
    ) = None


class AgentDeliveryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: AgentDeliveryFactTopic
    payload: AgentDeliveryFactPayload

    @model_validator(mode="after")
    def validate_topic_shape(self) -> "AgentDeliveryFact":
        if self.topic is AgentDeliveryFactTopic.CONFIRMED and self.payload.reason_code is not None:
            raise ValueError("confirmed Agent delivery fact cannot contain a fence reason")
        if self.topic is AgentDeliveryFactTopic.FENCED and self.payload.reason_code is None:
            raise ValueError("fenced Agent delivery fact requires a reason")
        return self


_AGENT_PUSH_TRANSITIONS = {
    AgentPushState.AUTHORIZED: {
        AgentPushState.IN_FLIGHT,
        AgentPushState.BLOCKED,
        AgentPushState.FENCED,
    },
    AgentPushState.IN_FLIGHT: {
        AgentPushState.UNKNOWN,
        AgentPushState.SUCCEEDED,
        AgentPushState.BLOCKED,
        AgentPushState.FENCED,
    },
    AgentPushState.UNKNOWN: {
        AgentPushState.RECONCILIATION,
        AgentPushState.FENCED,
    },
    AgentPushState.RECONCILIATION: {
        AgentPushState.UNKNOWN,
        AgentPushState.SUCCEEDED,
        AgentPushState.BLOCKED,
        AgentPushState.FENCED,
    },
    AgentPushState.SUCCEEDED: set(),
    AgentPushState.BLOCKED: set(),
    AgentPushState.FENCED: set(),
}


def transition_agent_push(
    current: AgentPushState,
    target: AgentPushState,
) -> AgentPushState:
    if target not in _AGENT_PUSH_TRANSITIONS[current]:
        raise InvalidAgentPushTransition(f"{current.value}->{target.value}")
    return target


def validate_agent_push_expiry(
    *,
    issued_at: datetime,
    expires_at: datetime,
    max_ttl: timedelta,
) -> None:
    ttl = expires_at - issued_at
    if max_ttl <= timedelta(0) or ttl <= timedelta(0) or ttl > max_ttl:
        raise InvalidAgentPushGrant("Agent push grant expiry is invalid")


def agent_push_request_fingerprint(spec: AgentPushRequestSpec) -> str:
    canonical = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def digest_agent_push_grant(raw_grant: str) -> str:
    if not raw_grant.strip():
        raise InvalidAgentPushGrant("Agent push grant is invalid")
    return f"sha256:{hashlib.sha256(raw_grant.encode('utf-8')).hexdigest()}"
