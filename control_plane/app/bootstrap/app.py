from fastapi import FastAPI

from control_plane.app import __version__
from control_plane.app.modules.identity.api.routes import router as identity_router
from control_plane.app.shared.api.problem import register_problem_handlers

API_DESCRIPTION = """内部研发平台 Control Plane API。

全局约定：JSON 一律 camelCase；ID 一律 string；错误统一 application/problem+json（RFC 9457）；
分页 cursor 型 {items, nextCursor}；写并发 If-Match/ETag；变更命令 Idempotency-Key——
后三者自 V0.2 首个真实接口起强制执行。"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="engineering-platform-control-plane",
        version=__version__,
        description=API_DESCRIPTION,
    )
    register_problem_handlers(app)

    @app.get("/healthz", operation_id="system_healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(identity_router)

    return app
