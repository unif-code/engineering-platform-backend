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


class EffectOperation(StrEnum):
    CREATE_TASK_BRANCH = "CREATE_TASK_BRANCH"
    CREATE_INTEGRATION_MR = "CREATE_INTEGRATION_MR"
    MERGE_INTEGRATION_MR = "MERGE_INTEGRATION_MR"


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


ExactHeadSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class TaskBranchEffectPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CreateIntegrationMergeRequestEffectPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    branch_binding_id: NonEmptyStr = Field(alias="branchBindingId")
    head_sha: ExactHeadSha = Field(alias="headSha")


class MergeIntegrationMergeRequestEffectPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    binding_id: NonEmptyStr = Field(alias="bindingId")
    requested_head_sha: ExactHeadSha = Field(alias="requestedHeadSha")


SourceControlEffectPayload = (
    TaskBranchEffectPayload
    | CreateIntegrationMergeRequestEffectPayload
    | MergeIntegrationMergeRequestEffectPayload
)


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


class SourceControlBatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    claimed: NonNegativeInt
    processed: NonNegativeInt
    released: NonNegativeInt = 0
    effect_ids: tuple[NonEmptyStr, ...] = ()
    error_codes: tuple[NonEmptyStr, ...] = ()


class SourceControlEffectDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    effect_key: NonEmptyStr
    operation: EffectOperation
    subject_key: NonEmptyStr = ""
    payload: SourceControlEffectPayload = Field(default_factory=TaskBranchEffectPayload)
    work_item_id: NonEmptyStr
    requirement_id: NonEmptyStr
    repository_id: NonEmptyStr
    work_item_number: PositiveInt | None
    branch_name: NonEmptyStr | None
    base_commit_sha: NonEmptyStr | None
    request_fingerprint: NonEmptyStr
    attempts: NonNegativeInt
    next_reconcile_at: AwareDatetime | None
    state: EffectState
    last_error_code: NonEmptyStr | None
    callback_state: RequirementCallbackState
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None

    @model_validator(mode="before")
    @classmethod
    def default_historical_branch_subject(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        if (
            values.get("operation")
            in {
                EffectOperation.CREATE_TASK_BRANCH,
                EffectOperation.CREATE_TASK_BRANCH.value,
            }
            and "subject_key" not in values
            and values.get("work_item_id")
        ):
            return {
                **values,
                "subject_key": f"work-item:{values['work_item_id']}",
            }
        return values

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "SourceControlEffectDto":
        branch_values = (
            self.work_item_number,
            self.branch_name,
            self.base_commit_sha,
        )
        work_item_subject = f"work-item:{self.work_item_id}"
        if self.operation is EffectOperation.CREATE_TASK_BRANCH:
            if (
                any(value is None for value in branch_values)
                or self.subject_key != work_item_subject
                or not isinstance(self.payload, TaskBranchEffectPayload)
            ):
                raise ValueError("branch effect operation shape is invalid")
        elif self.operation is EffectOperation.CREATE_INTEGRATION_MR:
            if (
                any(value is not None for value in branch_values)
                or self.subject_key != work_item_subject
                or not isinstance(
                    self.payload,
                    CreateIntegrationMergeRequestEffectPayload,
                )
            ):
                raise ValueError("merge request creation effect shape is invalid")
        else:
            subject_parts = self.subject_key.split(":")
            if (
                any(value is not None for value in branch_values)
                or not isinstance(
                    self.payload,
                    MergeIntegrationMergeRequestEffectPayload,
                )
                or len(subject_parts) != 3
                or subject_parts[0] != "mr"
                or not subject_parts[1]
                or len(subject_parts[2]) != 40
                or any(character not in "0123456789abcdef" for character in subject_parts[2])
                or subject_parts[1] != self.payload.binding_id
                or subject_parts[2] != self.payload.requested_head_sha
            ):
                raise ValueError("merge effect operation shape is invalid")
        return self


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


class ReconcileDueIntegrationEffectsResult(BaseModel):
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
    mr_iid: PositiveInt | None = None
    mr_action: Literal["open", "update", "merge", "close", "reopen"] | None = None
    source_branch: NonEmptyStr | None = None
    target_branch: NonEmptyStr | None = None
    mr_state: Literal["opened", "merged", "closed", "locked"] | None = None
    old_head_sha: ExactHeadSha | None = None
    head_sha: ExactHeadSha | None = None


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
    mr_iid: PositiveInt | None = None
    mr_action: Literal["open", "update", "merge", "close", "reopen"] | None = None
    source_branch: NonEmptyStr | None = None
    target_branch: NonEmptyStr | None = None
    mr_state: Literal["opened", "merged", "closed", "locked"] | None = None
    old_head_sha: ExactHeadSha | None = None
    head_sha: ExactHeadSha | None = None
    state: WebhookInboxState
    last_error_code: NonEmptyStr | None
    received_at: AwareDatetime
    updated_at: AwareDatetime
    processed_at: AwareDatetime | None
