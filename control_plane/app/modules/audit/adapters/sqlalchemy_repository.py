from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    Column,
    Engine,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    insert,
    or_,
    select,
)

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope
from control_plane.app.shared.api.request_id import current_request_id

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
    Column("request_id", Text),
    Column("schema_version", Integer, nullable=False),
)


class SqlAlchemyAuditEventRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def append(self, envelope: AuditEnvelope) -> None:
        values = envelope.model_dump()
        values["request_id"] = current_request_id()
        with self._engine.begin() as conn:
            conn.execute(insert(audit_event).values(**values))

    def list_events(
        self,
        *,
        actor: str | None,
        target_type: str | None,
        target_id: str | None,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
        request_id: str | None,
        after_occurred_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[AuditEnvelope]:
        statement = select(audit_event)
        if actor is not None:
            statement = statement.where(audit_event.c.actor == actor)
        if target_type is not None:
            statement = statement.where(audit_event.c.target_type == target_type)
        if target_id is not None:
            statement = statement.where(audit_event.c.target_id == target_id)
        if occurred_from is not None:
            statement = statement.where(audit_event.c.occurred_at >= occurred_from)
        if occurred_to is not None:
            statement = statement.where(audit_event.c.occurred_at < occurred_to)
        if request_id is not None:
            statement = statement.where(audit_event.c.request_id == request_id)
        if after_occurred_at is not None and after_id is not None:
            statement = statement.where(
                or_(
                    audit_event.c.occurred_at > after_occurred_at,
                    and_(
                        audit_event.c.occurred_at == after_occurred_at,
                        audit_event.c.id > after_id,
                    ),
                )
            )
        statement = statement.order_by(audit_event.c.occurred_at, audit_event.c.id).limit(limit)
        with self._engine.connect() as db:
            rows = db.execute(statement).mappings().all()
        return [AuditEnvelope.model_validate(row) for row in rows]
