from types import SimpleNamespace
from typing import Any, cast

import pytest

from control_plane.app.modules.source_control.application.formal import _provider_merge
from control_plane.app.modules.source_control.ports import (
    GitLabAccessDenied,
    GitLabMergeRequestSnapshot,
    GitLabProviderUnavailable,
    GitLabRepositoryProfile,
)
from tests.source_control.test_v06_formal_application import (
    FakeFormalGitLab,
    FakeRequirementFormalDelivery,
    _admission,
    _dependencies,
)


@pytest.mark.parametrize(
    "later_error", [GitLabAccessDenied("later denial"), GitLabProviderUnavailable("later outage")]
)
def test_confirmed_merge_response_is_not_replaced_by_a_later_read_failure(
    later_error: Exception,
) -> None:
    class Provider(FakeFormalGitLab):
        reads = 0

        def get_merge_request(self, repository: object, *, iid: int) -> GitLabMergeRequestSnapshot:
            self.reads += 1
            if self.merged:
                raise later_error
            return super().get_merge_request(repository, iid=iid)

    admission = _admission()
    provider = Provider()
    dependencies = _dependencies(
        cast(Any, SimpleNamespace(runtime=object())),
        FakeRequirementFormalDelivery(admission),
        provider,
    )
    result = _provider_merge(
        admission,
        GitLabRepositoryProfile(
            repository_id=admission.repository_id,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            default_branch="main",
            credential_secret_ref="secret-ref:gitlab",
        ),
        {"merge_request_iid": 77, "external_project_id": "101"},
        actor_id="employee-1",
        dependencies=dependencies,
    )
    assert result.state == "merged"
    assert result.merge_commit_sha is not None and result.merged_at is not None
    assert provider.reads == provider.merged == 1
