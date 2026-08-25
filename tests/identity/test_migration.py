from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import pytest
from sqlalchemy import CHAR, TIMESTAMP, Connection, Engine, inspect, text
from sqlalchemy.engine.interfaces import ReflectedColumn
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
IDENTITY_CONFIGURATION_TABLES = {
    "active_pointer",
    "configuration_idempotency_record",
    "configuration_outbox",
    "draft",
    "policy_key",
    "version",
}

EXPECTED_COLUMNS = {
    "account": [
        ("id", "UUID", False, None),
        ("employee_no", "CHAR(8)", False, None),
        ("display_name", "TEXT", False, None),
        ("profession", "TEXT", True, None),
        ("status", "TEXT", False, None),
        ("password_hash", "TEXT", True, None),
        ("password_set_at", "TIMESTAMPTZ", True, None),
        ("totp_sealed", "BYTEA", True, None),
        ("totp_confirmed_at", "TIMESTAMPTZ", True, None),
        ("totp_last_step", "BIGINT", True, None),
        ("is_super_admin", "BOOLEAN", False, "false"),
        ("version", "BIGINT", False, "1"),
        ("created_at", "TIMESTAMPTZ", False, "now()"),
        ("updated_at", "TIMESTAMPTZ", False, "now()"),
    ],
    "auth_challenge": [
        ("id", "UUID", False, None),
        ("token_hash", "TEXT", False, None),
        ("purpose", "TEXT", False, None),
        ("account_id", "UUID", False, None),
        ("actor_id", "UUID", True, None),
        ("issued_at", "TIMESTAMPTZ", False, "now()"),
        ("expires_at", "TIMESTAMPTZ", False, None),
        ("attempt_limit", "INTEGER", False, None),
        ("attempt_count", "INTEGER", False, "0"),
        ("consumed_at", "TIMESTAMPTZ", True, None),
        ("revoked_at", "TIMESTAMPTZ", True, None),
    ],
    "idempotency_record": [
        ("id", "UUID", False, None),
        ("actor", "TEXT", False, None),
        ("operation", "TEXT", False, None),
        ("idempotency_key", "TEXT", False, None),
        ("request_fingerprint", "TEXT", False, None),
        ("state", "TEXT", False, None),
        ("http_status", "INTEGER", True, None),
        ("result_metadata", "JSONB", True, None),
        ("sealed_response", "BYTEA", True, None),
        ("created_at", "TIMESTAMPTZ", False, "now()"),
        ("updated_at", "TIMESTAMPTZ", False, "now()"),
        ("completed_at", "TIMESTAMPTZ", True, None),
    ],
    "login_backoff": [
        ("employee_no", "CHAR(8)", False, None),
        ("failure_count", "INTEGER", False, "0"),
        ("last_failure_at", "TIMESTAMPTZ", True, None),
        ("locked_until", "TIMESTAMPTZ", True, None),
        ("source", "TEXT", False, None),
    ],
    "session": [
        ("id", "UUID", False, None),
        ("account_id", "UUID", False, None),
        ("token_hash", "TEXT", False, None),
        ("kind", "TEXT", False, None),
        ("created_at", "TIMESTAMPTZ", False, "now()"),
        ("last_seen_at", "TIMESTAMPTZ", False, "now()"),
        ("expires_hint", "TIMESTAMPTZ", False, None),
        ("revoked_at", "TIMESTAMPTZ", True, None),
        ("revoke_reason", "TEXT", True, None),
        ("bootstrap_purpose", "TEXT", True, None),
        ("bootstrap_totp_attempt_count", "INTEGER", False, "0"),
    ],
    "temp_credential": [
        ("id", "UUID", False, None),
        ("account_id", "UUID", False, None),
        ("secret_hash", "TEXT", False, None),
        ("expires_at", "TIMESTAMPTZ", False, None),
        ("consumed_at", "TIMESTAMPTZ", True, None),
        ("issued_by", "UUID", False, None),
        ("created_at", "TIMESTAMPTZ", False, "now()"),
    ],
}

EXPECTED_UNIQUE_COLUMNS = {
    "account": {("employee_no",)},
    "auth_challenge": {("token_hash",)},
    "idempotency_record": {("actor", "operation", "idempotency_key")},
    "login_backoff": set(),
    "session": {("token_hash",)},
    "temp_credential": set(),
}

EXPECTED_PRIMARY_KEYS = {
    "account": ("id",),
    "auth_challenge": ("id",),
    "idempotency_record": ("id",),
    "login_backoff": ("employee_no", "source"),
    "session": ("id",),
    "temp_credential": ("id",),
}

EXPECTED_FOREIGN_KEYS = {
    "account": set(),
    "auth_challenge": {
        (("account_id",), "identity", "account", ("id",)),
        (("actor_id",), "identity", "account", ("id",)),
    },
    "idempotency_record": set(),
    "login_backoff": set(),
    "session": {(("account_id",), "identity", "account", ("id",))},
    "temp_credential": {
        (("account_id",), "identity", "account", ("id",)),
        (("issued_by",), "identity", "account", ("id",)),
    },
}

T_BEFORE = "2025-12-31 23:59:59+00"
T_CREATED = "2026-01-01 00:00:00+00"
T_AFTER = "2026-01-01 00:00:01+00"
T_EXPIRES = "2026-01-01 01:00:00+00"


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


def _postgres_type(column: ReflectedColumn) -> str:
    column_type = column["type"]
    type_name = type(column_type).__name__
    if isinstance(column_type, CHAR):
        return f"CHAR({column_type.length})"
    if isinstance(column_type, TIMESTAMP) and column_type.timezone:
        return "TIMESTAMPTZ"
    return type_name


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
    assert tables == IDENTITY_TABLES | IDENTITY_CONFIGURATION_TABLES


def test_identity_columns_types_nullability_and_defaults_match_contract(
    identity_owner_engine: Engine,
) -> None:
    inspector = inspect(identity_owner_engine)
    actual = {
        table_name: [
            (
                column["name"],
                _postgres_type(column),
                column["nullable"],
                column["default"],
            )
            for column in inspector.get_columns(table_name, schema="identity")
        ]
        for table_name in sorted(IDENTITY_TABLES)
    }
    assert actual == EXPECTED_COLUMNS


def test_identity_unique_constraints_have_matching_unique_indexes(
    identity_owner_engine: Engine,
) -> None:
    inspector = inspect(identity_owner_engine)
    unique_constraints = {
        table_name: {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table_name, schema="identity")
        }
        for table_name in sorted(IDENTITY_TABLES)
    }
    unique_indexes = {
        table_name: {
            tuple(index["column_names"])
            for index in inspector.get_indexes(table_name, schema="identity")
            if index["unique"]
        }
        for table_name in sorted(IDENTITY_TABLES)
    }
    assert unique_constraints == EXPECTED_UNIQUE_COLUMNS
    assert unique_indexes == EXPECTED_UNIQUE_COLUMNS


def test_identity_primary_keys_match_contract(identity_owner_engine: Engine) -> None:
    inspector = inspect(identity_owner_engine)
    actual = {
        table_name: tuple(
            inspector.get_pk_constraint(table_name, schema="identity")["constrained_columns"]
        )
        for table_name in sorted(IDENTITY_TABLES)
    }
    assert actual == EXPECTED_PRIMARY_KEYS


def test_identity_foreign_keys_stay_inside_identity_schema(
    identity_owner_engine: Engine,
) -> None:
    inspector = inspect(identity_owner_engine)
    actual = {
        table_name: {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_schema"],
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
            )
            for foreign_key in inspector.get_foreign_keys(table_name, schema="identity")
        }
        for table_name in sorted(IDENTITY_TABLES)
    }
    assert actual == EXPECTED_FOREIGN_KEYS


def test_current_alembic_heads_are_installed(identity_owner_engine: Engine) -> None:
    with identity_owner_engine.connect() as conn:
        installed = set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    assert installed == {
        "0008_audit_requirement_grant",
        "0010_identity_policy_reauth",
        "0006_auth_v03_routes",
    }


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
    ("token_hash", "kind", "last_seen_at", "expires_hint", "revoked_at", "reason"),
    [
        ("", "FULL", T_CREATED, T_EXPIRES, None, None),
        ("session-hash", "UNKNOWN", T_CREATED, T_EXPIRES, None, None),
        ("session-hash", "FULL", T_BEFORE, T_EXPIRES, None, None),
        ("session-hash", "FULL", T_CREATED, T_CREATED, None, None),
        ("session-hash", "FULL", T_CREATED, T_EXPIRES, T_BEFORE, "revoked"),
        ("session-hash", "FULL", T_CREATED, T_EXPIRES, None, "revoked"),
        ("session-hash", "FULL", T_CREATED, T_EXPIRES, T_AFTER, None),
        ("session-hash", "FULL", T_CREATED, T_EXPIRES, T_AFTER, ""),
    ],
)
def test_session_rejects_invalid_hash_kind_and_lifecycle(
    identity_owner_engine: Engine,
    token_hash: str,
    kind: str,
    last_seen_at: str,
    expires_hint: str,
    revoked_at: str | None,
    reason: str | None,
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        _insert_account(conn)
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.session "
                    "(id, account_id, token_hash, kind, created_at, last_seen_at, expires_hint, "
                    "revoked_at, revoke_reason) VALUES "
                    "('00000000-0000-0000-0000-000000000011', "
                    "'00000000-0000-0000-0000-000000000001', :token_hash, :kind, "
                    "CAST(:created_at AS timestamptz), CAST(:last_seen_at AS timestamptz), "
                    "CAST(:expires_hint AS timestamptz), CAST(:revoked_at AS timestamptz), :reason)"
                ),
                {
                    "token_hash": token_hash,
                    "kind": kind,
                    "created_at": T_CREATED,
                    "last_seen_at": last_seen_at,
                    "expires_hint": expires_hint,
                    "revoked_at": revoked_at,
                    "reason": reason,
                },
            )


@pytest.mark.parametrize(
    ("employee_no", "failure_count", "last_failure_at", "locked_until"),
    [
        ("INVALID!", 1, T_CREATED, None),
        ("00000001", -1, T_CREATED, None),
        ("00000001", 0, T_CREATED, None),
        ("00000001", 1, None, None),
        ("00000001", 1, T_CREATED, T_BEFORE),
    ],
)
def test_login_backoff_rejects_invalid_employee_count_and_lock_lifecycle(
    identity_owner_engine: Engine,
    employee_no: str,
    failure_count: int,
    last_failure_at: str | None,
    locked_until: str | None,
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.login_backoff "
                    "(employee_no, source, failure_count, last_failure_at, locked_until) VALUES "
                    "(:employee_no, 'legacy', :failure_count, "
                    "CAST(:last_failure_at AS timestamptz), "
                    "CAST(:locked_until AS timestamptz))"
                ),
                {
                    "employee_no": employee_no,
                    "failure_count": failure_count,
                    "last_failure_at": last_failure_at,
                    "locked_until": locked_until,
                },
            )


@pytest.mark.parametrize(
    ("secret_hash", "account_id", "issued_by", "expires_at", "consumed_at"),
    [
        (
            "",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            T_EXPIRES,
            None,
        ),
        (
            "hash",
            "00000000-0000-0000-0000-000000000099",
            "00000000-0000-0000-0000-000000000002",
            T_EXPIRES,
            None,
        ),
        (
            "hash",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000099",
            T_EXPIRES,
            None,
        ),
        (
            "hash",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            T_CREATED,
            None,
        ),
        (
            "hash",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            T_EXPIRES,
            T_BEFORE,
        ),
        (
            "hash",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            T_EXPIRES,
            "2026-01-01 02:00:00+00",
        ),
    ],
)
def test_temp_credential_rejects_invalid_hash_expiry_consume_and_foreign_keys(
    identity_owner_engine: Engine,
    secret_hash: str,
    account_id: str,
    issued_by: str,
    expires_at: str,
    consumed_at: str | None,
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        _insert_account(conn)
        _insert_account(
            conn,
            account_id="00000000-0000-0000-0000-000000000002",
            employee_no="00000002",
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.temp_credential "
                    "(id, account_id, secret_hash, expires_at, consumed_at, issued_by, created_at) "
                    "VALUES ('00000000-0000-0000-0000-000000000012', :account_id, :secret_hash, "
                    "CAST(:expires_at AS timestamptz), CAST(:consumed_at AS timestamptz), "
                    ":issued_by, CAST(:created_at AS timestamptz))"
                ),
                {
                    "account_id": UUID(account_id),
                    "secret_hash": secret_hash,
                    "expires_at": expires_at,
                    "consumed_at": consumed_at,
                    "issued_by": UUID(issued_by),
                    "created_at": T_CREATED,
                },
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
    ("actor_id", "consumed_at", "revoked_at"),
    [
        ("00000000-0000-0000-0000-000000000099", None, None),
        (None, T_BEFORE, None),
        (None, None, T_BEFORE),
        (None, T_AFTER, T_AFTER),
        (None, "2026-01-01 02:00:00+00", None),
    ],
)
def test_auth_challenge_rejects_invalid_actor_and_terminal_lifecycle(
    identity_owner_engine: Engine,
    actor_id: str | None,
    consumed_at: str | None,
    revoked_at: str | None,
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        _insert_account(conn)
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.auth_challenge "
                    "(id, token_hash, purpose, account_id, actor_id, issued_at, expires_at, "
                    "attempt_limit, consumed_at, revoked_at) VALUES "
                    "('00000000-0000-0000-0000-000000000022', 'challenge-terminal', "
                    "'LOGIN_TOTP', '00000000-0000-0000-0000-000000000001', :actor_id, "
                    "CAST(:issued_at AS timestamptz), CAST(:expires_at AS timestamptz), 5, "
                    "CAST(:consumed_at AS timestamptz), CAST(:revoked_at AS timestamptz))"
                ),
                {
                    "actor_id": UUID(actor_id) if actor_id else None,
                    "issued_at": T_CREATED,
                    "expires_at": T_EXPIRES,
                    "consumed_at": consumed_at,
                    "revoked_at": revoked_at,
                },
            )


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


@pytest.mark.parametrize("column", ["actor", "operation", "idempotency_key", "request_fingerprint"])
def test_idempotency_identity_fields_must_be_non_empty(
    identity_owner_engine: Engine,
    column: str,
) -> None:
    values = {
        "actor": "actor-1",
        "operation": "operation-1",
        "idempotency_key": "key-1",
        "request_fingerprint": "fingerprint-1",
    }
    values[column] = ""
    with _rollback_connection(identity_owner_engine) as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.idempotency_record "
                    "(id, actor, operation, idempotency_key, request_fingerprint, state) VALUES "
                    "('00000000-0000-0000-0000-000000000032', :actor, :operation, "
                    ":idempotency_key, :request_fingerprint, 'IN_PROGRESS')"
                ),
                values,
            )


@pytest.mark.parametrize(
    ("http_status", "updated_at", "completed_at"),
    [
        (99, T_AFTER, T_AFTER),
        (600, T_AFTER, T_AFTER),
        (200, T_BEFORE, T_AFTER),
        (200, T_CREATED, T_AFTER),
        (200, T_AFTER, T_BEFORE),
    ],
)
def test_completed_idempotency_rejects_invalid_http_status_and_timestamps(
    identity_owner_engine: Engine,
    http_status: int,
    updated_at: str,
    completed_at: str,
) -> None:
    with _rollback_connection(identity_owner_engine) as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.idempotency_record "
                    "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                    "http_status, result_metadata, sealed_response, created_at, updated_at, "
                    "completed_at) VALUES "
                    "('00000000-0000-0000-0000-000000000033', 'actor-1', 'operation-1', "
                    "'key-1', 'fingerprint-1', 'COMPLETED', :http_status, '{}'::jsonb, "
                    "decode('00', 'hex'), CAST(:created_at AS timestamptz), "
                    "CAST(:updated_at AS timestamptz), CAST(:completed_at AS timestamptz))"
                ),
                {
                    "http_status": http_status,
                    "created_at": T_CREATED,
                    "updated_at": updated_at,
                    "completed_at": completed_at,
                },
            )


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
