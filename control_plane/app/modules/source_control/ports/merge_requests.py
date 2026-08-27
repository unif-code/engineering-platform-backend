from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.app.modules.source_control.ports.gitlab import (
    BranchSnapshot,
    GitLabError,
    GitLabRepositoryProfile,
)


class GitLabProjectNotFound(GitLabError):
    pass


class GitLabProjectPolicyUnsupported(GitLabError):
    pass


class GitLabTargetBranchNotProtected(GitLabError):
    pass


class GitLabMergeRequestNotFound(GitLabError):
    pass


class GitLabMergeRequestHeadChanged(GitLabError):
    pass


class GitLabMergeRequestBlocked(GitLabError):
    pass


class GitLabProjectDeliveryProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str
    project_path: str
    default_branch: str
    merge_method: Literal["merge", "rebase_merge", "ff"]


class GitLabMergeRequestSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str
    iid: int
    source_branch: str
    target_branch: str
    head_sha: str
    state: Literal["opened", "merged", "closed", "locked"]
    detailed_merge_status: str
    has_conflicts: bool
    blocking_discussions_resolved: bool
    head_pipeline_status: str | None
    merge_commit_sha: str | None
    merge_user_id: str | None
    merged_at: datetime | None


class GitLabMergeRequestPort(Protocol):
    def get_project_delivery_profile(
        self,
        repository: GitLabRepositoryProfile,
    ) -> GitLabProjectDeliveryProfile: ...

    def get_branch(
        self,
        repository: GitLabRepositoryProfile,
        name: str,
    ) -> BranchSnapshot: ...

    def list_merge_requests(
        self,
        repository: GitLabRepositoryProfile,
        *,
        source_branch: str,
        target_branch: str,
        state: Literal["all"] = "all",
    ) -> list[GitLabMergeRequestSnapshot]: ...

    def create_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        source_branch: str,
        target_branch: str,
        expected_head_sha: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestSnapshot: ...

    def get_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        iid: int,
    ) -> GitLabMergeRequestSnapshot: ...

    def merge_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot: ...
