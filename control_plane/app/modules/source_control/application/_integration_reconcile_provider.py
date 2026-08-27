from dataclasses import dataclass

from control_plane.app.modules.source_control.application._integration_common import (
    REQUIREMENT_TYPE_PREFIXES,
    TARGET_BRANCH,
)
from control_plane.app.modules.source_control.application._integration_reconcile_context import (
    CreateReconciliationContext,
)
from control_plane.app.modules.source_control.application._integration_snapshot import (
    has_valid_merge_fact_shape,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import MergeRequestCreationOrigin
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import (
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectNotFound,
    GitLabProjectPolicyUnsupported,
    GitLabProviderUnavailable,
    GitLabResultUnknown,
    GitLabTargetBranchNotProtected,
)


@dataclass(frozen=True, slots=True)
class CreateProviderProof:
    snapshot: GitLabMergeRequestSnapshot
    creation_origin: MergeRequestCreationOrigin


@dataclass(frozen=True, slots=True)
class ReconciliationProviderBlocked(Exception):
    reason: SourceControlReason


class ReconciliationProviderUnknown(Exception):
    pass


def discover_create_candidate(
    context: CreateReconciliationContext,
    *,
    dependencies: SourceControlDependencies,
) -> GitLabMergeRequestSnapshot | None:
    gitlab = dependencies.gitlab_merge_requests
    if gitlab is None:
        raise ReconciliationProviderUnknown
    try:
        project = gitlab.get_project_delivery_profile(context.profile)
        candidates = gitlab.list_merge_requests(
            context.profile,
            source_branch=context.source_branch,
            target_branch=TARGET_BRANCH,
            state="all",
        )
    except GitLabProjectPolicyUnsupported:
        raise ReconciliationProviderBlocked(
            SourceControlReason.PROJECT_PROFILE_UNSUPPORTED
        ) from None
    except GitLabTargetBranchNotProtected:
        raise ReconciliationProviderBlocked(
            SourceControlReason.TARGET_BRANCH_NOT_PROTECTED
        ) from None
    except GitLabBranchNotFound:
        raise ReconciliationProviderBlocked(SourceControlReason.TARGET_BRANCH_NOT_FOUND) from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise ReconciliationProviderBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise ReconciliationProviderUnknown from None
    if (
        project.project_id != context.profile.project_id
        or project.project_path != context.profile.project_path
        or project.default_branch != "main"
        or project.merge_method != "merge"
    ):
        raise ReconciliationProviderBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED)
    if len(candidates) > 1:
        raise ReconciliationProviderBlocked(SourceControlReason.MR_CONFLICT)
    if not candidates:
        return None
    candidate = candidates[0]
    if (
        candidate.project_id != context.profile.project_id
        or candidate.source_branch != context.source_branch
        or candidate.target_branch != TARGET_BRANCH
    ):
        raise ReconciliationProviderBlocked(SourceControlReason.MR_CONFLICT)
    return candidate


def retry_create_merge_request(
    context: CreateReconciliationContext,
    *,
    dependencies: SourceControlDependencies,
) -> int:
    gitlab = dependencies.gitlab_merge_requests
    requirement = dependencies.requirement
    if gitlab is None or requirement is None:
        raise ReconciliationProviderUnknown
    binding_context = requirement.binding_context(context.effect.work_item_id)
    if (
        binding_context.work_item_id != context.effect.work_item_id
        or binding_context.requirement_id != context.effect.requirement_id
        or binding_context.workspace_id != context.workspace_id
        or binding_context.repository_id != context.effect.repository_id
        or binding_context.requirement_type not in REQUIREMENT_TYPE_PREFIXES
    ):
        raise ReconciliationProviderBlocked(SourceControlReason.MR_CONFLICT)
    try:
        locator = gitlab.create_merge_request(
            context.profile,
            source_branch=context.source_branch,
            target_branch=TARGET_BRANCH,
            expected_head_sha=context.payload.head_sha,
            title=(f"{binding_context.requirement_type}: integrate {context.effect.work_item_id}"),
            description=(
                f"Requirement: {context.effect.requirement_id}\n"
                f"Work-Item: {context.effect.work_item_id}\n"
                f"Source-Control-Effect: {context.effect.id}"
            ),
        )
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise ReconciliationProviderBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise ReconciliationProviderUnknown from None
    if (
        locator.project_id != context.profile.project_id
        or locator.source_branch != context.source_branch
        or locator.target_branch != TARGET_BRANCH
    ):
        raise ReconciliationProviderUnknown
    return locator.iid


def prove_create_merge_request(
    context: CreateReconciliationContext,
    *,
    iid: int,
    creation_origin: MergeRequestCreationOrigin,
    dependencies: SourceControlDependencies,
) -> CreateProviderProof:
    gitlab = dependencies.gitlab_merge_requests
    if gitlab is None:
        raise ReconciliationProviderUnknown
    try:
        snapshot = gitlab.get_merge_request(context.profile, iid=iid)
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise ReconciliationProviderBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except (GitLabMergeRequestNotFound, GitLabProviderUnavailable, GitLabResultUnknown):
        raise ReconciliationProviderUnknown from None
    if (
        snapshot.project_id != context.profile.project_id
        or snapshot.iid != iid
        or snapshot.source_branch != context.source_branch
        or snapshot.target_branch != TARGET_BRANCH
    ):
        raise ReconciliationProviderBlocked(SourceControlReason.MR_CONFLICT)
    if not has_valid_merge_fact_shape(snapshot):
        raise ReconciliationProviderUnknown
    if snapshot.state == "locked":
        raise ReconciliationProviderUnknown
    if snapshot.state == "opened":
        try:
            source = gitlab.get_branch(context.profile, context.source_branch)
        except (GitLabAccessDenied, GitLabProjectNotFound):
            raise ReconciliationProviderBlocked(
                SourceControlReason.REPOSITORY_NOT_AUTHORIZED
            ) from None
        except (GitLabBranchNotFound, GitLabProviderUnavailable, GitLabResultUnknown):
            raise ReconciliationProviderUnknown from None
        if source.name != context.source_branch:
            raise ReconciliationProviderBlocked(SourceControlReason.MR_CONFLICT)
        if snapshot.head_sha != source.commit_sha:
            raise ReconciliationProviderUnknown
    return CreateProviderProof(
        snapshot=snapshot,
        creation_origin=creation_origin,
    )


__all__: list[str] = []
