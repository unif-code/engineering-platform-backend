import ast
import importlib
import os
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import cast, get_type_hints

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from tests.source_control.conftest import IsolatedSourceControlDatabase

DATABASE_PASSWORD_SENTINEL = "test-only-database-password-sentinel"
UNRELATED_ENVIRONMENT_SENTINEL = "test-only-unrelated-environment-sentinel"
MALFORMED_URL_SENTINEL = "test-only-malformed-url-sentinel"

REQUIRED_MESSAGE = "MIGRATION_DATABASE_URL is required for PostgreSQL integration tests"
MALFORMED_MESSAGE = "MIGRATION_DATABASE_URL must be a valid SQLAlchemy database URL"
SKIP_MESSAGE = "MIGRATION_DATABASE_URL is not configured; PostgreSQL integration tests skipped"


def _migration_database_url_reader() -> Callable[[], URL]:
    try:
        module = importlib.import_module("tests.integration_database")
    except ModuleNotFoundError:
        pytest.fail("shared integration database helper is missing", pytrace=False)
    reader = getattr(module, "migration_database_url", None)
    if not callable(reader):
        pytest.fail("migration database URL reader is missing", pytrace=False)
    return cast(Callable[[], URL], reader)


def _controlled_environment(
    monkeypatch: pytest.MonkeyPatch,
    values: Mapping[str, str],
) -> None:
    monkeypatch.setattr(os, "environ", dict(values))


def _long_traceback_leaks(
    caught: pytest.ExceptionInfo[BaseException],
    sentinels: tuple[str, ...],
) -> tuple[bool, ...]:
    """Return leak flags without retaining traceback text in an assertion failure."""
    rendered = str(caught.getrepr(funcargs=True, style="long"))
    return tuple(sentinel in rendered for sentinel in sentinels)


def test_missing_required_migration_url_has_value_free_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _controlled_environment(
        monkeypatch,
        {
            "REQUIRE_INTEGRATION_DB": "1",
            "TEST_DATABASE_PASSWORD": DATABASE_PASSWORD_SENTINEL,
            "TEST_UNRELATED_ENVIRONMENT": UNRELATED_ENVIRONMENT_SENTINEL,
        },
    )

    with pytest.raises(BaseException) as caught:
        _migration_database_url_reader()()

    leaks = _long_traceback_leaks(
        caught,
        (
            DATABASE_PASSWORD_SENTINEL,
            UNRELATED_ENVIRONMENT_SENTINEL,
        ),
    )
    assert isinstance(caught.value, pytest.fail.Exception)
    assert str(caught.value) == REQUIRED_MESSAGE
    assert caught.value.pytrace is False
    assert caught.value.__context__ is None
    assert leaks == (False, False)


def test_malformed_migration_url_has_value_free_unchained_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed_url = f"not-a-database-url-{MALFORMED_URL_SENTINEL}"
    _controlled_environment(
        monkeypatch,
        {
            "MIGRATION_DATABASE_URL": malformed_url,
            "REQUIRE_INTEGRATION_DB": "1",
        },
    )

    with pytest.raises(BaseException) as caught:
        _migration_database_url_reader()()

    leaks = _long_traceback_leaks(caught, (malformed_url, MALFORMED_URL_SENTINEL))
    assert isinstance(caught.value, pytest.fail.Exception)
    assert str(caught.value) == MALFORMED_MESSAGE
    assert caught.value.pytrace is False
    assert caught.value.__context__ is None
    assert leaks == (False, False)


def test_valid_migration_url_and_fixture_repr_hide_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_url = (
        f"postgresql+psycopg://platform_owner:{DATABASE_PASSWORD_SENTINEL}@localhost:5432/platform"
    )
    _controlled_environment(
        monkeypatch,
        {
            "MIGRATION_DATABASE_URL": configured_url,
            "REQUIRE_INTEGRATION_DB": "1",
        },
    )

    database_url = _migration_database_url_reader()()

    assert isinstance(database_url, URL)
    assert DATABASE_PASSWORD_SENTINEL not in repr(database_url)
    assert DATABASE_PASSWORD_SENTINEL not in str(database_url)
    assert get_type_hints(IsolatedSourceControlDatabase)["url"] is URL
    url_field = next(
        field for field in fields(IsolatedSourceControlDatabase) if field.name == "url"
    )
    assert url_field.repr is False

    engine = create_engine(database_url)
    try:
        isolated = IsolatedSourceControlDatabase(owner=engine, runtime=engine, url=database_url)
        assert DATABASE_PASSWORD_SENTINEL not in repr(isolated)
        assert configured_url not in repr(isolated)
    finally:
        engine.dispose()


def test_tests_do_not_subscript_process_environment() -> None:
    violations: list[str] = []
    for path in Path("tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "environ"
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
            ):
                violations.append(f"{path.as_posix()}:{node.lineno}")

    assert violations == []


def test_missing_optional_migration_url_skips_without_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _controlled_environment(
        monkeypatch,
        {
            "TEST_DATABASE_PASSWORD": DATABASE_PASSWORD_SENTINEL,
            "TEST_UNRELATED_ENVIRONMENT": UNRELATED_ENVIRONMENT_SENTINEL,
        },
    )

    with pytest.raises(BaseException) as caught:
        _migration_database_url_reader()()

    leaks = _long_traceback_leaks(
        caught,
        (DATABASE_PASSWORD_SENTINEL, UNRELATED_ENVIRONMENT_SENTINEL),
    )
    assert isinstance(caught.value, pytest.skip.Exception)
    assert str(caught.value) == SKIP_MESSAGE
    assert caught.value.__context__ is None
    assert leaks == (False, False)
