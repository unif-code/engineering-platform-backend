from fastapi.testclient import TestClient

from control_plane.app.bootstrap.app import create_app


def test_me_returns_stub_principal_in_camel_case(client: TestClient) -> None:
    resp = client.get("/api/v1/me")
    assert resp.status_code == 200
    assert resp.json() == {"employeeId": "00000000", "name": "V0.1 Stub"}


def test_navigation_matches_frontend_route_registry(client: TestClient) -> None:
    resp = client.get("/api/v1/navigation")
    assert resp.status_code == 200
    assert resp.json() == [
        {"routeKey": "home", "name": "首页", "order": 1},
        {"routeKey": "admin", "name": "管理后台", "order": 2},
    ]


def test_operation_ids_are_stable() -> None:
    schema = create_app().openapi()
    assert schema["paths"]["/api/v1/me"]["get"]["operationId"] == "identity_me"
    assert schema["paths"]["/api/v1/navigation"]["get"]["operationId"] == "identity_navigation"
