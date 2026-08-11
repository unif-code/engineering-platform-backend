import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration


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
