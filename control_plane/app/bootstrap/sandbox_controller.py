from collections.abc import Callable
from functools import lru_cache

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from control_plane.app import __version__
from control_plane.app.modules.agent_run.api import (
    SandboxHttpRuntime,
    UnavailableServiceIdentityVerifier,
    create_sandbox_router,
    register_sandbox_exception_handlers,
)
from control_plane.app.shared.api.problem import problem_response, register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware

SANDBOX_API_DESCRIPTION = """Private workload-to-workload Sandbox Controller API.

JSON uses camelCase. Mutations require Idempotency-Key; current-materialization
mutations also require a strong If-Match entity tag. Errors use RFC 9457 Problem
Details. This API is not mounted in the browser Control Plane application.
"""


@lru_cache(maxsize=1)
def unavailable_sandbox_http_runtime() -> SandboxHttpRuntime:
    return SandboxHttpRuntime(
        controller=None,
        identity_verifier=UnavailableServiceIdentityVerifier(),
    )


def create_sandbox_controller_app(
    *,
    runtime_provider: Callable[[], SandboxHttpRuntime] = unavailable_sandbox_http_runtime,
) -> FastAPI:
    app = FastAPI(
        title="engineering-platform-sandbox-controller",
        version=__version__,
        description=SANDBOX_API_DESCRIPTION,
    )
    register_problem_handlers(app)
    register_sandbox_exception_handlers(app)
    app.middleware("http")(request_id_middleware)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        try:
            runtime = runtime_provider()
        except Exception:
            return problem_response(503, "Not ready")
        if not runtime.ready:
            return problem_response(503, "Not ready")
        return JSONResponse(content={"status": "ready"})

    app.include_router(create_sandbox_router(runtime_provider))
    return app
