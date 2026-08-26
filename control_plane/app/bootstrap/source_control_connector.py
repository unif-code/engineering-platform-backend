from collections.abc import Callable
from functools import lru_cache

from fastapi import FastAPI
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app import __version__
from control_plane.app.modules.source_control import SourceControlDependencyUnavailable
from control_plane.app.modules.source_control.api import (
    SourceControlWebhookRuntime,
    create_webhook_router,
)
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
    register_problem_handlers,
)
from control_plane.app.shared.api.request_id import request_id_middleware
from control_plane.app.shared.db.engine import ping
from control_plane.app.shared.db.settings import DbSettings


@lru_cache(maxsize=1)
def source_control_runtime_engine() -> Engine:
    """Construct the Source Control engine without opening a connection."""
    return create_engine(
        DbSettings().source_control_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


def source_control_webhook_runtime() -> SourceControlWebhookRuntime:
    # The DB is wired here, but secret resolution stays unavailable until the
    # GitOps-owned provider adapter is supplied.
    source_control_runtime_engine()
    raise SourceControlDependencyUnavailable(
        "Source Control secret reference resolution is unavailable"
    )


def create_source_control_connector_app(
    *,
    runtime_provider: Callable[[], SourceControlWebhookRuntime] = source_control_webhook_runtime,
) -> FastAPI:
    app = FastAPI(
        title="engineering-platform-source-control-connector",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)

    @app.get("/healthz", responses={500: PROBLEM_RESPONSES[500]})
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        responses={503: SERVICE_UNAVAILABLE_RESPONSE, 500: PROBLEM_RESPONSES[500]},
    )
    def readyz() -> object:
        try:
            runtime = runtime_provider()
            if not ping(runtime.dependencies.engine):
                return problem_response(503, "Not ready")
        except (SourceControlDependencyUnavailable, SQLAlchemyError):
            return problem_response(503, "Not ready")
        return {"status": "ready"}

    app.include_router(create_webhook_router(runtime_provider))
    return app


__all__ = [
    "create_source_control_connector_app",
    "source_control_runtime_engine",
    "source_control_webhook_runtime",
]
