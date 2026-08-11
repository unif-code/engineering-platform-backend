import logging

import pytest
from fastapi.testclient import TestClient

from control_plane.app.bootstrap.app import create_app
from control_plane.app.shared.api.request_id import current_request_id


def test_response_carries_generated_request_id() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.headers["x-request-id"]


def test_inbound_request_id_is_propagated() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz", headers={"X-Request-ID": "req-abc"})

    assert response.headers["x-request-id"] == "req-abc"


def test_problem_body_contains_request_id() -> None:
    client = TestClient(create_app())

    response = client.get("/nope", headers={"X-Request-ID": "req-x"})

    assert response.status_code == 404
    assert response.json()["requestId"] == "req-x"


def test_unhandled_problem_retains_request_id() -> None:
    app = create_app()

    @app.get("/test-only/error")
    async def error_probe() -> None:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/test-only/error", headers={"X-Request-ID": "req-error"})

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "req-error"
    assert response.json()["requestId"] == "req-error"


def test_unhandled_exception_is_reported_with_request_id_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app()

    @app.get("/test-only/logged-error")
    async def logged_error_probe() -> None:
        raise RuntimeError("Bearer top-secret-token")

    caplog.set_level(logging.ERROR, logger="control_plane.app.shared.api.request_id")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/test-only/logged-error", headers={"X-Request-ID": "req-log"})

    records = [
        record
        for record in caplog.records
        if record.name == "control_plane.app.shared.api.request_id"
    ]
    assert response.status_code == 500
    assert len(records) == 1
    assert records[0].getMessage() == "Unhandled request exception"
    assert records[0].__dict__["request_id"] == "req-log"
    assert records[0].__dict__["exception_type"] == "RuntimeError"
    assert "top-secret-token" not in caplog.text


def test_request_id_context_is_available_only_during_its_request() -> None:
    app = create_app()

    @app.get("/test-only/request-id")
    async def request_id_probe() -> dict[str, str | None]:
        return {"requestId": current_request_id()}

    client = TestClient(app)

    response = client.get("/test-only/request-id", headers={"X-Request-ID": "req-isolated"})

    assert response.json() == {"requestId": "req-isolated"}
    assert current_request_id() is None
