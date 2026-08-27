from dataclasses import dataclass

from control_plane.app.modules.source_control.application._integration_common import (
    TARGET_BRANCH as _TARGET_BRANCH,
)
from control_plane.app.modules.source_control.application._integration_common import (
    AcquiredEffect as _AcquiredEffect,
)
from control_plane.app.modules.source_control.application._integration_common import (
    Admission as _Admission,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProviderPreflight as _ProviderPreflight,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProviderPreflightBlocked as _ProviderPreflightBlocked,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProviderPreflightTransient as _ProviderPreflightTransient,
)
from control_plane.app.modules.source_control.domain import MergeRequestCreationOrigin
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import (
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestPort,
    GitLabMergeRequestSnapshot,
    GitLabProjectNotFound,
    GitLabProjectPolicyUnsupported,
    GitLabProviderUnavailable,
    GitLabResultUnknown,
    GitLabTargetBranchNotProtected,
)


@dataclass(frozen=True, slots=True)
class _ProviderProof:
    snapshot: GitLabMergeRequestSnapshot
    creation_origin: MergeRequestCreationOrigin


@dataclass(frozen=True, slots=True)
class _ProviderBlocked(Exception):
    reason_code: SourceControlReason


class _ProviderUnknown(Exception):
    pass


def _read_provider_preflight(
    admission: _Admission,
    *,
    gitlab: GitLabMergeRequestPort,
) -> _ProviderPreflight:
    context = admission.context
    profile = admission.repository_profile
    assert context.task_branch is not None
    assert context.base_commit_sha is not None
    try:
        project = gitlab.get_project_delivery_profile(profile)
    except GitLabProjectPolicyUnsupported:
        raise _ProviderPreflightBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED) from None
    except GitLabTargetBranchNotProtected:
        raise _ProviderPreflightBlocked(SourceControlReason.TARGET_BRANCH_NOT_PROTECTED) from None
    except GitLabBranchNotFound:
        raise _ProviderPreflightBlocked(SourceControlReason.TARGET_BRANCH_NOT_FOUND) from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _ProviderPreflightBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _ProviderPreflightTransient from None
    try:
        source = gitlab.get_branch(profile, context.task_branch)
    except GitLabBranchNotFound:
        raise _ProviderPreflightBlocked(SourceControlReason.BRANCH_BINDING_MISSING) from None
    except GitLabAccessDenied:
        raise _ProviderPreflightBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    except (GitLabProviderUnavailable, GitLabResultUnknown):
        raise _ProviderPreflightTransient from None
    if (
        project.project_id != profile.project_id
        or project.project_path != profile.project_path
        or project.default_branch != profile.default_branch
        or project.merge_method != "merge"
        or source.name != context.task_branch
    ):
        raise _ProviderPreflightBlocked(SourceControlReason.PROJECT_PROFILE_UNSUPPORTED)
    if source.commit_sha == context.base_commit_sha:
        raise _ProviderPreflightBlocked(SourceControlReason.NO_DELIVERY_COMMIT)
    return _ProviderPreflight(source=source)


def _prove_created_or_adopted_merge_request(
    admission: _Admission,
    acquired: _AcquiredEffect,
    *,
    gitlab: GitLabMergeRequestPort,
) -> _ProviderProof:
    profile = admission.repository_profile
    context = admission.context
    effect = acquired.effect
    try:
        candidates = gitlab.list_merge_requests(
            profile,
            source_branch=admission.task_branch,
            target_branch=_TARGET_BRANCH,
            state="all",
        )
    except (GitLabResultUnknown, GitLabProviderUnavailable):
        raise _ProviderUnknown from None
    except (GitLabAccessDenied, GitLabProjectNotFound):
        raise _ProviderBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
    if len(candidates) > 1:
        raise _ProviderBlocked(SourceControlReason.MR_CONFLICT)
    if candidates:
        candidate = candidates[0]
        if (
            candidate.project_id != profile.project_id
            or candidate.source_branch != admission.task_branch
            or candidate.target_branch != _TARGET_BRANCH
        ):
            raise _ProviderBlocked(SourceControlReason.MR_CONFLICT)
        merge_request_iid = candidate.iid
        creation_origin = MergeRequestCreationOrigin.EXTERNAL_ADOPTED
    else:
        try:
            created = gitlab.create_merge_request(
                profile,
                source_branch=admission.task_branch,
                target_branch=_TARGET_BRANCH,
                expected_head_sha=acquired.payload.head_sha,
                title=(
                    f"{admission.binding_context.requirement_type}: "
                    f"integrate {context.work_item_id}"
                ),
                description=(
                    f"Requirement: {context.requirement_id}\n"
                    f"Work-Item: {context.work_item_id}\n"
                    f"Source-Control-Effect: {effect.id}"
                ),
            )
        except (GitLabAccessDenied, GitLabProjectNotFound):
            raise _ProviderBlocked(SourceControlReason.REPOSITORY_NOT_AUTHORIZED) from None
        except (GitLabResultUnknown, GitLabProviderUnavailable):
            raise _ProviderUnknown from None
        merge_request_iid = created.iid
        creation_origin = MergeRequestCreationOrigin.PLATFORM_CREATED
    try:
        readback = gitlab.get_merge_request(profile, iid=merge_request_iid)
        source_readback = gitlab.get_branch(profile, admission.task_branch)
    except (
        GitLabMergeRequestNotFound,
        GitLabBranchNotFound,
        GitLabAccessDenied,
        GitLabProjectNotFound,
        GitLabProviderUnavailable,
        GitLabResultUnknown,
    ):
        raise _ProviderUnknown from None
    if (
        readback.project_id != profile.project_id
        or readback.iid != merge_request_iid
        or readback.source_branch != admission.task_branch
        or readback.target_branch != _TARGET_BRANCH
        or source_readback.name != admission.task_branch
        or readback.head_sha != source_readback.commit_sha
        or readback.state == "locked"
    ):
        raise _ProviderUnknown
    return _ProviderProof(snapshot=readback, creation_origin=creation_origin)
