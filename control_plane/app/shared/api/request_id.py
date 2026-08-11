import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from uuid import uuid4

from fastapi import Request, Response

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
RequestResponseEndpoint = Callable[[Request], Awaitable[Response]]
logger = logging.getLogger(__name__)


def current_request_id() -> str | None:
    return _request_id.get()


async def request_id_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex[:16]
    token = _request_id.set(request_id)
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            from control_plane.app.shared.api.problem import problem_response

            logger.error(
                "Unhandled request exception",
                extra={"request_id": request_id, "exception_type": type(exc).__name__},
            )
            response = problem_response(500, "Internal server error")
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        _request_id.reset(token)
