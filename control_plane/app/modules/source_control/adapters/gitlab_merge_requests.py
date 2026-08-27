import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import httpx

from control_plane.app.modules.source_control.ports.gitlab import (
    BranchSnapshot,
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
    SecretReferencePort,
)
from control_plane.app.modules.source_control.ports.merge_requests import (
    GitLabMergeRequestBlocked,
    GitLabMergeRequestHeadChanged,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
    GitLabProjectNotFound,
)

_FULL_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
_MERGEABLE_PIPELINE_STATUS = "success"
_MR_LIST_PAGE_SIZE = 100
_MR_LIST_MAX_PAGES = 1000


@dataclass(frozen=True, slots=True)
class HttpxGitLabMergeRequestAdapter:
    client: httpx.Client
    secrets: SecretReferencePort
    connection_ref: str

    def _headers(self, repository: GitLabRepositoryProfile) -> dict[str, str]:
        if repository.connection_ref != self.connection_ref:
            raise GitLabAccessDenied("GitLab connection is not authorized for the repository")
        return {"PRIVATE-TOKEN": self.secrets.resolve(repository.credential_secret_ref)}

    @staticmethod
    def _project(repository: GitLabRepositoryProfile) -> str:
        return quote(repository.project_id, safe="")

    @staticmethod
    def _sha(value: object) -> str:
        if not isinstance(value, str) or _FULL_COMMIT_SHA.fullmatch(value) is None:
            raise GitLabProviderUnavailable("GitLab returned an invalid commit SHA")
        return value.lower()

    @staticmethod
    def _iid(value: int) -> None:
        if value <= 0:
            raise ValueError("GitLab merge request IID must be positive")

    def get_project_delivery_profile(
        self,
        repository: GitLabRepositoryProfile,
    ) -> GitLabProjectDeliveryProfile:
        try:
            response = self.client.get(
                f"/projects/{self._project(repository)}",
                headers=self._headers(repository),
            )
        except httpx.HTTPError:
            raise GitLabProviderUnavailable("GitLab project read is unavailable") from None
        if response.status_code in {401, 403}:
            raise GitLabAccessDenied("GitLab access denied")
        if response.status_code == 404:
            raise GitLabProjectNotFound("GitLab project was not found")
        if response.status_code != 200:
            raise GitLabProviderUnavailable("GitLab project read is unavailable")
        try:
            payload = cast(dict[str, Any], response.json())
            project_path = payload["path_with_namespace"]
            default_branch = payload["default_branch"]
            merge_method = payload["merge_method"]
        except (KeyError, TypeError, ValueError):
            raise GitLabProviderUnavailable("GitLab returned an invalid project response") from None
        if (
            not isinstance(project_path, str)
            or project_path != repository.project_path
            or not isinstance(default_branch, str)
            or default_branch != repository.default_branch
            or merge_method not in {"merge", "rebase_merge", "ff"}
            or merge_method != "merge"
        ):
            raise GitLabProviderUnavailable("GitLab project delivery policy is invalid")
        return GitLabProjectDeliveryProfile(
            project_id=repository.project_id,
            project_path=project_path,
            default_branch=default_branch,
            merge_method=merge_method,
        )

    def get_branch(
        self,
        repository: GitLabRepositoryProfile,
        name: str,
    ) -> BranchSnapshot:
        branch = quote(name, safe="")
        try:
            response = self.client.get(
                f"/projects/{self._project(repository)}/repository/branches/{branch}",
                headers=self._headers(repository),
            )
        except httpx.HTTPError:
            raise GitLabProviderUnavailable("GitLab branch read is unavailable") from None
        if response.status_code in {401, 403}:
            raise GitLabAccessDenied("GitLab access denied")
        if response.status_code == 404:
            raise GitLabBranchNotFound("GitLab branch was not found")
        if response.status_code != 200:
            raise GitLabProviderUnavailable("GitLab branch read is unavailable")
        try:
            payload = cast(dict[str, Any], response.json())
            observed_name = payload["name"]
            commit_sha = payload["commit"]["id"]
            protected = payload["protected"]
        except (KeyError, TypeError, ValueError):
            raise GitLabProviderUnavailable("GitLab returned an invalid branch response") from None
        if observed_name != name or not isinstance(protected, bool):
            raise GitLabProviderUnavailable("GitLab returned an invalid branch response")
        if name == "dev" and not protected:
            raise GitLabProviderUnavailable("GitLab target branch is not protected")
        try:
            return BranchSnapshot(name=observed_name, commit_sha=self._sha(commit_sha))
        except GitLabProviderUnavailable:
            raise GitLabProviderUnavailable("GitLab returned an invalid branch response") from None

    def list_merge_requests(
        self,
        repository: GitLabRepositoryProfile,
        *,
        source_branch: str,
        target_branch: str,
    ) -> list[GitLabMergeRequestSnapshot]:
        page = 1
        seen_pages: set[int] = set()
        snapshots: list[GitLabMergeRequestSnapshot] = []
        while True:
            if page in seen_pages or len(seen_pages) >= _MR_LIST_MAX_PAGES:
                raise GitLabProviderUnavailable("GitLab merge request pagination is invalid")
            seen_pages.add(page)
            try:
                response = self.client.get(
                    f"/projects/{self._project(repository)}/merge_requests",
                    params={
                        "state": "opened",
                        "source_branch": source_branch,
                        "target_branch": target_branch,
                        "per_page": _MR_LIST_PAGE_SIZE,
                        "page": page,
                    },
                    headers=self._headers(repository),
                )
            except httpx.HTTPError:
                raise GitLabProviderUnavailable(
                    "GitLab merge request list is unavailable"
                ) from None
            if response.status_code in {401, 403}:
                raise GitLabAccessDenied("GitLab access denied")
            if response.status_code == 404:
                raise GitLabProjectNotFound("GitLab project was not found")
            if response.status_code != 200:
                raise GitLabProviderUnavailable("GitLab merge request list is unavailable")
            try:
                payload = response.json()
            except ValueError:
                raise GitLabProviderUnavailable(
                    "GitLab returned an invalid merge request list"
                ) from None
            if not isinstance(payload, list):
                raise GitLabProviderUnavailable("GitLab returned an invalid merge request list")
            try:
                snapshots.extend(
                    self._decode_merge_request(
                        item,
                        repository=repository,
                        source_branch=source_branch,
                        target_branch=target_branch,
                    )
                    for item in payload
                )
            except GitLabProviderUnavailable:
                raise GitLabProviderUnavailable(
                    "GitLab returned an invalid merge request list"
                ) from None
            if len(snapshots) > 1:
                raise GitLabResultUnknown("GitLab merge request result is ambiguous")
            next_page = response.headers.get("X-Next-Page", "")
            if next_page == "":
                return snapshots
            if not next_page.isdecimal() or int(next_page) <= 0:
                raise GitLabProviderUnavailable("GitLab merge request pagination is invalid")
            page = int(next_page)

    def create_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        source_branch: str,
        target_branch: str,
        expected_head_sha: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestSnapshot:
        expected_head_sha = self._write_sha(expected_head_sha)
        try:
            response = self.client.post(
                f"/projects/{self._project(repository)}/merge_requests",
                params={
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "title": title,
                    "description": description,
                    "squash": False,
                    "remove_source_branch": False,
                },
                headers=self._headers(repository),
            )
        except httpx.HTTPError:
            raise GitLabResultUnknown("GitLab merge request creation result is unknown") from None
        if response.status_code in {401, 403}:
            raise GitLabAccessDenied("GitLab access denied")
        if response.status_code == 404:
            raise GitLabProjectNotFound("GitLab project was not found")
        if response.status_code != 201:
            raise GitLabResultUnknown("GitLab merge request creation result is unknown")
        try:
            snapshot = self._decode_merge_request(
                response.json(),
                repository=repository,
                source_branch=source_branch,
                target_branch=target_branch,
            )
        except (GitLabProviderUnavailable, ValueError):
            raise GitLabResultUnknown("GitLab merge request creation result is unknown") from None
        if snapshot.head_sha != expected_head_sha:
            raise GitLabResultUnknown("GitLab merge request creation result is unknown")
        return snapshot

    def get_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        iid: int,
    ) -> GitLabMergeRequestSnapshot:
        self._iid(iid)
        try:
            response = self.client.get(
                f"/projects/{self._project(repository)}/merge_requests/{iid}",
                headers=self._headers(repository),
            )
        except httpx.HTTPError:
            raise GitLabProviderUnavailable("GitLab merge request read is unavailable") from None
        if response.status_code in {401, 403}:
            raise GitLabAccessDenied("GitLab access denied")
        if response.status_code == 404:
            raise GitLabMergeRequestNotFound("GitLab merge request was not found")
        if response.status_code != 200:
            raise GitLabProviderUnavailable("GitLab merge request read is unavailable")
        try:
            return self._decode_merge_request(response.json(), repository=repository)
        except (GitLabProviderUnavailable, ValueError):
            raise GitLabProviderUnavailable(
                "GitLab returned an invalid merge request response"
            ) from None

    def merge_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot:
        self._iid(iid)
        expected_head_sha = self._write_sha(expected_head_sha)
        try:
            response = self.client.put(
                f"/projects/{self._project(repository)}/merge_requests/{iid}/merge",
                params={
                    "sha": expected_head_sha,
                    "squash": False,
                    "should_remove_source_branch": False,
                },
                headers=self._headers(repository),
            )
        except httpx.HTTPError:
            raise GitLabResultUnknown("GitLab merge result is unknown") from None
        if response.status_code in {401, 403}:
            raise GitLabAccessDenied("GitLab access denied")
        if response.status_code == 404:
            raise GitLabMergeRequestNotFound("GitLab merge request was not found")
        if response.status_code == 409:
            raise GitLabMergeRequestHeadChanged("GitLab merge request head changed")
        if response.status_code in {405, 422}:
            raise GitLabMergeRequestBlocked("GitLab merge request is blocked")
        if response.status_code != 200:
            raise GitLabResultUnknown("GitLab merge result is unknown")
        try:
            snapshot = self._decode_merge_request(response.json(), repository=repository)
        except (GitLabProviderUnavailable, ValueError):
            raise GitLabResultUnknown("GitLab merge result is unknown") from None
        if snapshot.head_sha != expected_head_sha:
            raise GitLabMergeRequestHeadChanged("GitLab merge request head changed")
        if snapshot.state != "merged":
            self._require_mergeable(snapshot)
            raise GitLabResultUnknown("GitLab merge result is unknown")
        return snapshot

    @staticmethod
    def _write_sha(value: str) -> str:
        try:
            return HttpxGitLabMergeRequestAdapter._sha(value)
        except GitLabProviderUnavailable:
            raise ValueError("GitLab merge request SHA must be a full commit SHA") from None

    @staticmethod
    def _require_mergeable(snapshot: GitLabMergeRequestSnapshot) -> None:
        if (
            snapshot.state != "opened"
            or snapshot.detailed_merge_status != "mergeable"
            or snapshot.has_conflicts
            or not snapshot.blocking_discussions_resolved
            or snapshot.head_pipeline_status != _MERGEABLE_PIPELINE_STATUS
        ):
            raise GitLabMergeRequestBlocked("GitLab merge request is blocked")

    def _decode_merge_request(
        self,
        payload: object,
        *,
        repository: GitLabRepositoryProfile,
        source_branch: str | None = None,
        target_branch: str | None = None,
    ) -> GitLabMergeRequestSnapshot:
        if not isinstance(payload, dict):
            raise GitLabProviderUnavailable("GitLab returned an invalid merge request response")
        try:
            iid = payload["iid"]
            observed_source = payload["source_branch"]
            observed_target = payload["target_branch"]
            sha = payload["sha"]
            state = payload["state"]
            detailed_merge_status = payload["detailed_merge_status"]
            has_conflicts = payload["has_conflicts"]
            discussions_resolved = payload["blocking_discussions_resolved"]
            diff_head_sha = payload["diff_refs"]["head_sha"]
        except (KeyError, TypeError):
            raise GitLabProviderUnavailable(
                "GitLab returned an invalid merge request response"
            ) from None
        if (
            not isinstance(iid, int)
            or iid <= 0
            or not isinstance(observed_source, str)
            or not observed_source
            or not isinstance(observed_target, str)
            or not observed_target
            or state not in {"opened", "merged", "closed", "locked"}
            or not isinstance(detailed_merge_status, str)
            or not isinstance(has_conflicts, bool)
            or not isinstance(discussions_resolved, bool)
        ):
            raise GitLabProviderUnavailable("GitLab returned an invalid merge request response")
        if source_branch is not None and observed_source != source_branch:
            raise GitLabProviderUnavailable("GitLab returned an unexpected merge request source")
        if target_branch is not None and observed_target != target_branch:
            raise GitLabProviderUnavailable("GitLab returned an unexpected merge request target")
        try:
            head_sha = self._sha(sha)
            if self._sha(diff_head_sha) != head_sha:
                raise GitLabProviderUnavailable(
                    "GitLab returned an inconsistent merge request head"
                )
        except GitLabProviderUnavailable:
            raise GitLabProviderUnavailable(
                "GitLab returned an invalid merge request response"
            ) from None
        head_pipeline_status = self._pipeline_status(payload.get("head_pipeline"))
        merge_user_id = self._merge_user_id(payload.get("merge_user"))
        merge_commit_sha = self._optional_sha(payload.get("merge_commit_sha"))
        merged_at = payload.get("merged_at")
        try:
            return GitLabMergeRequestSnapshot(
                project_id=repository.project_id,
                iid=iid,
                source_branch=observed_source,
                target_branch=observed_target,
                head_sha=head_sha,
                state=state,
                detailed_merge_status=detailed_merge_status,
                has_conflicts=has_conflicts,
                blocking_discussions_resolved=discussions_resolved,
                head_pipeline_status=head_pipeline_status,
                merge_commit_sha=merge_commit_sha,
                merge_user_id=merge_user_id,
                merged_at=merged_at,
            )
        except (TypeError, ValueError):
            raise GitLabProviderUnavailable(
                "GitLab returned an invalid merge request response"
            ) from None

    @staticmethod
    def _pipeline_status(payload: object) -> str | None:
        if payload is None:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
            raise GitLabProviderUnavailable("GitLab returned an invalid merge request response")
        return cast(str, payload["status"])

    @staticmethod
    def _merge_user_id(payload: object) -> str | None:
        if payload is None:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise GitLabProviderUnavailable("GitLab returned an invalid merge request response")
        return str(payload["id"])

    def _optional_sha(self, value: object) -> str | None:
        if value is None:
            return None
        return self._sha(value)
