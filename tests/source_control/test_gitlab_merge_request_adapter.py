from collections.abc import Callable

import httpx
import pytest

from control_plane.app.modules.source_control.adapters import HttpxGitLabMergeRequestAdapter
from control_plane.app.modules.source_control.ports import (
    GitLabAccessDenied,
    GitLabMergeRequestBlocked,
    GitLabMergeRequestHeadChanged,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectNotFound,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
    GitLabResultUnknown,
)

HEAD_SHA = "a" * 40
TASK_BRANCH = "feat/wi-42-source-control"


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


def _adapter(client: httpx.Client) -> HttpxGitLabMergeRequestAdapter:
    return HttpxGitLabMergeRequestAdapter(
        client=client,
        secrets=FakeSecrets(),
        connection_ref="gitlab-dev",
    )


def _merge_request(
    *,
    iid: int = 17,
    source_branch: str = TASK_BRANCH,
    target_branch: str = "dev",
    sha: str = HEAD_SHA,
    state: str = "opened",
    detailed_merge_status: str = "mergeable",
    has_conflicts: bool = False,
    blocking_discussions_resolved: bool = True,
    pipeline_status: str | None = "success",
) -> dict[str, object]:
    return {
        "iid": iid,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "sha": sha,
        "state": state,
        "detailed_merge_status": detailed_merge_status,
        "has_conflicts": has_conflicts,
        "blocking_discussions_resolved": blocking_discussions_resolved,
        "head_pipeline": None if pipeline_status is None else {"status": pipeline_status},
        "merge_commit_sha": None,
        "merge_user": None,
        "merged_at": None,
        "diff_refs": {"head_sha": sha},
    }


def run_create_integration_mr(
    adapter: HttpxGitLabMergeRequestAdapter,
    *,
    repository: GitLabRepositoryProfile,
    source_branch: str,
    expected_head_sha: str,
    title: str,
    description: str,
) -> GitLabMergeRequestSnapshot:
    profile = adapter.get_project_delivery_profile(repository)
    assert profile.default_branch == "main"
    adapter.get_branch(repository, "dev")
    assert adapter.list_merge_requests(
        repository,
        source_branch=source_branch,
        target_branch="dev",
    ) == []
    created = adapter.create_merge_request(
        repository,
        source_branch=source_branch,
        target_branch="dev",
        expected_head_sha=expected_head_sha,
        title=title,
        description=description,
    )
    return adapter.get_merge_request(repository, iid=created.iid)


def test_adapter_lists_then_creates_and_reads_exact_integration_mr() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode().split("?", maxsplit=1)[0]
        calls.append((request.method, raw_path.removeprefix("/api/v4")))
        assert request.headers["PRIVATE-TOKEN"] == "test-only-token"
        assert "test-only-token" not in str(request.url)
        if request.method == "GET" and request.url.path.endswith("/projects/platform/backend"):
            return httpx.Response(
                200,
                json={
                    "path_with_namespace": "platform/backend",
                    "default_branch": "main",
                    "merge_method": "merge",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/branches/dev"):
            return httpx.Response(
                200,
                json={"name": "dev", "commit": {"id": HEAD_SHA}, "protected": True},
            )
        if request.method == "GET" and request.url.path.endswith("/merge_requests"):
            assert dict(request.url.params) == {
                "state": "opened",
                "source_branch": TASK_BRANCH,
                "target_branch": "dev",
            }
            return httpx.Response(200, json=[])
        if request.method == "POST":
            assert dict(request.url.params) == {
                "source_branch": TASK_BRANCH,
                "target_branch": "dev",
                "title": "feat: integrate WI-42",
                "description": "Platform-Work-Item: 42",
                "squash": "false",
                "remove_source_branch": "false",
            }
            return httpx.Response(201, json=_merge_request())
        if request.method == "GET" and request.url.path.endswith("/merge_requests/17"):
            return httpx.Response(200, json=_merge_request())
        raise AssertionError(f"unexpected GitLab request: {request.method} {request.url.path}")

    with _client(handler) as client:
        result = run_create_integration_mr(
            _adapter(client),
            repository=_profile(),
            source_branch=TASK_BRANCH,
            expected_head_sha=HEAD_SHA,
            title="feat: integrate WI-42",
            description="Platform-Work-Item: 42",
        )

    assert result.source_branch == TASK_BRANCH
    assert result.target_branch == "dev"
    assert result.head_sha == HEAD_SHA
    assert calls == [
        ("GET", "/projects/platform%2Fbackend"),
        ("GET", "/projects/platform%2Fbackend/repository/branches/dev"),
        ("GET", "/projects/platform%2Fbackend/merge_requests"),
        ("POST", "/projects/platform%2Fbackend/merge_requests"),
        ("GET", "/projects/platform%2Fbackend/merge_requests/17"),
    ]


def test_adapter_merges_with_exact_sha_without_squash_or_source_removal() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_merge_request(state="merged"))

    with _client(handler) as client:
        merged = _adapter(client).merge_merge_request(
            _profile(),
            iid=17,
            expected_head_sha=HEAD_SHA,
        )

    merge_request = requests[0]
    assert merge_request.method == "PUT"
    assert merge_request.url.params["sha"] == HEAD_SHA
    assert merge_request.url.params["squash"] == "false"
    assert merge_request.url.params["should_remove_source_branch"] == "false"
    assert "auto_merge" not in merge_request.url.params
    assert merged.state == "merged"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, GitLabAccessDenied),
        (403, GitLabAccessDenied),
        (404, GitLabProjectNotFound),
        (500, GitLabProviderUnavailable),
    ],
)
def test_project_profile_normalizes_read_failures_without_response_body(
    status: int,
    error_type: type[Exception],
) -> None:
    with (
        _client(lambda _request: httpx.Response(status, text="private provider body")) as client,
        pytest.raises(error_type) as raised,
    ):
        _adapter(client).get_project_delivery_profile(_profile())

    assert "private provider body" not in str(raised.value)


@pytest.mark.parametrize(
    ("method", "status", "error_type"),
    [
        ("GET", 404, GitLabMergeRequestNotFound),
        ("PUT", 404, GitLabMergeRequestNotFound),
        ("PUT", 405, GitLabMergeRequestBlocked),
        ("PUT", 409, GitLabMergeRequestHeadChanged),
        ("PUT", 422, GitLabMergeRequestBlocked),
        ("PUT", 500, GitLabResultUnknown),
    ],
)
def test_merge_request_reads_and_writes_normalize_provider_failures(
    method: str,
    status: int,
    error_type: type[Exception],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        return httpx.Response(status, text="private provider body")

    with _client(handler) as client, pytest.raises(error_type) as raised:
        adapter = _adapter(client)
        if method == "GET":
            adapter.get_merge_request(_profile(), iid=17)
        else:
            adapter.merge_merge_request(_profile(), iid=17, expected_head_sha=HEAD_SHA)

    assert "private provider body" not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        _merge_request(sha="b" * 40),
        {**_merge_request(), "source_branch": "other-source"},
        {**_merge_request(), "target_branch": "main"},
        {**_merge_request(), "diff_refs": {}},
    ],
)
def test_create_merge_request_treats_malformed_or_non_exact_write_result_as_unknown(
    payload: dict[str, object],
) -> None:
    with (
        _client(lambda _request: httpx.Response(201, json=payload)) as client,
        pytest.raises(GitLabResultUnknown),
    ):
        _adapter(client).create_merge_request(
            _profile(),
            source_branch=TASK_BRANCH,
            target_branch="dev",
            expected_head_sha=HEAD_SHA,
            title="feat: integrate WI-42",
            description="Platform-Work-Item: 42",
        )


@pytest.mark.parametrize(
    "payload",
    [
        _merge_request(detailed_merge_status="checking"),
        _merge_request(has_conflicts=True),
        _merge_request(blocking_discussions_resolved=False),
        _merge_request(pipeline_status="failed"),
    ],
)
def test_merge_rejects_provider_merge_checks_that_are_not_ready(payload: dict[str, object]) -> None:
    with (
        _client(lambda _request: httpx.Response(200, json=payload)) as client,
        pytest.raises(GitLabMergeRequestBlocked),
    ):
        _adapter(client).merge_merge_request(_profile(), iid=17, expected_head_sha=HEAD_SHA)


def test_list_merge_requests_rejects_multiple_exact_candidates() -> None:
    payload = [_merge_request(iid=17), _merge_request(iid=18)]
    with (
        _client(lambda _request: httpx.Response(200, json=payload)) as client,
        pytest.raises(GitLabResultUnknown),
    ):
        _adapter(client).list_merge_requests(
            _profile(),
            source_branch=TASK_BRANCH,
            target_branch="dev",
        )


def test_target_branch_must_be_protected_and_project_must_use_merge_method() -> None:
    responses = [
        httpx.Response(
            200,
            json={
                "path_with_namespace": "platform/backend",
                "default_branch": "main",
                "merge_method": "rebase_merge",
            },
        ),
        httpx.Response(
            200,
            json={"name": "dev", "commit": {"id": HEAD_SHA}, "protected": False},
        ),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    with _client(handler) as client:
        adapter = _adapter(client)
        with pytest.raises(GitLabProviderUnavailable):
            adapter.get_project_delivery_profile(_profile())
        with pytest.raises(GitLabProviderUnavailable):
            adapter.get_branch(_profile(), "dev")


def test_write_timeout_is_unknown_and_never_leaks_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("token=test-only-token", request=request)

    with _client(handler) as client, pytest.raises(GitLabResultUnknown) as raised:
        _adapter(client).create_merge_request(
            _profile(),
            source_branch=TASK_BRANCH,
            target_branch="dev",
            expected_head_sha=HEAD_SHA,
            title="feat: integrate WI-42",
            description="Platform-Work-Item: 42",
        )

    assert "test-only-token" not in str(raised.value)
