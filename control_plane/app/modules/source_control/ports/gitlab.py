from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class GitLabError(RuntimeError):
    pass


class GitLabAccessDenied(GitLabError):
    pass


class GitLabDefaultBranchNotFound(GitLabError):
    pass


class GitLabBranchNotFound(GitLabError):
    pass


class GitLabBranchAlreadyExists(GitLabError):
    pass


class GitLabProviderUnavailable(GitLabError):
    pass


class GitLabResultUnknown(GitLabError):
    pass


class GitLabBranchConflict(GitLabError):
    pass


class GitLabRepositoryProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository_id: str
    project_id: str
    project_path: str
    connection_ref: str
    default_branch: str
    credential_secret_ref: str


class BranchSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    commit_sha: str


class SecretReferencePort(Protocol):
    def resolve(self, reference: str) -> str: ...


class GitLabPort(Protocol):
    def validate_repository(self, repository: GitLabRepositoryProfile) -> None: ...

    def get_branch(
        self,
        repository: GitLabRepositoryProfile,
        name: str,
    ) -> BranchSnapshot: ...

    def create_branch(
        self,
        repository: GitLabRepositoryProfile,
        *,
        name: str,
        ref_sha: str,
    ) -> BranchSnapshot: ...


class SourceControlPolicyPort(Protocol):
    def next_reconcile_at(self, *, now: datetime, attempts: int) -> datetime: ...

    def webhook_replay_window(self) -> timedelta: ...


def create_and_verify_branch(
    gitlab: GitLabPort,
    repository: GitLabRepositoryProfile,
    *,
    branch_name: str,
    base_commit_sha: str,
) -> BranchSnapshot:
    uncertain = False
    try:
        gitlab.create_branch(
            repository,
            name=branch_name,
            ref_sha=base_commit_sha,
        )
        uncertain = True
    except (GitLabBranchAlreadyExists, GitLabResultUnknown):
        uncertain = True
    try:
        snapshot = gitlab.get_branch(repository, branch_name)
    except (GitLabAccessDenied, GitLabBranchNotFound, GitLabProviderUnavailable) as error:
        if uncertain:
            raise GitLabResultUnknown("GitLab branch result is unknown") from error
        raise
    if snapshot.commit_sha != base_commit_sha:
        raise GitLabBranchConflict("GitLab branch points to a different commit")
    return snapshot


def run_create_branch_saga(
    gitlab: GitLabPort,
    repository: GitLabRepositoryProfile,
    branch_name: str,
) -> BranchSnapshot:
    gitlab.validate_repository(repository)
    base = gitlab.get_branch(repository, repository.default_branch)
    return create_and_verify_branch(
        gitlab,
        repository,
        branch_name=branch_name,
        base_commit_sha=base.commit_sha,
    )
