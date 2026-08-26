from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.app.modules.requirement import RequirementType
from control_plane.app.modules.source_control import (
    BindingRequestEnvelope,
    EffectState,
    GitLabWebhookEnvelope,
    InvalidBranchName,
    InvalidEffectTransition,
    RepositoryAuthorizationState,
    RepositoryBranchBindingDto,
    RequirementCallbackState,
    SourceControlEffectDto,
    WorkspaceRepositoryDto,
    build_task_branch_name,
    transition_effect,
)

NOW = datetime(2026, 8, 26, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (EffectState.PLANNED, EffectState.IN_FLIGHT),
        (EffectState.PLANNED, EffectState.BLOCKED),
        (EffectState.IN_FLIGHT, EffectState.SUCCEEDED),
        (EffectState.IN_FLIGHT, EffectState.BLOCKED),
        (EffectState.IN_FLIGHT, EffectState.UNKNOWN),
        (EffectState.IN_FLIGHT, EffectState.RECONCILIATION),
        (EffectState.UNKNOWN, EffectState.RECONCILIATION),
        (EffectState.RECONCILIATION, EffectState.IN_FLIGHT),
        (EffectState.RECONCILIATION, EffectState.RECONCILIATION),
        (EffectState.RECONCILIATION, EffectState.UNKNOWN),
        (EffectState.RECONCILIATION, EffectState.SUCCEEDED),
        (EffectState.RECONCILIATION, EffectState.BLOCKED),
    ],
)
def test_effect_transition_matrix(current: EffectState, target: EffectState) -> None:
    assert transition_effect(current, target) is target


def test_terminal_effect_cannot_return_to_in_flight() -> None:
    with pytest.raises(InvalidEffectTransition, match="SUCCEEDED->IN_FLIGHT"):
        transition_effect(EffectState.SUCCEEDED, EffectState.IN_FLIGHT)


def test_branch_name_is_deterministic_and_unicode_safe() -> None:
    assert (
        build_task_branch_name(
            requirement_type=RequirementType.FEAT,
            work_item_number=42,
            title="创建 GitLab 分支 / HMAC 验签",
        )
        == "feat/wi-42-创建-gitlab-分支-hmac-验签"
    )


def test_branch_name_removes_git_ref_forbidden_sequences() -> None:
    name = build_task_branch_name(
        requirement_type=RequirementType.FIX,
        work_item_number=7,
        title=".. @{ lock.lock",
    )

    assert name == "fix/wi-7-lock-lock"
    assert ".." not in name
    assert "@{" not in name
    assert not name.endswith(".lock")


def test_branch_name_rejects_non_positive_work_item_numbers() -> None:
    with pytest.raises(InvalidBranchName, match="positive"):
        build_task_branch_name(
            requirement_type=RequirementType.CHORE,
            work_item_number=0,
            title="Invalid number",
        )


def test_workspace_repository_rejects_an_empty_repository_identity() -> None:
    with pytest.raises(ValidationError):
        WorkspaceRepositoryDto(
            repository_id="   ",
            workspace_id="workspace-1",
            provider="GITLAB",
            project_id="101",
            project_path="platform/backend",
            default_branch="main",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:gitlab-dev",
            webhook_signing_secret_ref=None,
            status=RepositoryAuthorizationState.AUTHORIZED,
            revision=1,
            created_at=NOW,
            updated_at=NOW,
        )


def test_branch_binding_is_immutable_after_remote_fact_is_recorded() -> None:
    binding = RepositoryBranchBindingDto(
        id="binding-1",
        work_item_id="work-item-1",
        requirement_id="requirement-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        work_item_number=42,
        base_commit_sha="a" * 40,
        branch_name="feat/wi-42-governed-branch",
        effect_id="effect-1",
        created_at=NOW,
    )

    with pytest.raises(ValidationError, match="frozen"):
        binding.branch_name = "feat/wi-42-mutated"


def test_domain_envelopes_validate_claims_effects_and_sanitized_webhooks() -> None:
    request = BindingRequestEnvelope(
        message_id="message-1",
        topic="requirement.repository-binding.requested",
        requirement_id="requirement-1",
        requirement_version=1,
        work_item_id="work-item-1",
        repository_id="repository-1",
        attempts=1,
    )
    effect = SourceControlEffectDto(
        id="effect-1",
        effect_key="source-control:create-task-branch:work-item-1",
        operation="CREATE_TASK_BRANCH",
        work_item_id=request.work_item_id,
        requirement_id=request.requirement_id,
        repository_id=request.repository_id,
        work_item_number=42,
        branch_name="feat/wi-42-governed-branch",
        base_commit_sha="a" * 40,
        request_fingerprint="sha256:" + "b" * 64,
        attempts=0,
        next_reconcile_at=None,
        state=EffectState.PLANNED,
        last_error_code=None,
        callback_state=RequirementCallbackState.PENDING,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )
    webhook = GitLabWebhookEnvelope(
        repository_id=request.repository_id,
        webhook_id="webhook-1",
        webhook_timestamp=NOW,
        payload_digest="sha256:" + "c" * 64,
        provider_event_uuid="provider-event-1",
        event_type="Push Hook",
        object_kind="push",
        project_id="101",
        ref="refs/heads/feat/wi-42-governed-branch",
        before_sha="d" * 40,
        after_sha="a" * 40,
        checkout_sha="a" * 40,
    )

    assert request.attempts == 1
    assert effect.state is EffectState.PLANNED
    assert effect.callback_state is RequirementCallbackState.PENDING
    assert webhook.payload_digest == "sha256:" + "c" * 64
