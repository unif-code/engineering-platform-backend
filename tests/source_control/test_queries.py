from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from control_plane.app.modules.source_control import (
    AuthorizedRepositorySummaryDto,
    list_authorized_repositories,
)

WORKSPACE_ID = "20000000-0000-0000-0000-000000000401"


class StubRepository:
    def authorized_repositories(self, workspace_id: str) -> list[dict[str, Any]]:
        assert workspace_id == WORKSPACE_ID
        return [
            {
                "id": "10000000-0000-0000-0000-000000000402",
                "provider": "GITLAB",
                "project_path": "platform/alpha",
                "default_branch": "main",
                "connection_ref": "must-not-escape",
                "credential_secret_ref": "must-not-escape",
                "webhook_signing_secret_ref": "must-not-escape",
            }
        ]


def test_list_authorized_repositories_returns_only_the_public_summary() -> None:
    summaries = list_authorized_repositories(
        object(),  # type: ignore[arg-type]
        workspace_id=WORKSPACE_ID,
        dependencies=SimpleNamespace(  # type: ignore[arg-type]
            repository_factory=lambda _db: StubRepository()
        ),
    )

    assert summaries == (
        AuthorizedRepositorySummaryDto(
            repository_id="10000000-0000-0000-0000-000000000402",
            provider="GITLAB",
            project_path="platform/alpha",
            default_branch="main",
        ),
    )
    assert summaries[0].model_dump() == {
        "repository_id": "10000000-0000-0000-0000-000000000402",
        "provider": "GITLAB",
        "project_path": "platform/alpha",
        "default_branch": "main",
    }


def test_authorized_repository_summary_is_immutable_and_rejects_private_fields() -> None:
    summary = AuthorizedRepositorySummaryDto(
        repository_id="10000000-0000-0000-0000-000000000402",
        provider="GITLAB",
        project_path="platform/alpha",
        default_branch="main",
    )

    with pytest.raises(ValidationError):
        summary.project_path = "platform/changed"
    with pytest.raises(ValidationError):
        AuthorizedRepositorySummaryDto.model_validate(
            {
                **summary.model_dump(),
                "credential_secret_ref": "secret-ref:must-not-escape",
            }
        )
