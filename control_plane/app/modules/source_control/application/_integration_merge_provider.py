from dataclasses import dataclass
from typing import Protocol

from control_plane.app.modules.source_control.application._integration_common import (
    TARGET_BRANCH as _TARGET_BRANCH,
)
from control_plane.app.modules.source_control.application._integration_snapshot import (
    has_valid_merge_fact_shape,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    MergeRequestBindingDto,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import (
    BranchSnapshot,
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabMergeRequestBlocked,
    GitLabMergeRequestHeadChanged,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
    GitLabProjectNotFound,
    GitLabProjectPolicyUnsupported,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
    GitLabTargetBranchNotProtected,
)


@dataclass(frozen=True, slots=True)
class _MergeProviderProof:
    project: GitLabProjectDeliveryProfile
    source: BranchSnapshot | None
    merge_request: GitLabMergeRequestSnapshot

    @property
    def current_head_sha(self) -> str:
        if self.source is not None:
            return self.source.commit_sha
        return self.merge_request.head_sha


@dataclass(frozen=True, slots=True)
class _CompletedMergeProof:
    merge_request: GitLabMergeRequestSnapshot
    source: BranchSnapshot


@dataclass(frozen=True, slots=True)
class _MergePreflightBlocked(Exception):
    reason_code: SourceControlReason


class _MergePreflightTransient(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _MergeExecutionBlocked(Exception):
    reason_code: SourceControlReason
    readback: GitLabMergeRequestSnapshot | None = None


class _MergeExecutionUnknown(Exception):
    pass


class _MergeProviderContext(Protocol):
    @property
    def repository_profile(self) -> GitLabRepositoryProfile: ...

    @property
    def binding(self) -> MergeRequestBindingDto: ...


def _read_merge_provider_proof(
    admission: _MergeProviderContext,
    *,
    dependencies: SourceControlDependencies,
) -> _MergeProviderProof:
    gitlab = dependencies.gitlab_merge_requests
    if gitlab is None:
        raise SourceControlDependencyUnavailable("Integration merge provider unavailable")
    profile = admission.repository_profile
    try:
        project = gitlab.get_project_delivery_profile(profile)
    except GitLabProjectPolicyUnsupported:
        raise _MergePreflightBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED) from None
    except GitLabTargetBranchNotProtected:
        raise _MergePreflightBlocked(SourceControlReason.TARGET_BRANCH_NOT_PROTECTED) from None
    except GitLabBranchNotFound:
        raise _MergePreflightBlocked(SourceControlReason.TARGET_BRANCH_NOT_FOUND) from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _MergePreflightBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _MergePreflightTransient from None
    if (
        project.project_id != profile.project_id
        or project.project_path != profile.project_path
        or profile.default_branch != "main"
        or project.default_branch != "main"
        or project.default_branch != profile.default_branch
        or project.merge_method != "merge"
    ):
        raise _MergePreflightBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED)
    try:
        merge_request = gitlab.get_merge_request(
            profile,
            iid=admission.binding.merge_request_iid,
        )
    except GitLabMergeRequestNotFound:
        raise _MergePreflightBlocked(SourceControlReason.MR_CLOSED) from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _MergePreflightBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _MergePreflightTransient from None
    if (
        merge_request.project_id != admission.binding.external_project_id
        or merge_request.iid != admission.binding.merge_request_iid
        or merge_request.source_branch != admission.binding.source_branch
        or merge_request.target_branch != _TARGET_BRANCH
    ):
        raise _MergePreflightBlocked(SourceControlReason.MR_CONFLICT)
    if not has_valid_merge_fact_shape(merge_request):
        raise _MergePreflightTransient
    try:
        source: BranchSnapshot | None = gitlab.get_branch(
            profile,
            admission.binding.source_branch,
        )
    except GitLabBranchNotFound:
        if merge_request.state not in {"merged", "closed"}:
            raise _MergePreflightBlocked(SourceControlReason.BRANCH_BINDING_MISSING) from None
        source = None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _MergePreflightBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _MergePreflightTransient from None
    if source is not None:
        if source.name != admission.binding.source_branch:
            raise _MergePreflightBlocked(SourceControlReason.BRANCH_BINDING_MISSING)
        if merge_request.state == "opened" and merge_request.head_sha != source.commit_sha:
            raise _MergePreflightBlocked(SourceControlReason.HEAD_SHA_CHANGED)
    return _MergeProviderProof(
        project=project,
        source=source,
        merge_request=merge_request,
    )


def _provider_block_reason(proof: _MergeProviderProof) -> SourceControlReason | None:
    merge_request = proof.merge_request
    if merge_request.state == "merged":
        return SourceControlReason.EXTERNAL_MERGE_DRIFT
    if merge_request.state == "closed":
        return SourceControlReason.MR_CLOSED
    if merge_request.state == "locked":
        return SourceControlReason.MR_CHECKS_BLOCKED
    if merge_request.has_conflicts:
        return SourceControlReason.MERGE_CONFLICT
    if (
        merge_request.detailed_merge_status != "mergeable"
        or not merge_request.blocking_discussions_resolved
        or merge_request.head_pipeline_status != "success"
    ):
        return SourceControlReason.MR_CHECKS_BLOCKED
    return None


def _merge_exact_head(
    admission: _MergeProviderContext,
    *,
    requested_head_sha: str,
    preflight: _MergeProviderProof,
    dependencies: SourceControlDependencies,
) -> _CompletedMergeProof:
    gitlab = dependencies.gitlab_merge_requests
    if gitlab is None:
        raise SourceControlDependencyUnavailable("Integration merge provider unavailable")
    try:
        gitlab.merge_merge_request(
            admission.repository_profile,
            iid=admission.binding.merge_request_iid,
            expected_head_sha=requested_head_sha,
        )
    except GitLabMergeRequestHeadChanged:
        raise _MergeExecutionBlocked(SourceControlReason.HEAD_SHA_CHANGED) from None
    except GitLabMergeRequestBlocked:
        raise _MergeExecutionBlocked(SourceControlReason.MERGE_CONFLICT) from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _MergeExecutionBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except GitLabMergeRequestNotFound:
        raise _MergeExecutionBlocked(SourceControlReason.MR_CLOSED) from None
    except (GitLabResultUnknown, GitLabProviderUnavailable):
        raise _MergeExecutionUnknown from None
    try:
        readback = gitlab.get_merge_request(
            admission.repository_profile,
            iid=admission.binding.merge_request_iid,
        )
    except (
        GitLabMergeRequestNotFound,
        GitLabAccessDenied,
        GitLabProjectNotFound,
        GitLabProviderUnavailable,
        GitLabResultUnknown,
    ):
        raise _MergeExecutionUnknown from None
    merged_coordinates_valid = (
        readback.state == "merged"
        and readback.project_id == preflight.merge_request.project_id
        and readback.iid == preflight.merge_request.iid
        and readback.source_branch == admission.binding.source_branch
        and readback.target_branch == _TARGET_BRANCH
        and readback.head_sha == requested_head_sha
        and readback.merge_commit_sha is not None
        and readback.merged_at is not None
    )
    if not merged_coordinates_valid:
        if readback.head_sha != requested_head_sha:
            raise _MergeExecutionBlocked(SourceControlReason.HEAD_SHA_CHANGED)
        if readback.state == "closed":
            raise _MergeExecutionBlocked(SourceControlReason.MR_CLOSED)
        raise _MergeExecutionUnknown
    try:
        source = gitlab.get_branch(
            admission.repository_profile,
            admission.binding.source_branch,
        )
    except GitLabBranchNotFound:
        raise _MergeExecutionBlocked(
            SourceControlReason.SOURCE_BRANCH_MISSING_AFTER_INTEGRATION,
            readback=readback,
        ) from None
    except (
        GitLabAccessDenied,
        GitLabProjectNotFound,
        GitLabProviderUnavailable,
        GitLabResultUnknown,
    ):
        raise _MergeExecutionUnknown from None
    if source.name != admission.binding.source_branch or source.commit_sha != requested_head_sha:
        raise _MergeExecutionBlocked(SourceControlReason.HEAD_SHA_CHANGED, readback=readback)
    return _CompletedMergeProof(merge_request=readback, source=source)


__all__: list[str] = []
