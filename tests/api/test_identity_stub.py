from fastapi.testclient import TestClient

from control_plane.app.bootstrap.app import create_app


def test_me_requires_real_session(client: TestClient) -> None:
    resp = client.get("/api/v1/me")
    assert resp.status_code == 401
    assert resp.json()["title"] == "Authentication required"


def test_navigation_requires_real_session(client: TestClient) -> None:
    resp = client.get("/api/v1/navigation")
    assert resp.status_code == 401
    assert resp.json()["title"] == "Authentication required"


def test_operation_ids_are_stable() -> None:
    schema = create_app().openapi()
    assert schema["paths"]["/api/v1/me"]["get"]["operationId"] == "identity_me"
    assert schema["paths"]["/api/v1/navigation"]["get"]["operationId"] == "identity_navigation"
