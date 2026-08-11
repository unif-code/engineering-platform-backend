from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from control_plane.app.shared.api.camel import CamelModel
from control_plane.app.shared.api.request_id import current_request_id

PROBLEM_MEDIA_TYPE = "application/problem+json"


class Problem(CamelModel):
    type: str | None = None
    title: str
    status: int
    detail: str | None = None
    request_id: str


def _problem_response_declaration(description: str) -> dict[str, object]:
    return {
        "description": description,
        "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}},
    }


PROBLEM_RESPONSES: dict[int, dict[str, object]] = {
    401: _problem_response_declaration("Unauthorized"),
    403: _problem_response_declaration("Forbidden"),
    404: _problem_response_declaration("Not Found"),
    409: _problem_response_declaration("Conflict"),
    422: _problem_response_declaration("Validation failed"),
    500: _problem_response_declaration("Internal server error"),
}

SERVICE_UNAVAILABLE_RESPONSE = _problem_response_declaration("Not ready")


def problem_response(
    status: int,
    title: str,
    detail: str | None = None,
    extra: dict[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {"title": title, "status": status}
    if request_id := current_request_id():
        body["requestId"] = request_id
    if detail is not None:
        body["detail"] = detail
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE, headers=headers
    )


def register_problem_handlers(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("schemas", {})["Problem"] = (
            Problem.model_json_schema(by_alias=True)
        )
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(exc.status_code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            422, "Validation failed", extra={"errors": jsonable_encoder(exc.errors())}
        )

    @app.exception_handler(Exception)
    async def unhandled_exception(_: Request, exc: Exception) -> JSONResponse:
        return problem_response(500, "Internal server error")
