from fastapi.testclient import TestClient

from control_plane.app.bootstrap.app import create_app


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    resp = client.get("/no-such-route")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 404
    assert body["title"]


def test_method_not_allowed_keeps_allow_header(client: TestClient) -> None:
    resp = client.post("/healthz")
    assert resp.status_code == 405
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "GET" in resp.headers["allow"]


def test_validation_error_maps_to_422_problem() -> None:
    app = create_app()

    @app.get("/test-only/probe")
    async def probe(count: int) -> dict[str, int]:  # pragma: no cover - 仅供本测试
        return {"count": count}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/test-only/probe", params={"count": "not-a-number"})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 422
    assert isinstance(body["errors"], list) and body["errors"]
