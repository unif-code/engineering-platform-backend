from dataclasses import dataclass

from control_plane.app.modules.source_control.application._integration_common import (
    TARGET_BRANCH as _TARGET_BRANCH,
)
from control_plane.app.modules.source_control.application._integration_merge_context import (
    _MergeAdmission,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    SourceControlDependencyUnavailable,
)
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
    reason_code: str


class _MergePreflightTransient(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _MergeExecutionBlocked(Exception):
    reason_code: str
    readback: GitLabMergeRequestSnapshot | None = None


class _MergeExecutionUnknown(Exception):
    pass


def _read_merge_provider_proof(
    admission: _MergeAdmission,
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
        raise _MergePreflightBlocked("PROJECT_PROFILE_UNSUPPORTED") from None
    except GitLabTargetBranchNotProtected:
        raise _MergePreflightBlocked("TARGET_BRANCH_NOT_PROTECTED") from None
    except GitLabBranchNotFound:
        raise _MergePreflightBlocked("TARGET_BRANCH_NOT_FOUND") from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _MergePreflightBlocked("REPOSITORY_NOT_AUTHORIZED") from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _MergePreflightTransient from None
    if (
        project.project_id != profile.project_id
        or project.project_path != profile.project_path
        or project.default_branch != profile.default_branch
        or project.merge_method != "merge"
    ):
        raise _MergePreflightBlocked("PROJECT_PROFILE_UNSUPPORTED")
    try:
        merge_request = gitlab.get_merge_request(
            profile,
            iid=admission.binding.merge_request_iid,
        )
    except GitLabMergeRequestNotFound:
        raise _MergePreflightBlocked("MR_CLOSED") from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _MergePreflightBlocked("REPOSITORY_NOT_AUTHORIZED") from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _MergePreflightTransient from None
    if (
        merge_request.project_id != admission.binding.external_project_id
        or merge_request.iid != admission.binding.merge_request_iid
        or merge_request.source_branch != admission.binding.source_branch
        or merge_request.target_branch != _TARGET_BRANCH
    ):
        raise _MergePreflightBlocked("MR_CONFLICT")
    if merge_request.state == "merged" and (
        merge_request.merge_commit_sha is None or merge_request.merged_at is None
    ):
        raise _MergePreflightTransient
    try:
        source: BranchSnapshot | None = gitlab.get_branch(
            profile,
            admission.binding.source_branch,
        )
    except GitLabBranchNotFound:
        if merge_request.state != "merged":
            raise _MergePreflightBlocked("BRANCH_BINDING_MISSING") from None
        source = None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _MergePreflightBlocked("REPOSITORY_NOT_AUTHORIZED") from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _MergePreflightTransient from None
    if source is not None:
        if source.name != admission.binding.source_branch:
            raise _MergePreflightBlocked("BRANCH_BINDING_MISSING")
        if merge_request.head_sha != source.commit_sha:
            raise _MergePreflightBlocked("HEAD_SHA_CHANGED")
    return _MergeProviderProof(
        project=project,
        source=source,
        merge_request=merge_request,
    )


def _provider_block_reason(proof: _MergeProviderProof) -> str | None:
    merge_request = proof.merge_request
    if merge_request.state == "merged":
        return "EXTERNAL_MERGE_DRIFT"
    if merge_request.state == "closed":
        return "MR_CLOSED"
    if merge_request.state == "locked":
        return "MR_CHECKS_BLOCKED"
    if merge_request.has_conflicts:
        return "MERGE_CONFLICT"
    if (
        merge_request.detailed_merge_status != "mergeable"
        or not merge_request.blocking_discussions_resolved
        or merge_request.head_pipeline_status != "success"
    ):
        return "MR_CHECKS_BLOCKED"
    return None


def _merge_exact_head(
    admission: _MergeAdmission,
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
        raise _MergeExecutionBlocked("HEAD_SHA_CHANGED") from None
    except GitLabMergeRequestBlocked:
        raise _MergeExecutionBlocked("MERGE_CONFLICT") from None
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
            raise _MergeExecutionBlocked("HEAD_SHA_CHANGED")
        if readback.state == "closed":
            raise _MergeExecutionBlocked("MR_CLOSED")
        raise _MergeExecutionUnknown
    try:
        source = gitlab.get_branch(
            admission.repository_profile,
            admission.binding.source_branch,
        )
    except GitLabBranchNotFound:
        raise _MergeExecutionBlocked(
            "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION",
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
        raise _MergeExecutionBlocked("HEAD_SHA_CHANGED", readback=readback)
    return _CompletedMergeProof(merge_request=readback, source=source)


__all__: list[str] = []
