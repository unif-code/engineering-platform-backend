from collections.abc import Callable
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, create_engine

from control_plane.app import __version__
from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.identity import IdentityDependencies
from control_plane.app.modules.identity.adapters.policy import DefaultEffectivePolicy
from control_plane.app.modules.identity.adapters.runtime import (
    SystemClock,
    SystemRandom,
    no_auth_change,
)
from control_plane.app.modules.identity.adapters.sqlalchemy import SqlAlchemyIdentityRepository
from control_plane.app.modules.identity.api.auth_routes import (
    IdentityHttpRuntime,
    create_auth_router,
)
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
from control_plane.app.shared.db.settings import DbSettings, SecuritySettings
from control_plane.app.shared.security import FileSecretManager

API_DESCRIPTION = """内部研发平台 Control Plane API。

全局约定：JSON 一律 camelCase；ID 一律 string；错误统一 application/problem+json（RFC 9457）；
分页 cursor 型 {items, nextCursor}；写并发 If-Match/ETag；变更命令 Idempotency-Key——
后三者自 V0.2 首个真实接口起强制执行。"""


class ReadyDto(CamelModel):
    status: Literal["ready"]


@lru_cache(maxsize=1)
def identity_runtime_engine() -> Engine:
    """Identity runtime engine; constructing it does not open a database connection."""
    return create_engine(
        DbSettings().identity_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


@lru_cache(maxsize=1)
def identity_http_runtime() -> IdentityHttpRuntime:
    # Task 9 replaces the explicit no-projection hook before protected consumers
    # are registered. Until then no authorization projection can become stale.
    dependencies = IdentityDependencies(
        repository_factory=SqlAlchemyIdentityRepository,
        secret_manager=FileSecretManager(SecuritySettings()),
        policy=DefaultEffectivePolicy(),
        clock=SystemClock(),
        random=SystemRandom(),
        audit=SqlAlchemyTransactionalAuditAppender(),
        on_auth_change=no_auth_change,
    )
    return IdentityHttpRuntime(engine=identity_runtime_engine(), dependencies=dependencies)


def create_app(
    *,
    identity_runtime_provider: Callable[[], IdentityHttpRuntime] = identity_http_runtime,
) -> FastAPI:
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
    app.include_router(create_auth_router(identity_runtime_provider))

    return app
