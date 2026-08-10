from sqlalchemy import TIMESTAMP, Column, Engine, Integer, MetaData, Table, Text, insert

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope

metadata = MetaData(schema="audit")

audit_event = Table(
    "audit_event",
    metadata,
    Column("id", Text, primary_key=True),
    Column("occurred_at", TIMESTAMP(timezone=True), nullable=False),
    Column("actor", Text, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target_type", Text, nullable=False),
    Column("target_id", Text, nullable=False),
    Column("result", Text, nullable=False),
    Column("reason", Text),
    Column("correlation_id", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
)


class SqlAlchemyAuditEventRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def append(self, envelope: AuditEnvelope) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(audit_event).values(**envelope.model_dump()))
