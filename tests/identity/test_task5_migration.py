import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration


def test_session_schema_persists_an_unforgeable_bootstrap_purpose(
    identity_owner_engine: Engine,
) -> None:
    with identity_owner_engine.connect() as conn:
        columns = {
            row.column_name
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='identity' AND table_name='session'"
                )
            )
        }

    assert "bootstrap_purpose" in columns


@pytest.mark.parametrize(
    ("kind", "purpose"),
    [
        ("FULL", "INITIAL_SETUP"),
        ("BOOTSTRAP", None),
        ("BOOTSTRAP", "FORGED"),
    ],
)
def test_session_rejects_kind_and_bootstrap_purpose_mismatches(
    clean_identity_db: None,
    identity_owner_engine: Engine,
    kind: str,
    purpose: str | None,
) -> None:
    with identity_owner_engine.begin() as conn:
        account_id = conn.execute(
            text(
                "INSERT INTO identity.account "
                "(id, employee_no, display_name, status) "
                "VALUES ('00000000-0000-0000-0000-000000000001', "
                "'00000001', 'Alice', 'PENDING_INIT') RETURNING id"
            )
        ).scalar_one()

    with identity_owner_engine.connect() as conn:
        transaction = conn.begin()
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.session "
                    "(id, account_id, token_hash, kind, bootstrap_purpose, expires_hint) "
                    "VALUES ('00000000-0000-0000-0000-000000000002', "
                    ":account_id, 'purpose-mismatch', :kind, :purpose, "
                    "now() + interval '1 hour')"
                ),
                {"account_id": account_id, "kind": kind, "purpose": purpose},
            )
        transaction.rollback()


def test_login_backoff_is_partitioned_by_employee_and_source(
    identity_owner_engine: Engine,
) -> None:
    with identity_owner_engine.connect() as conn:
        columns = {
            row.column_name
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='identity' AND table_name='login_backoff'"
                )
            )
        }
        primary_key = tuple(
            conn.execute(
                text(
                    "SELECT a.attname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid=c.conrelid "
                    "JOIN pg_namespace n ON n.oid=t.relnamespace "
                    "JOIN unnest(c.conkey) WITH ORDINALITY k(attnum, ord) ON true "
                    "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum "
                    "WHERE n.nspname='identity' AND t.relname='login_backoff' "
                    "AND c.contype='p' ORDER BY k.ord"
                )
            ).scalars()
        )

    assert "source" in columns
    assert primary_key == ("employee_no", "source")


def test_login_backoff_requires_an_explicit_non_empty_source(
    identity_owner_engine: Engine,
) -> None:
    with identity_owner_engine.connect() as conn:
        transaction = conn.begin()
        default = conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema='identity' AND table_name='login_backoff' "
                "AND column_name='source'"
            )
        ).scalar_one()
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO identity.login_backoff "
                    "(employee_no, source, failure_count) VALUES ('00000001', '', 0)"
                )
            )
        transaction.rollback()

    assert default is None


def test_identity_0002_downgrade_conservatively_merges_all_sources(
    clean_identity_db: None,
    identity_owner_engine: Engine,
) -> None:
    with identity_owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO identity.login_backoff "
                "(employee_no, source, failure_count, last_failure_at, locked_until) VALUES "
                "('00000001', 'source-a', 4, '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:05:00Z'), "
                "('00000001', 'source-b', 7, '2026-01-02T00:00:00Z', "
                "'2026-01-02T00:15:00Z')"
            )
        )

    config = Config("alembic.ini")
    try:
        command.downgrade(config, "0001_identity_base")
        with identity_owner_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT employee_no, failure_count, last_failure_at, locked_until "
                    "FROM identity.login_backoff WHERE employee_no='00000001'"
                )
            ).one()
        assert row.failure_count == 11
        assert str(row.last_failure_at) == "2026-01-02 00:00:00+00:00"
        assert str(row.locked_until) == "2026-01-02 00:15:00+00:00"
    finally:
        command.upgrade(config, "heads")


def test_identity_role_can_append_audit_only_through_hardened_function(
    identity_owner_engine: Engine,
) -> None:
    with identity_owner_engine.connect() as conn:
        function = conn.execute(
            text(
                "SELECT p.prosecdef, p.proconfig, r.rolname AS owner "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "JOIN pg_roles r ON r.oid=p.proowner "
                "WHERE n.nspname='audit' AND p.proname='append_event'"
            )
        ).one()
        can_execute = conn.execute(
            text(
                "SELECT has_function_privilege("
                "'identity_rw', "
                "'audit.append_event(text,timestamptz,text,text,text,text,text,text,"
                "text,text,int)', "
                "'EXECUTE')"
            )
        ).scalar_one()
        can_insert = conn.execute(
            text("SELECT has_table_privilege('identity_rw','audit.audit_event','INSERT')")
        ).scalar_one()
        can_select = conn.execute(
            text("SELECT has_table_privilege('identity_rw','audit.audit_event','SELECT')")
        ).scalar_one()
        public_can_execute = conn.execute(
            text(
                "SELECT has_function_privilege("
                "'public', "
                "'audit.append_event(text,timestamptz,text,text,text,text,text,text,"
                "text,text,int)', "
                "'EXECUTE')"
            )
        ).scalar_one()

    assert function.prosecdef is True
    assert function.proconfig == ["search_path=pg_catalog, audit"]
    assert function.owner == "platform_owner"
    assert can_execute is True
    assert can_insert is False
    assert can_select is False
    assert public_can_execute is False
