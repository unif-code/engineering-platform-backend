from collections.abc import Callable

import httpx
import pytest

from control_plane.app.modules.source_control.adapters import HttpxGitLabAdapter
from control_plane.app.modules.source_control.ports import (
    GitLabAccessDenied,
    GitLabBranchConflict,
    GitLabDefaultBranchNotFound,
    GitLabRepositoryProfile,
    run_create_branch_saga,
)

BASE_SHA = "a" * 40
OTHER_SHA = "b" * 40
TASK_BRANCH = "feat/wi-42-source-control/编码"


class FakeSecrets:
    def resolve(self, reference: str) -> str:
        assert reference == "secret-ref:gitlab-token"
        return "test-only-token"


def _profile() -> GitLabRepositoryProfile:
    return GitLabRepositoryProfile(
        repository_id="gitlab-project-1",
        project_id="platform/backend",
        project_path="platform/backend",
        connection_ref="gitlab-dev",
        default_branch="main",
        credential_secret_ref="secret-ref:gitlab-token",
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        base_url="https://gitlab.example/api/v4",
        transport=httpx.MockTransport(handler),
        timeout=1,
    )


def _adapter(
    client: httpx.Client,
    *,
    connection_ref: str = "gitlab-dev",
) -> HttpxGitLabAdapter:
    return HttpxGitLabAdapter(
        client=client,
        secrets=FakeSecrets(),
        connection_ref=connection_ref,
    )


def test_gitlab_adapter_reads_main_creates_from_exact_sha_and_reads_back() -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.raw_path.decode(), dict(request.url.params)))
        assert request.headers["PRIVATE-TOKEN"] == "test-only-token"
        assert "test-only-token" not in str(request.url)
        if request.method == "GET" and request.url.path.endswith("/branches/main"):
            return httpx.Response(200, json={"name": "main", "commit": {"id": BASE_SHA}})
        if request.method == "POST":
            assert request.url.params["branch"] == TASK_BRANCH
            assert request.url.params["ref"] == BASE_SHA
            return httpx.Response(
                201,
                json={"name": TASK_BRANCH, "commit": {"id": BASE_SHA}},
            )
        return httpx.Response(
            200,
            json={"name": TASK_BRANCH, "commit": {"id": BASE_SHA}},
        )

    with _client(handler) as client:
        result = run_create_branch_saga(
            _adapter(client),
            _profile(),
            TASK_BRANCH,
        )

    assert result.commit_sha == BASE_SHA
    assert [method for method, _, _ in calls] == ["GET", "POST", "GET"]
    assert "%2F" in calls[0][1] or "platform/backend" not in calls[0][1]


@pytest.mark.parametrize("status", [401, 403])
def test_gitlab_adapter_maps_access_denial_without_response_body(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="sensitive provider response")

    with _client(handler) as client, pytest.raises(GitLabAccessDenied) as raised:
        _adapter(client).get_branch(
            _profile(),
            "main",
        )

    assert "sensitive provider response" not in str(raised.value)


def test_gitlab_adapter_maps_main_404_to_default_branch_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="private project detail")

    with _client(handler) as client, pytest.raises(GitLabDefaultBranchNotFound) as raised:
        _adapter(client).get_branch(
            _profile(),
            "main",
        )

    assert "private project detail" not in str(raised.value)


@pytest.mark.parametrize("post_result", ["timeout", "conflict"])
def test_unknown_or_conflicting_create_converges_through_exact_readback(
    post_result: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"name": "main", "commit": {"id": BASE_SHA}})
        if calls == 2 and post_result == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if calls == 2:
            return httpx.Response(409, text="already exists")
        return httpx.Response(
            200,
            json={"name": TASK_BRANCH, "commit": {"id": BASE_SHA}},
        )

    with _client(handler) as client:
        result = run_create_branch_saga(
            _adapter(client),
            _profile(),
            TASK_BRANCH,
        )

    assert result.commit_sha == BASE_SHA
    assert calls == 3


def test_malformed_successful_create_response_is_unknown_until_exact_readback() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"name": "main", "commit": {"id": BASE_SHA}})
        if calls == 2:
            return httpx.Response(201, content=b"created-but-unreadable")
        return httpx.Response(
            200,
            json={"name": TASK_BRANCH, "commit": {"id": BASE_SHA}},
        )

    with _client(handler) as client:
        result = run_create_branch_saga(
            _adapter(client),
            _profile(),
            TASK_BRANCH,
        )

    assert result.commit_sha == BASE_SHA
    assert calls == 3


def test_differing_readback_sha_is_a_branch_conflict() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"name": "main", "commit": {"id": BASE_SHA}})
        if calls == 2:
            return httpx.Response(201, json={"name": TASK_BRANCH, "commit": {"id": BASE_SHA}})
        return httpx.Response(
            200,
            json={"name": TASK_BRANCH, "commit": {"id": OTHER_SHA}},
        )

    with _client(handler) as client, pytest.raises(GitLabBranchConflict):
        run_create_branch_saga(
            _adapter(client),
            _profile(),
            TASK_BRANCH,
        )


def test_adapter_rejects_repository_for_a_different_authorized_connection() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"name": "main", "commit": {"id": BASE_SHA}})

    with _client(handler) as client, pytest.raises(GitLabAccessDenied):
        _adapter(client, connection_ref="gitlab-secondary").get_branch(_profile(), "main")

    assert calls == 0
