from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from control_plane.app.modules.source_control.domain import (
    DeliveryRequestEnvelope,
    DeliveryRequestKind,
    EffectOperation,
    EffectState,
    MergeRequestBindingDto,
    MergeRequestCreationOrigin,
    MergeRequestKind,
    MergeRequestObservationDto,
    MergeRequestState,
    RequirementCallbackState,
    SourceControlEffectDto,
    branch_effect_coordinates,
    merge_effect_subject,
)

NOW = datetime(2026, 8, 26, 4, 0, tzinfo=UTC)
BINDING_ID = "70000000-0000-0000-0000-000000000501"
OBSERVATION_ID = "80000000-0000-0000-0000-000000000501"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000501"


def test_delivery_request_shape_binds_topic_kind_and_existing_mr_binding() -> None:
    create = DeliveryRequestEnvelope(
        message_id="30000000-0000-0000-0000-000000000501",
        topic="requirement.integration-merge-request.requested",
        payload_hash="sha256:create",
        requirement_id="40000000-0000-0000-0000-000000000501",
        requirement_revision=3,
        work_item_id=WORK_ITEM_ID,
        work_item_revision=5,
        repository_id="10000000-0000-0000-0000-000000000501",
        actor_id="employee-1",
        kind=DeliveryRequestKind.CREATE_MR,
        integration_merge_request_binding_id=None,
        attempts=1,
    )
    assert create.kind is DeliveryRequestKind.CREATE_MR

    with pytest.raises(ValidationError):
        DeliveryRequestEnvelope(
            **{
                **create.model_dump(),
                "kind": DeliveryRequestKind.MERGE_MR,
                "topic": "requirement.integration-merge.requested",
            }
        )


def test_merge_effect_subject_is_exact_binding_and_head() -> None:
    assert merge_effect_subject(BINDING_ID, "a" * 40) == f"mr:{BINDING_ID}:" + "a" * 40


def test_observation_requires_merge_commit_only_when_merged() -> None:
    MergeRequestObservationDto(
        id=OBSERVATION_ID,
        binding_id=BINDING_ID,
        head_sha="a" * 40,
        state=MergeRequestState.OPEN,
        merge_commit_sha=None,
        external_merge_user_id=None,
        merged_at=None,
        observation_digest="sha256:open",
        observed_at=NOW,
    )
    with pytest.raises(ValidationError):
        MergeRequestObservationDto(
            id=OBSERVATION_ID,
            binding_id=BINDING_ID,
            head_sha="a" * 40,
            state=MergeRequestState.MERGED,
            merge_commit_sha=None,
            external_merge_user_id="42",
            merged_at=NOW,
            observation_digest="sha256:merged",
            observed_at=NOW,
        )
    with pytest.raises(ValidationError):
        MergeRequestObservationDto(
            id=OBSERVATION_ID,
            binding_id=BINDING_ID,
            head_sha="a" * 40,
            state=MergeRequestState.OPEN,
            merge_commit_sha="b" * 40,
            external_merge_user_id="42",
            merged_at=NOW,
            observation_digest="sha256:invalid-open",
            observed_at=NOW,
        )


def test_integration_binding_and_observation_are_frozen() -> None:
    binding = MergeRequestBindingDto(
        id=BINDING_ID,
        kind=MergeRequestKind.INTEGRATION,
        work_item_id=WORK_ITEM_ID,
        requirement_id="40000000-0000-0000-0000-000000000501",
        workspace_id="20000000-0000-0000-0000-000000000501",
        repository_id="10000000-0000-0000-0000-000000000501",
        branch_binding_id="71000000-0000-0000-0000-000000000501",
        external_project_id="101",
        merge_request_iid=42,
        source_branch="feat/wi-501-integration",
        target_branch="dev",
        create_effect_id="60000000-0000-0000-0000-000000000501",
        head_sha="a" * 40,
        creation_origin=MergeRequestCreationOrigin.PLATFORM_CREATED,
        created_at=NOW,
    )

    with pytest.raises(ValidationError, match="frozen"):
        binding.target_branch = "dev"


def test_effect_operation_shapes_preserve_branch_dto_and_reject_ambiguous_fields() -> None:
    common: dict[str, Any] = {
        "id": "60000000-0000-0000-0000-000000000501",
        "effect_key": "source-control:create-task-branch:work-item-501",
        "work_item_id": WORK_ITEM_ID,
        "requirement_id": "40000000-0000-0000-0000-000000000501",
        "repository_id": "10000000-0000-0000-0000-000000000501",
        "request_fingerprint": "sha256:fingerprint",
        "attempts": 0,
        "next_reconcile_at": None,
        "state": EffectState.PLANNED,
        "last_error_code": None,
        "callback_state": RequirementCallbackState.PENDING,
        "created_at": NOW,
        "updated_at": NOW,
        "completed_at": None,
    }
    branch = SourceControlEffectDto(
        **common,
        operation=EffectOperation.CREATE_TASK_BRANCH,
        work_item_number=501,
        branch_name="feat/wi-501-integration",
        base_commit_sha="a" * 40,
    )
    create_mr = SourceControlEffectDto(
        **{
            **common,
            "id": "60000000-0000-0000-0000-000000000502",
            "effect_key": "source-control:create-integration-mr:work-item-501",
        },
        operation=EffectOperation.CREATE_INTEGRATION_MR,
        subject_key=f"work-item:{WORK_ITEM_ID}",
        payload={"branchBindingId": "71000000-0000-0000-0000-000000000501"},
        work_item_number=None,
        branch_name=None,
        base_commit_sha=None,
    )

    assert branch.subject_key == f"work-item:{WORK_ITEM_ID}"
    assert branch.payload == {}
    assert branch_effect_coordinates(branch) == (
        501,
        "feat/wi-501-integration",
        "a" * 40,
    )
    assert create_mr.operation is EffectOperation.CREATE_INTEGRATION_MR
    with pytest.raises(ValidationError):
        SourceControlEffectDto(
            **{
                **common,
                "id": "60000000-0000-0000-0000-000000000503",
                "effect_key": "source-control:ambiguous:work-item-501",
            },
            operation=EffectOperation.MERGE_INTEGRATION_MR,
            subject_key=merge_effect_subject(BINDING_ID, "a" * 40),
            payload={"bindingId": BINDING_ID, "requestedHeadSha": "a" * 40},
            work_item_number=501,
            branch_name=None,
            base_commit_sha=None,
        )
