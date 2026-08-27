from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from control_plane.app.modules.requirement import IntegrationDeliveryBlockedReason
from control_plane.app.modules.source_control.domain import (
    CreateIntegrationMergeRequestEffectPayload,
    DeliveryRequestEnvelope,
    DeliveryRequestKind,
    EffectOperation,
    EffectState,
    MergeIntegrationMergeRequestEffectPayload,
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
from control_plane.app.modules.source_control.domain.reasons import (
    CREATE_PREFLIGHT_REASONS,
    MERGE_PREFLIGHT_REASONS,
    REQUIREMENT_DELIVERY_REASONS,
    SourceControlReason,
)
from control_plane.app.modules.source_control.ports import IntegrationDeliveryBlockedResult

NOW = datetime(2026, 8, 26, 4, 0, tzinfo=UTC)
BINDING_ID = "70000000-0000-0000-0000-000000000501"
OBSERVATION_ID = "80000000-0000-0000-0000-000000000501"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000501"
BRANCH_BINDING_ID = "71000000-0000-0000-0000-000000000501"
HEAD_SHA = "a" * 40


def test_internal_delivery_blocked_callback_keeps_typed_reason_until_adapter_boundary() -> None:
    result = IntegrationDeliveryBlockedResult(
        work_item_id=WORK_ITEM_ID,
        binding_id=None,
        reason_code=SourceControlReason.MR_CONFLICT,
        expected_revision=5,
        idempotency_key="source-control:blocked:typed-reason",
    )

    assert result.reason_code is SourceControlReason.MR_CONFLICT


def test_source_control_requirement_reasons_match_public_requirement_contract() -> None:
    requirement_values = {reason.value for reason in IntegrationDeliveryBlockedReason}
    source_control_values = {reason.value for reason in REQUIREMENT_DELIVERY_REASONS}

    assert source_control_values == requirement_values
    assert CREATE_PREFLIGHT_REASONS <= REQUIREMENT_DELIVERY_REASONS
    assert MERGE_PREFLIGHT_REASONS <= REQUIREMENT_DELIVERY_REASONS


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
        payload=CreateIntegrationMergeRequestEffectPayload(
            branchBindingId=BRANCH_BINDING_ID,
            headSha=HEAD_SHA,
        ),
        work_item_number=None,
        branch_name=None,
        base_commit_sha=None,
    )

    assert branch.subject_key == f"work-item:{WORK_ITEM_ID}"
    assert branch.payload.model_dump(by_alias=True) == {}
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
            payload=MergeIntegrationMergeRequestEffectPayload(
                bindingId=BINDING_ID,
                requestedHeadSha="a" * 40,
            ),
            work_item_number=501,
            branch_name=None,
            base_commit_sha=None,
        )


@pytest.mark.parametrize(
    ("operation", "subject_key", "payload"),
    [
        (
            EffectOperation.CREATE_INTEGRATION_MR,
            f"work-item:{WORK_ITEM_ID}",
            {"branchBindingId": BRANCH_BINDING_ID},
        ),
        (
            EffectOperation.CREATE_INTEGRATION_MR,
            f"work-item:{WORK_ITEM_ID}",
            {
                "branchBindingId": BRANCH_BINDING_ID,
                "headSha": HEAD_SHA,
                "projectId": "101",
            },
        ),
        (
            EffectOperation.CREATE_INTEGRATION_MR,
            f"work-item:{WORK_ITEM_ID}",
            {
                "branchBindingId": BRANCH_BINDING_ID,
                "headSha": HEAD_SHA,
                "token": "must-not-persist",
            },
        ),
        (
            EffectOperation.MERGE_INTEGRATION_MR,
            merge_effect_subject(BINDING_ID, HEAD_SHA),
            {"bindingId": BINDING_ID},
        ),
        (
            EffectOperation.MERGE_INTEGRATION_MR,
            merge_effect_subject(BINDING_ID, HEAD_SHA),
            {
                "bindingId": BINDING_ID,
                "requestedHeadSha": HEAD_SHA,
                "providerBody": {"state": "merged"},
            },
        ),
        (
            EffectOperation.MERGE_INTEGRATION_MR,
            merge_effect_subject(BINDING_ID, HEAD_SHA),
            {"bindingId": BINDING_ID, "requestedHeadSha": "A" * 40},
        ),
    ],
)
def test_integration_effect_payload_rejects_missing_or_unrelated_facts(
    operation: EffectOperation,
    subject_key: str,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SourceControlEffectDto.model_validate(
            {
                "id": "60000000-0000-0000-0000-000000000504",
                "effect_key": "source-control:review-payload",
                "operation": operation,
                "subject_key": subject_key,
                "payload": payload,
                "work_item_id": WORK_ITEM_ID,
                "requirement_id": "40000000-0000-0000-0000-000000000501",
                "repository_id": "10000000-0000-0000-0000-000000000501",
                "work_item_number": None,
                "branch_name": None,
                "base_commit_sha": None,
                "request_fingerprint": "sha256:payload-review",
                "attempts": 0,
                "next_reconcile_at": None,
                "state": EffectState.PLANNED,
                "last_error_code": None,
                "callback_state": RequirementCallbackState.PENDING,
                "created_at": NOW,
                "updated_at": NOW,
                "completed_at": None,
            }
        )


def test_effect_payload_cannot_be_mutated_after_validation() -> None:
    effect = SourceControlEffectDto(
        id="60000000-0000-0000-0000-000000000505",
        effect_key="source-control:immutable-payload",
        operation=EffectOperation.CREATE_INTEGRATION_MR,
        subject_key=f"work-item:{WORK_ITEM_ID}",
        payload=CreateIntegrationMergeRequestEffectPayload(
            branchBindingId=BRANCH_BINDING_ID,
            headSha=HEAD_SHA,
        ),
        work_item_id=WORK_ITEM_ID,
        requirement_id="40000000-0000-0000-0000-000000000501",
        repository_id="10000000-0000-0000-0000-000000000501",
        work_item_number=None,
        branch_name=None,
        base_commit_sha=None,
        request_fingerprint="sha256:immutable-payload",
        attempts=0,
        next_reconcile_at=None,
        state=EffectState.PLANNED,
        last_error_code=None,
        callback_state=RequirementCallbackState.PENDING,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )

    mutable_view = cast(dict[str, object], effect.payload)
    with pytest.raises(TypeError):
        mutable_view["headSha"] = "b" * 40


@pytest.mark.parametrize(
    "payload",
    [
        MergeIntegrationMergeRequestEffectPayload(
            bindingId=BRANCH_BINDING_ID,
            requestedHeadSha=HEAD_SHA,
        ),
        MergeIntegrationMergeRequestEffectPayload(
            bindingId=BINDING_ID,
            requestedHeadSha="b" * 40,
        ),
    ],
)
def test_merge_effect_subject_must_match_payload_binding_and_head(
    payload: MergeIntegrationMergeRequestEffectPayload,
) -> None:
    with pytest.raises(ValidationError):
        SourceControlEffectDto(
            id="60000000-0000-0000-0000-000000000506",
            effect_key="source-control:subject-payload-match",
            operation=EffectOperation.MERGE_INTEGRATION_MR,
            subject_key=merge_effect_subject(BINDING_ID, HEAD_SHA),
            payload=payload,
            work_item_id=WORK_ITEM_ID,
            requirement_id="40000000-0000-0000-0000-000000000501",
            repository_id="10000000-0000-0000-0000-000000000501",
            work_item_number=None,
            branch_name=None,
            base_commit_sha=None,
            request_fingerprint="sha256:subject-payload-match",
            attempts=0,
            next_reconcile_at=None,
            state=EffectState.PLANNED,
            last_error_code=None,
            callback_state=RequirementCallbackState.PENDING,
            created_at=NOW,
            updated_at=NOW,
            completed_at=None,
        )
