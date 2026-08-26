from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from control_plane.app.modules.source_control.domain.models import (
    EffectOperation,
    NonEmptyStr,
    PositiveInt,
    SourceControlEffectDto,
)


class DeliveryRequestKind(StrEnum):
    CREATE_MR = "CREATE_MR"
    MERGE_MR = "MERGE_MR"


class MergeRequestKind(StrEnum):
    INTEGRATION = "INTEGRATION"


class MergeRequestCreationOrigin(StrEnum):
    PLATFORM_CREATED = "PLATFORM_CREATED"
    EXTERNAL_ADOPTED = "EXTERNAL_ADOPTED"


class MergeRequestState(StrEnum):
    OPEN = "OPEN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"
    LOCKED = "LOCKED"


class DeliveryRequestEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: NonEmptyStr
    topic: Literal[
        "requirement.integration-merge-request.requested",
        "requirement.integration-merge.requested",
    ]
    payload_hash: NonEmptyStr
    requirement_id: NonEmptyStr
    requirement_revision: PositiveInt
    work_item_id: NonEmptyStr
    work_item_revision: PositiveInt
    repository_id: NonEmptyStr
    actor_id: NonEmptyStr
    kind: DeliveryRequestKind
    integration_merge_request_binding_id: NonEmptyStr | None
    attempts: PositiveInt

    @model_validator(mode="after")
    def validate_request_shape(self) -> "DeliveryRequestEnvelope":
        create_shape = (
            self.kind is DeliveryRequestKind.CREATE_MR
            and self.topic == "requirement.integration-merge-request.requested"
            and self.integration_merge_request_binding_id is None
        )
        merge_shape = (
            self.kind is DeliveryRequestKind.MERGE_MR
            and self.topic == "requirement.integration-merge.requested"
            and self.integration_merge_request_binding_id is not None
        )
        if not (create_shape or merge_shape):
            raise ValueError("delivery request operation shape is invalid")
        return self


class MergeRequestBindingDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    kind: MergeRequestKind
    work_item_id: NonEmptyStr
    requirement_id: NonEmptyStr
    workspace_id: NonEmptyStr
    repository_id: NonEmptyStr
    branch_binding_id: NonEmptyStr
    external_project_id: NonEmptyStr
    merge_request_iid: PositiveInt
    source_branch: NonEmptyStr
    target_branch: Literal["dev"]
    create_effect_id: NonEmptyStr
    head_sha: NonEmptyStr
    creation_origin: MergeRequestCreationOrigin
    created_at: AwareDatetime


class MergeRequestObservationDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    binding_id: NonEmptyStr
    head_sha: NonEmptyStr
    state: MergeRequestState
    merge_commit_sha: NonEmptyStr | None
    external_merge_user_id: NonEmptyStr | None
    merged_at: AwareDatetime | None
    observation_digest: NonEmptyStr
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_state_shape(self) -> "MergeRequestObservationDto":
        if self.state is MergeRequestState.MERGED:
            if self.merge_commit_sha is None or self.merged_at is None:
                raise ValueError("merged observation requires merge facts")
        elif any(
            value is not None
            for value in (
                self.merge_commit_sha,
                self.external_merge_user_id,
                self.merged_at,
            )
        ):
            raise ValueError("non-merged observation cannot contain merge facts")
        return self


def merge_effect_subject(binding_id: str, head_sha: str) -> str:
    if not binding_id.strip() or not head_sha.strip():
        raise ValueError("merge effect subject requires binding and head")
    return f"mr:{binding_id}:{head_sha}"


def branch_effect_coordinates(effect: SourceControlEffectDto) -> tuple[int, str, str]:
    if (
        effect.operation is not EffectOperation.CREATE_TASK_BRANCH
        or effect.work_item_number is None
        or effect.branch_name is None
        or effect.base_commit_sha is None
    ):
        raise ValueError("effect is not a complete branch operation")
    return effect.work_item_number, effect.branch_name, effect.base_commit_sha
