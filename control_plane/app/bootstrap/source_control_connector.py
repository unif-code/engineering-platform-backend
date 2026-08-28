from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from control_plane.app import __version__
from control_plane.app.bootstrap.source_control_runtime import (
    SourceControlRuntime,
    source_control_runtime_context,
)
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

RuntimeContextProvider = Callable[[], AbstractContextManager[SourceControlRuntime]]


@dataclass(slots=True)
class _ManagedRuntimeState:
    runtime: SourceControlRuntime | None = None


def create_source_control_connector_app(
    *,
    runtime_provider: Callable[[], SourceControlWebhookRuntime] | None = None,
    runtime_context_provider: RuntimeContextProvider = source_control_runtime_context,
) -> FastAPI:
    managed_state = _ManagedRuntimeState()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        context: AbstractContextManager[SourceControlRuntime] | None = None
        try:
            context = runtime_context_provider()
            managed_state.runtime = context.__enter__()
        except SourceControlDependencyUnavailable:
            context = None
            managed_state.runtime = None
        try:
            yield
        finally:
            managed_state.runtime = None
            if context is not None:
                context.__exit__(None, None, None)

    def managed_runtime_provider() -> SourceControlWebhookRuntime:
        runtime = managed_state.runtime
        if runtime is None:
            raise SourceControlDependencyUnavailable(
                "Source Control connector runtime is unavailable"
            )
        return SourceControlWebhookRuntime(runtime.dependencies)

    resolved_runtime_provider = runtime_provider or managed_runtime_provider
    app = FastAPI(
        title="engineering-platform-source-control-connector",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=None if runtime_provider is not None else lifespan,
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
            if runtime_provider is None:
                managed_runtime = managed_state.runtime
                if managed_runtime is None:
                    raise SourceControlDependencyUnavailable(
                        "Source Control connector runtime is unavailable"
                    )
                managed_runtime.ensure_ready()
            runtime = resolved_runtime_provider()
            if not ping(runtime.dependencies.engine):
                return problem_response(503, "Not ready")
        except (SourceControlDependencyUnavailable, SQLAlchemyError):
            return problem_response(503, "Not ready")
        return {"status": "ready"}

    app.include_router(create_webhook_router(resolved_runtime_provider))
    return app


__all__ = [
    "create_source_control_connector_app",
]
