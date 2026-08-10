import pytest
from fastapi.testclient import TestClient

from control_plane.app.shared.db import engine as engine_module


def test_readyz_reports_unreachable_db_as_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://audit_rw:wrong@localhost:59999/platform"
    )
    engine_module.runtime_engine.cache_clear()
    resp = client.get("/readyz")
    engine_module.runtime_engine.cache_clear()
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["status"] == 503
