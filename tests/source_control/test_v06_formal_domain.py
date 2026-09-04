from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.app.modules.source_control.domain import (
    CreateFormalMergeRequestEffectPayload,
    EffectOperation,
    EffectState,
    MergeFormalMergeRequestEffectPayload,
    RequirementCallbackState,
    SourceControlEffectDto,
)

HEAD_SHA = "b" * 40
OTHER_HEAD_SHA = "c" * 40
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000301"
BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000301"
FORMAL_BINDING_ID = "98000000-0000-0000-0000-000000000601"
ACCEPTANCE_DECISION_ID = "99000000-0000-0000-0000-000000000601"
REVIEW_DECISION_ID = "99000000-0000-0000-0000-000000000602"
REQUEST_FINGERPRINT = "sha256:" + "1" * 64


def _effect(
    *,
    operation: EffectOperation,
    subject_key: str,
    payload: CreateFormalMergeRequestEffectPayload | MergeFormalMergeRequestEffectPayload,
) -> SourceControlEffectDto:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    return SourceControlEffectDto(
        id="97000000-0000-0000-0000-000000000601",
        effect_key="effect-key",
        operation=operation,
        subject_key=subject_key,
        payload=payload,
        work_item_id="50000000-0000-0000-0000-000000000301",
        requirement_id="40000000-0000-0000-0000-000000000301",
        repository_id="10000000-0000-0000-0000-000000000301",
        work_item_number=None,
        branch_name=None,
        base_commit_sha=None,
        request_fingerprint=REQUEST_FINGERPRINT,
        attempts=1,
        next_reconcile_at=None,
        state=EffectState.IN_FLIGHT,
        last_error_code=None,
        callback_state=RequirementCallbackState.PENDING,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


def test_formal_effect_shapes_bind_exact_work_item_binding_and_head() -> None:
    created = _effect(
        operation=EffectOperation.CREATE_FORMAL_MR,
        subject_key=f"formal-work-item:{WORK_ITEM_ID}:{HEAD_SHA}:{REQUEST_FINGERPRINT}",
        payload=CreateFormalMergeRequestEffectPayload(
            acceptanceDecisionId=ACCEPTANCE_DECISION_ID,
            branchBindingId=BRANCH_BINDING_ID,
            headSha=HEAD_SHA,
        ),
    )
    merged = _effect(
        operation=EffectOperation.MERGE_FORMAL_MR,
        subject_key=f"formal-mr:{FORMAL_BINDING_ID}:{HEAD_SHA}:{REQUEST_FINGERPRINT}",
        payload=MergeFormalMergeRequestEffectPayload(
            acceptanceDecisionId=ACCEPTANCE_DECISION_ID,
            bindingId=FORMAL_BINDING_ID,
            requestedHeadSha=HEAD_SHA,
            reviewDecisionId=REVIEW_DECISION_ID,
        ),
    )

    assert created.operation is EffectOperation.CREATE_FORMAL_MR
    assert merged.operation is EffectOperation.MERGE_FORMAL_MR


@pytest.mark.parametrize(
    ("operation", "subject_key", "payload"),
    [
        pytest.param(
            EffectOperation.CREATE_FORMAL_MR,
            (
                "formal-work-item:50000000-0000-0000-0000-000000000399:"
                f"{HEAD_SHA}:{REQUEST_FINGERPRINT}"
            ),
            CreateFormalMergeRequestEffectPayload(
                acceptanceDecisionId=ACCEPTANCE_DECISION_ID,
                branchBindingId=BRANCH_BINDING_ID,
                headSha=HEAD_SHA,
            ),
            id="wrong-create-subject",
        ),
        pytest.param(
            EffectOperation.CREATE_FORMAL_MR,
            f"formal-work-item:{WORK_ITEM_ID}:{OTHER_HEAD_SHA}:{REQUEST_FINGERPRINT}",
            CreateFormalMergeRequestEffectPayload(
                acceptanceDecisionId=ACCEPTANCE_DECISION_ID,
                branchBindingId=BRANCH_BINDING_ID,
                headSha=HEAD_SHA,
            ),
            id="wrong-create-head",
        ),
        pytest.param(
            EffectOperation.MERGE_FORMAL_MR,
            (f"formal-mr:98000000-0000-0000-0000-000000000699:{HEAD_SHA}:{REQUEST_FINGERPRINT}"),
            MergeFormalMergeRequestEffectPayload(
                acceptanceDecisionId=ACCEPTANCE_DECISION_ID,
                bindingId=FORMAL_BINDING_ID,
                requestedHeadSha=HEAD_SHA,
                reviewDecisionId=REVIEW_DECISION_ID,
            ),
            id="wrong-merge-binding",
        ),
        pytest.param(
            EffectOperation.MERGE_FORMAL_MR,
            f"formal-mr:{FORMAL_BINDING_ID}:{OTHER_HEAD_SHA}:{REQUEST_FINGERPRINT}",
            MergeFormalMergeRequestEffectPayload(
                acceptanceDecisionId=ACCEPTANCE_DECISION_ID,
                bindingId=FORMAL_BINDING_ID,
                requestedHeadSha=HEAD_SHA,
                reviewDecisionId=REVIEW_DECISION_ID,
            ),
            id="wrong-merge-head",
        ),
        pytest.param(
            EffectOperation.MERGE_FORMAL_MR,
            f"formal-mr:{FORMAL_BINDING_ID}:{HEAD_SHA}:sha256:{'2' * 64}",
            MergeFormalMergeRequestEffectPayload(
                acceptanceDecisionId=ACCEPTANCE_DECISION_ID,
                bindingId=FORMAL_BINDING_ID,
                requestedHeadSha=HEAD_SHA,
                reviewDecisionId=REVIEW_DECISION_ID,
            ),
            id="wrong-request-fingerprint",
        ),
    ],
)
def test_formal_effect_rejects_wrong_subject_binding_head_or_request(
    operation: EffectOperation,
    subject_key: str,
    payload: CreateFormalMergeRequestEffectPayload | MergeFormalMergeRequestEffectPayload,
) -> None:
    with pytest.raises(ValidationError):
        _effect(operation=operation, subject_key=subject_key, payload=payload)


@pytest.mark.parametrize(
    ("payload_type", "payload"),
    [
        pytest.param(
            CreateFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "branchBindingId": BRANCH_BINDING_ID,
                "headSha": "not-a-head",
            },
            id="create-invalid-head",
        ),
        pytest.param(
            CreateFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "branchBindingId": " ",
                "headSha": HEAD_SHA,
            },
            id="create-blank-binding",
        ),
        pytest.param(
            CreateFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": " ",
                "branchBindingId": BRANCH_BINDING_ID,
                "headSha": HEAD_SHA,
            },
            id="create-blank-acceptance-decision",
        ),
        pytest.param(
            MergeFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": "not-a-head",
                "reviewDecisionId": REVIEW_DECISION_ID,
            },
            id="merge-invalid-head",
        ),
        pytest.param(
            MergeFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "bindingId": " ",
                "requestedHeadSha": HEAD_SHA,
                "reviewDecisionId": REVIEW_DECISION_ID,
            },
            id="merge-blank-binding",
        ),
        pytest.param(
            MergeFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": " ",
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": HEAD_SHA,
                "reviewDecisionId": REVIEW_DECISION_ID,
            },
            id="merge-blank-acceptance-decision",
        ),
        pytest.param(
            MergeFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": HEAD_SHA,
                "reviewDecisionId": " ",
            },
            id="merge-blank-review-decision",
        ),
        pytest.param(
            CreateFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "branchBindingId": BRANCH_BINDING_ID,
                "headSha": HEAD_SHA,
                "unexpected": "value",
            },
            id="create-extra-field",
        ),
        pytest.param(
            CreateFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "headSha": HEAD_SHA,
            },
            id="create-missing-field",
        ),
        pytest.param(
            MergeFormalMergeRequestEffectPayload,
            {
                "acceptanceDecisionId": ACCEPTANCE_DECISION_ID,
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": HEAD_SHA,
                "reviewDecisionId": REVIEW_DECISION_ID,
                "unexpected": "value",
            },
            id="merge-extra-field",
        ),
        pytest.param(
            MergeFormalMergeRequestEffectPayload,
            {
                "bindingId": FORMAL_BINDING_ID,
                "requestedHeadSha": HEAD_SHA,
                "reviewDecisionId": REVIEW_DECISION_ID,
            },
            id="merge-missing-acceptance-decision",
        ),
    ],
)
def test_formal_payload_rejects_invalid_extra_or_missing_fields(
    payload_type: (
        type[CreateFormalMergeRequestEffectPayload] | type[MergeFormalMergeRequestEffectPayload]
    ),
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        payload_type.model_validate(payload)
