import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from control_plane.app.modules.source_control.ports.gitlab import (
    BranchSnapshot,
    GitLabAccessDenied,
    GitLabBranchAlreadyExists,
    GitLabBranchNotFound,
    GitLabDefaultBranchNotFound,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
    SecretReferencePort,
)

_FULL_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")


@dataclass(frozen=True, slots=True)
class HttpxGitLabAdapter:
    client: httpx.Client
    secrets: SecretReferencePort

    def _headers(self, repository: GitLabRepositoryProfile) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.secrets.resolve(repository.credential_secret_ref)}

    @staticmethod
    def _decode_branch(response: httpx.Response) -> BranchSnapshot:
        try:
            payload = response.json()
            name = payload["name"]
            commit_sha = payload["commit"]["id"]
        except (KeyError, TypeError, ValueError):
            raise GitLabProviderUnavailable("GitLab returned an invalid branch response") from None
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(commit_sha, str)
            or _FULL_COMMIT_SHA.fullmatch(commit_sha) is None
        ):
            raise GitLabProviderUnavailable("GitLab returned an invalid branch response")
        return BranchSnapshot(name=name, commit_sha=commit_sha.lower())

    def get_branch(
        self,
        repository: GitLabRepositoryProfile,
        name: str,
    ) -> BranchSnapshot:
        project = quote(repository.project_id, safe="")
        branch = quote(name, safe="")
        try:
            response = self.client.get(
                f"/projects/{project}/repository/branches/{branch}",
                headers=self._headers(repository),
            )
        except httpx.HTTPError as error:
            raise GitLabProviderUnavailable("GitLab branch read is unavailable") from error
        if response.status_code in {401, 403}:
            raise GitLabAccessDenied("GitLab access denied")
        if response.status_code == 404:
            if name == repository.default_branch:
                raise GitLabDefaultBranchNotFound("GitLab default branch was not found")
            raise GitLabBranchNotFound("GitLab branch was not found")
        if response.status_code != 200:
            raise GitLabProviderUnavailable("GitLab branch read is unavailable")
        return self._decode_branch(response)

    def create_branch(
        self,
        repository: GitLabRepositoryProfile,
        *,
        name: str,
        ref_sha: str,
    ) -> BranchSnapshot:
        if _FULL_COMMIT_SHA.fullmatch(ref_sha) is None:
            raise GitLabResultUnknown("GitLab branch ref is invalid")
        project = quote(repository.project_id, safe="")
        try:
            response = self.client.post(
                f"/projects/{project}/repository/branches",
                params={"branch": name, "ref": ref_sha},
                headers=self._headers(repository),
            )
        except httpx.HTTPError as error:
            raise GitLabResultUnknown("GitLab branch creation result is unknown") from error
        if response.status_code in {401, 403}:
            raise GitLabAccessDenied("GitLab access denied")
        if response.status_code == 409:
            raise GitLabBranchAlreadyExists("GitLab branch already exists")
        if response.status_code != 201:
            if response.status_code >= 500:
                raise GitLabResultUnknown("GitLab branch creation result is unknown")
            raise GitLabProviderUnavailable("GitLab branch creation was rejected")
        return self._decode_branch(response)
