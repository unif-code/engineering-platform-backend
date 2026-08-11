import pytest
from fastapi.testclient import TestClient

from control_plane.app.bootstrap.app import create_app

AUTH_OPERATIONS = {
    "/api/v1/auth/login": "auth_login",
    "/api/v1/auth/totp": "auth_totp",
    "/api/v1/auth/logout": "auth_logout",
    "/api/v1/auth/bootstrap/password": "auth_bootstrap_password",
    "/api/v1/auth/bootstrap/totp/enroll": "auth_bootstrap_totp_enroll",
    "/api/v1/auth/bootstrap/totp/confirm": "auth_bootstrap_totp_confirm",
}


def test_openapi_declares_authentication_operations_and_idempotency_header() -> None:
    schema = create_app().openapi()

    for path, operation_id in AUTH_OPERATIONS.items():
        operation = schema["paths"][path]["post"]
        assert operation["operationId"] == operation_id
        idempotency = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "Idempotency-Key"
        )
        assert idempotency["in"] == "header"
        assert idempotency["required"] is True
        assert idempotency["schema"]["minLength"] == 8
        assert idempotency["schema"]["maxLength"] == 128
        assert idempotency["schema"]["pattern"] == r"^[A-Za-z0-9._:-]+$"
        assert "replay" in idempotency["description"].lower()
        assert set(operation["responses"]) >= {"200", "401", "403", "409", "422", "429", "500"}
        assert "Retry-After" in operation["responses"]["429"]["headers"]


def test_login_requires_idempotency_key_before_database_access() -> None:
    response = TestClient(create_app()).post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": "irrelevant"},
        headers={"X-Request-ID": "req-task6red"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["requestId"] == "req-task6red"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/auth/login", {"employeeNo": "00000001", "password": "irrelevant"}),
        ("/api/v1/auth/totp", {"challengeToken": "challenge", "code": "000000"}),
        ("/api/v1/auth/logout", None),
        ("/api/v1/auth/bootstrap/password", {"password": "irrelevant"}),
        ("/api/v1/auth/bootstrap/totp/enroll", None),
        ("/api/v1/auth/bootstrap/totp/confirm", {"code": "000000"}),
    ],
)
def test_all_authentication_routes_reject_cross_site_before_database_access(
    path: str,
    body: dict[str, str] | None,
) -> None:
    response = TestClient(create_app()).post(
        path,
        json=body,
        headers={
            "Idempotency-Key": "task6-red-key",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
            "X-Request-ID": "req-task6csrf",
        },
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["requestId"] == "req-task6csrf"


@pytest.mark.parametrize(
    "key",
    ["", "short", "contains space", "control\ncharacter", "x" * 129, b"\xff" * 8],
)
def test_login_rejects_malformed_idempotency_key_before_database_access(
    key: str | bytes,
) -> None:
    response = TestClient(create_app()).post(
        "/api/v1/auth/login",
        json={"employeeNo": "00000001", "password": "irrelevant"},
        headers={"Idempotency-Key": key},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    ("path", "body", "secret"),
    [
        (
            "/api/v1/auth/login",
            {"employeeNo": "00000001", "password": {"raw": "password-leak-731"}},
            "password-leak-731",
        ),
        (
            "/api/v1/auth/totp",
            {"challengeToken": {"raw": "challenge-leak-842"}, "code": "000000"},
            "challenge-leak-842",
        ),
        (
            "/api/v1/auth/bootstrap/totp/confirm",
            {"code": {"raw": "totp-leak-953"}},
            "totp-leak-953",
        ),
    ],
)
def test_validation_problem_does_not_echo_authentication_input(
    path: str,
    body: dict[str, object],
    secret: str,
) -> None:
    response = TestClient(create_app()).post(
        path,
        json=body,
        headers={"Idempotency-Key": "validation-safe-0001"},
    )

    assert response.status_code == 422
    assert secret not in response.text
    errors = response.json()["errors"]
    assert errors
    assert all(set(error) <= {"type", "loc", "msg", "ctx"} for error in errors)
    assert all({"type", "loc", "msg"} <= set(error) for error in errors)
