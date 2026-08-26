from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class EffectState(StrEnum):
    PLANNED = "PLANNED"
    IN_FLIGHT = "IN_FLIGHT"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION = "RECONCILIATION"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"


class InboxState(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class WebhookInboxState(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


class RepositoryAuthorizationState(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    REMOVED = "REMOVED"


class RequirementCallbackState(StrEnum):
    PENDING = "PENDING"
    ACKED = "ACKED"
    FAILED = "FAILED"


class WorkspaceRepositoryDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository_id: NonEmptyStr
    workspace_id: NonEmptyStr
    provider: Literal["GITLAB"]
    project_id: NonEmptyStr
    project_path: NonEmptyStr
    default_branch: Literal["main"]
    connection_ref: NonEmptyStr
    credential_secret_ref: NonEmptyStr
    webhook_signing_secret_ref: NonEmptyStr | None
    status: RepositoryAuthorizationState
    revision: PositiveInt
    created_at: AwareDatetime
    updated_at: AwareDatetime


class BindingRequestEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: NonEmptyStr
    topic: Literal["requirement.repository-binding.requested"]
    requirement_id: NonEmptyStr
    requirement_version: PositiveInt
    work_item_id: NonEmptyStr
    repository_id: NonEmptyStr
    attempts: PositiveInt


class BindingRequestInboxDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: NonEmptyStr
    payload_hash: NonEmptyStr
    requirement_id: NonEmptyStr
    requirement_version: PositiveInt
    work_item_id: NonEmptyStr
    repository_id: NonEmptyStr
    state: InboxState
    attempts: NonNegativeInt
    available_at: AwareDatetime
    last_error_code: NonEmptyStr | None
    received_at: AwareDatetime
    updated_at: AwareDatetime
    processed_at: AwareDatetime | None


class RelayBindingRequestsResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    claimed: NonNegativeInt
    accepted: NonNegativeInt
    released: NonNegativeInt


class SourceControlEffectDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    effect_key: NonEmptyStr
    operation: Literal["CREATE_TASK_BRANCH"]
    work_item_id: NonEmptyStr
    requirement_id: NonEmptyStr
    repository_id: NonEmptyStr
    work_item_number: PositiveInt
    branch_name: NonEmptyStr
    base_commit_sha: NonEmptyStr
    request_fingerprint: NonEmptyStr
    attempts: NonNegativeInt
    next_reconcile_at: AwareDatetime | None
    state: EffectState
    last_error_code: NonEmptyStr | None
    callback_state: RequirementCallbackState
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None


class RepositoryBranchBindingDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    work_item_id: NonEmptyStr
    requirement_id: NonEmptyStr
    workspace_id: NonEmptyStr
    repository_id: NonEmptyStr
    work_item_number: PositiveInt
    base_commit_sha: NonEmptyStr
    branch_name: NonEmptyStr
    effect_id: NonEmptyStr
    created_at: AwareDatetime


class ProcessBindingRequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    effect: SourceControlEffectDto | None
    binding: RepositoryBranchBindingDto | None
    blocked_reason: NonEmptyStr | None


class ReconcileDueEffectsResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    effects: tuple[SourceControlEffectDto, ...]


class GitLabWebhookEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository_id: NonEmptyStr
    webhook_id: NonEmptyStr
    webhook_timestamp: AwareDatetime
    payload_digest: NonEmptyStr
    provider_event_uuid: NonEmptyStr | None
    event_type: NonEmptyStr
    object_kind: NonEmptyStr | None
    project_id: NonEmptyStr
    ref: NonEmptyStr | None
    before_sha: NonEmptyStr | None
    after_sha: NonEmptyStr | None
    checkout_sha: NonEmptyStr | None


class VerifiedStandardWebhook(BaseModel):
    model_config = ConfigDict(frozen=True)

    webhook_id: NonEmptyStr
    timestamp: AwareDatetime


class WebhookInboxDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    repository_id: NonEmptyStr
    webhook_id: NonEmptyStr
    webhook_timestamp: AwareDatetime
    payload_digest: NonEmptyStr
    provider_event_uuid: NonEmptyStr | None
    event_type: NonEmptyStr
    object_kind: NonEmptyStr | None
    project_id: NonEmptyStr
    ref: NonEmptyStr | None
    before_sha: NonEmptyStr | None
    after_sha: NonEmptyStr | None
    checkout_sha: NonEmptyStr | None
    state: WebhookInboxState
    last_error_code: NonEmptyStr | None
    received_at: AwareDatetime
    updated_at: AwareDatetime
    processed_at: AwareDatetime | None
