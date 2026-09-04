from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.modules.source_control import (
    AgentDeliveryDependencyUnavailable,
    AgentPushNotFound,
    get_agent_delivery,
)
from control_plane.app.modules.source_control.api.dto import AgentDeliveryResponseDto
from control_plane.app.modules.source_control.api.repositories import SourceControlQueryRuntime
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
)

AGENT_DELIVERY_READ_CAPABILITY = "requirement.read"
_RESPONSES = cast(
    dict[int | str, dict[str, Any]],
    {
        **{status: PROBLEM_RESPONSES[status] for status in (401, 403, 404, 422, 500)},
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
)


def create_agent_delivery_query_router(
    runtime_provider: Callable[[], SourceControlQueryRuntime],
    principal_provider: Callable[[], Any],
    capability_guard: Callable[[Any, str, str | None], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workspaces", tags=["source-control"])

    @router.get(
        "/{workspaceId}/agent-deliveries/{deliveryId}",
        operation_id="source_control_agent_delivery_get",
        response_model=AgentDeliveryResponseDto,
        responses=_RESPONSES,
    )
    def agent_delivery_get(
        workspace_id: Annotated[UUID, Path(alias="workspaceId")],
        delivery_id: Annotated[UUID, Path(alias="deliveryId")],
        principal: Annotated[Any, Depends(principal_provider)],
    ) -> AgentDeliveryResponseDto | Response:
        resolved_workspace_id = str(workspace_id)
        capability_guard(
            principal,
            AGENT_DELIVERY_READ_CAPABILITY,
            resolved_workspace_id,
        )
        try:
            runtime = runtime_provider()
            with runtime.engine.connect() as db:
                delivery = get_agent_delivery(
                    db,
                    workspace_id=resolved_workspace_id,
                    request_id=str(delivery_id),
                    dependencies=runtime.dependencies,
                )
        except AgentPushNotFound:
            return problem_response(404, "Agent delivery not found")
        except (AgentDeliveryDependencyUnavailable, SQLAlchemyError):
            return problem_response(503, "Source Control agent delivery query unavailable")
        return AgentDeliveryResponseDto.from_domain(delivery)

    return router
