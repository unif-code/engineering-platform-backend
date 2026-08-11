from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from control_plane.app import __version__
from control_plane.app.modules.identity.api.routes import router as identity_router
from control_plane.app.shared.api.camel import CamelModel
from control_plane.app.shared.api.problem import (
    PROBLEM_RESPONSES,
    SERVICE_UNAVAILABLE_RESPONSE,
    problem_response,
    register_problem_handlers,
)
from control_plane.app.shared.api.request_id import request_id_middleware
from control_plane.app.shared.db.engine import ping, runtime_engine

API_DESCRIPTION = """内部研发平台 Control Plane API。

全局约定：JSON 一律 camelCase；ID 一律 string；错误统一 application/problem+json（RFC 9457）；
分页 cursor 型 {items, nextCursor}；写并发 If-Match/ETag；变更命令 Idempotency-Key——
后三者自 V0.2 首个真实接口起强制执行。"""


class ReadyDto(CamelModel):
    status: Literal["ready"]


def create_app() -> FastAPI:
    app = FastAPI(
        title="engineering-platform-control-plane",
        version=__version__,
        description=API_DESCRIPTION,
    )
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)

    @app.get(
        "/healthz",
        operation_id="system_healthz",
        responses={500: PROBLEM_RESPONSES[500]},
    )
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # 就绪返回固定 DTO；数据库不可达时返回 Problem Details。
    # 同步定义：ping 是阻塞 IO，由 FastAPI 放入线程池执行，不阻塞事件循环。
    @app.get(
        "/readyz",
        operation_id="system_readyz",
        response_model=ReadyDto,
        responses={503: SERVICE_UNAVAILABLE_RESPONSE, 500: PROBLEM_RESPONSES[500]},
    )
    def readyz() -> JSONResponse | dict[str, str]:
        if ping(runtime_engine()):
            return {"status": "ready"}
        return problem_response(503, "Not ready", detail="database unreachable")

    app.include_router(identity_router)

    return app
