from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.source_control import (
    SourceControlDependencies,
    list_authorized_repositories,
)
from control_plane.app.modules.source_control.api.dto import (
    AuthorizedRepositoryListResponseDto,
    AuthorizedRepositoryResponseDto,
)
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
)

REPOSITORY_CHOICE_CAPABILITY = "requirement.create"
_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)


@dataclass(frozen=True, slots=True)
class SourceControlQueryRuntime:
    engine: Engine
    dependencies: SourceControlDependencies


def create_repository_query_router(
    runtime_provider: Callable[[], SourceControlQueryRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workspaces", tags=["source-control"])

    @router.get(
        "/{workspaceId}/repositories",
        operation_id="source_control_authorized_repositories_list",
        response_model=AuthorizedRepositoryListResponseDto,
        responses=_RESPONSES,
    )
    def authorized_repositories_list(
        workspace_id: Annotated[UUID, Path(alias="workspaceId")],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> AuthorizedRepositoryListResponseDto | Response:
        resolved_workspace_id = str(workspace_id)
        capability_guard(
            principal,
            REPOSITORY_CHOICE_CAPABILITY,
            resolved_workspace_id,
        )
        try:
            runtime = runtime_provider()
            with runtime.engine.connect() as db:
                summaries = list_authorized_repositories(
                    db,
                    workspace_id=resolved_workspace_id,
                    dependencies=runtime.dependencies,
                )
        except SQLAlchemyError:
            return problem_response(503, "Source Control repository query unavailable")
        return AuthorizedRepositoryListResponseDto(
            items=[AuthorizedRepositoryResponseDto.from_domain(summary) for summary in summaries]
        )

    return router
