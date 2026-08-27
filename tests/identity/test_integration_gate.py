import pytest

from tests.integration_database import parse_database_url, required_engine

UNREACHABLE_PASSWORD = "test-only-unreachable-password"
UNREACHABLE_URL = parse_database_url(
    "postgresql+psycopg://identity_rw:"
    f"{UNREACHABLE_PASSWORD}@127.0.0.1:59999/never-connect?connect_timeout=1",
    setting_name="TEST_UNREACHABLE_DATABASE_URL",
)


def test_unavailable_integration_database_skips_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUIRE_INTEGRATION_DB", raising=False)

    with pytest.raises(pytest.skip.Exception) as caught:
        required_engine(UNREACHABLE_URL, role="identity_rw")

    message = str(caught.value)
    assert message == "PostgreSQL integration database unavailable for identity_rw"
    assert UNREACHABLE_PASSWORD not in message
    assert str(UNREACHABLE_URL) not in message


def test_unavailable_integration_database_fails_closed_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_INTEGRATION_DB", "1")

    with pytest.raises(pytest.fail.Exception) as caught:
        required_engine(UNREACHABLE_URL, role="identity_rw")

    message = str(caught.value)
    assert message == "Required PostgreSQL integration database unavailable for identity_rw"
    assert UNREACHABLE_PASSWORD not in message
    assert str(UNREACHABLE_URL) not in message
