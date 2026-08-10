import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from control_plane.app.modules.audit import AuditEnvelope, record
from control_plane.app.modules.audit.adapters.sqlalchemy_repository import (
    SqlAlchemyAuditEventRepository,
)

pytestmark = pytest.mark.integration


def _envelope() -> AuditEnvelope:
    return AuditEnvelope(
        actor="00000000",
        actor_type="HUMAN",
        action="test.append",
        target_type="TEST",
        target_id="t-1",
        result="OK",
        correlation_id="corr-1",
    )


def test_record_persists_via_rw_role(rw_engine: Engine) -> None:
    envelope = _envelope()
    record(envelope, SqlAlchemyAuditEventRepository(rw_engine))
    with rw_engine.connect() as conn:
        actor = conn.execute(
            text("SELECT actor FROM audit.audit_event WHERE id = :id"), {"id": envelope.id}
        ).scalar_one()
    assert actor == "00000000"


def test_update_is_denied_by_role(rw_engine: Engine) -> None:
    with rw_engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("UPDATE audit.audit_event SET result = 'TAMPERED'"))


def test_delete_is_denied_by_role(rw_engine: Engine) -> None:
    with rw_engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("DELETE FROM audit.audit_event"))
