import pytest
from fastapi.testclient import TestClient

from control_plane.app.bootstrap.app import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)
