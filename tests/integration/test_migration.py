import pytest
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


def test_audit_event_table_exists(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        exists = conn.execute(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_schema = 'audit' AND table_name = 'audit_event')"
            )
        ).scalar_one()
    assert exists is True


def test_audit_rw_grants_are_append_only(rw_engine: Engine) -> None:
    with rw_engine.connect() as conn:
        granted = (
            conn.execute(
                text(
                    "SELECT privilege FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) "
                    "AS privilege WHERE has_table_privilege("
                    "'audit_rw', 'audit.audit_event', privilege)"
                )
            )
            .scalars()
            .all()
        )
    assert sorted(granted) == ["INSERT", "SELECT"]
