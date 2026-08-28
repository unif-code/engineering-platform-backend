from control_plane.app.modules.source_control.domain import (
    AuthorizedRepositorySummaryDto,
)
from control_plane.app.modules.source_control.ports import SourceControlRepository


def list_authorized_repositories(
    repository: SourceControlRepository,
    *,
    workspace_id: str,
) -> tuple[AuthorizedRepositorySummaryDto, ...]:
    return tuple(
        AuthorizedRepositorySummaryDto(
            repository_id=str(row["id"]),
            provider=row["provider"],
            project_path=row["project_path"],
            default_branch=row["default_branch"],
        )
        for row in repository.authorized_repositories(workspace_id)
    )
