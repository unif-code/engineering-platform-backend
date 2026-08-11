from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError

from control_plane.app.shared.db.settings import DbSettings

pytestmark = pytest.mark.integration

IDENTITY_TABLES = {
    "account",
    "auth_challenge",
    "idempotency_record",
    "login_backoff",
    "session",
    "temp_credential",
}


@contextmanager
def _rollback_connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _insert_account(
    conn: Connection,
    *,
    account_id: str = "00000000-0000-0000-0000-000000000001",
    employee_no: str = "00000001",
    status: str = "ENABLED",
    version: int = 1,
) -> None:
    conn.execute(
        text(
            "INSERT INTO identity.account "
            "(id, employee_no, display_name, status, version) "
            "VALUES (:id, :employee_no, 'Task 4', :status, :version)"
        ),
        {
            "id": UUID(account_id),
            "employee_no": employee_no,
            "status": status,
            "version": version,
        },
    )


def test_identity_tables_exist(identity_owner_engine: Engine) -> None:
    with identity_owner_engine.connect() as conn:
        tables = set(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'identity'"
                )
            ).scalars()
        )
    assert tables == IDENTITY_TABLES


def test_both_independent_alembic_heads_are_installed(identity_owner_engine: Engine) -> None:
    with identity_owner_engine.connect() as conn:
        installed = set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    assert installed == {"0001_audit_event", "0001_identity_base"}


@pytest.mark.parametrize("employee_no", ["1234567", "123456789", "ABCDEFGH"])
def test_employee_number_requires_exactly_eight_digits(
    identity_owner_engine: Engine, employee_no: str
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        with pytest.raises((DataError, IntegrityError)):
            _insert_account(conn, employee_no=employee_no)


def test_employee_number_is_unique(identity_owner_engine: Engine) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        _insert_account(conn)
        with pytest.raises(IntegrityError):
            _insert_account(
                conn,
                account_id="00000000-0000-0000-0000-000000000002",
            )


@pytest.mark.parametrize(
    ("status", "version"),
    [("UNKNOWN", 1), ("ENABLED", 0), ("ENABLED", -1)],
)
def test_account_status_and_version_checks(
    identity_owner_engine: Engine, status: str, version: int
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        with pytest.raises(IntegrityError):
            _insert_account(conn, status=status, version=version)


def test_identity_local_foreign_keys_are_enforced(identity_owner_engine: Engine) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.temp_credential "
                    "(id, account_id, secret_hash, expires_at, issued_by) VALUES "
                    "('00000000-0000-0000-0000-000000000010', "
                    "'00000000-0000-0000-0000-000000000099', 'hash', now() + interval '1 hour', "
                    "'00000000-0000-0000-0000-000000000099')"
                )
            )


@pytest.mark.parametrize(
    ("attempt_limit", "attempt_count", "expires_delta"),
    [(0, 0, "1 hour"), (5, -1, "1 hour"), (5, 6, "1 hour"), (5, 0, "-1 hour")],
)
def test_auth_challenge_rejects_invalid_lifecycle_values(
    identity_owner_engine: Engine,
    attempt_limit: int,
    attempt_count: int,
    expires_delta: str,
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        _insert_account(conn)
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.auth_challenge "
                    "(id, token_hash, purpose, account_id, actor_id, issued_at, expires_at, "
                    "attempt_limit, attempt_count) VALUES "
                    "('00000000-0000-0000-0000-000000000020', 'challenge-hash', 'LOGIN_TOTP', "
                    "'00000000-0000-0000-0000-000000000001', NULL, now(), "
                    "now() + CAST(:expires_delta AS interval), :attempt_limit, :attempt_count)"
                ),
                {
                    "attempt_limit": attempt_limit,
                    "attempt_count": attempt_count,
                    "expires_delta": expires_delta,
                },
            )


def test_auth_challenge_token_hash_is_unique(identity_owner_engine: Engine) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        _insert_account(conn)
        statement = text(
            "INSERT INTO identity.auth_challenge "
            "(id, token_hash, purpose, account_id, issued_at, expires_at, attempt_limit) "
            "VALUES (:id, 'challenge-hash', 'LOGIN_TOTP', "
            "'00000000-0000-0000-0000-000000000001', now(), now() + interval '5 minutes', 5)"
        )
        conn.execute(statement, {"id": UUID("00000000-0000-0000-0000-000000000020")})
        with pytest.raises(IntegrityError):
            conn.execute(statement, {"id": UUID("00000000-0000-0000-0000-000000000021")})


@pytest.mark.parametrize(
    ("state", "http_status", "result_metadata", "sealed_response", "completed_at"),
    [
        ("UNKNOWN", None, None, None, None),
        ("IN_PROGRESS", 200, None, None, None),
        ("COMPLETED", None, "{}", b"sealed", "now()"),
        ("COMPLETED", 200, None, b"sealed", "now()"),
        ("COMPLETED", 200, "{}", None, "now()"),
        ("COMPLETED", 200, "{}", b"sealed", None),
    ],
)
def test_idempotency_record_state_matches_result_fields(
    identity_owner_engine: Engine,
    state: str,
    http_status: int | None,
    result_metadata: str | None,
    sealed_response: bytes | None,
    completed_at: str | None,
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.idempotency_record "
                    "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                    "http_status, result_metadata, sealed_response, completed_at) VALUES "
                    "('00000000-0000-0000-0000-000000000030', 'actor-1', 'op-1', 'key-1', "
                    "'fingerprint-1', :state, :http_status, CAST(:result_metadata AS jsonb), "
                    ":sealed_response, CAST(:completed_at AS timestamptz))"
                ),
                {
                    "state": state,
                    "http_status": http_status,
                    "result_metadata": result_metadata,
                    "sealed_response": sealed_response,
                    "completed_at": completed_at,
                },
            )


def test_idempotency_scope_is_unique(identity_owner_engine: Engine) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        statement = text(
            "INSERT INTO identity.idempotency_record "
            "(id, actor, operation, idempotency_key, request_fingerprint, state) VALUES "
            "(:id, 'actor-1', 'op-1', 'key-1', 'fingerprint-1', 'IN_PROGRESS')"
        )
        conn.execute(statement, {"id": UUID("00000000-0000-0000-0000-000000000030")})
        with pytest.raises(IntegrityError):
            conn.execute(statement, {"id": UUID("00000000-0000-0000-0000-000000000031")})


def test_identity_runtime_settings_have_a_distinct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = "postgresql+psycopg://identity_rw:localdev@127.0.0.1:55432/platform"
    monkeypatch.setenv("IDENTITY_DATABASE_URL", expected)
    settings = DbSettings()
    assert settings.identity_database_url == expected
    assert settings.identity_database_url != settings.database_url


def test_identity_rw_has_only_expected_schema_and_table_privileges(
    identity_rw_engine: Engine,
    identity_owner_engine: Engine,
) -> None:
    with identity_rw_engine.connect() as conn:
        schema_privileges = {
            privilege
            for privilege in ("USAGE", "CREATE")
            if conn.execute(
                text("SELECT has_schema_privilege('identity_rw', 'identity', :privilege)"),
                {"privilege": privilege},
            ).scalar_one()
        }
        table_privileges = {
            table_name: {
                privilege
                for privilege in (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                )
                if conn.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'identity_rw', 'identity.' || :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                ).scalar_one()
            }
            for table_name in IDENTITY_TABLES
        }
    with identity_owner_engine.connect() as conn:
        audit_access = conn.execute(
            text("SELECT has_table_privilege('identity_rw', 'audit.audit_event', 'SELECT')")
        ).scalar_one()

    assert schema_privileges == {"USAGE"}
    assert table_privileges == {
        table_name: {"SELECT", "INSERT", "UPDATE"} for table_name in IDENTITY_TABLES
    }
    assert audit_access is False


def test_identity_rw_cannot_delete(identity_rw_engine: Engine) -> None:
    with identity_rw_engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("DELETE FROM identity.account WHERE false"))
