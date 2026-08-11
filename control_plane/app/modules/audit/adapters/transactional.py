from sqlalchemy import Connection, text

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope


class SqlAlchemyTransactionalAuditAppender:
    """Least-privilege append adapter using the audit-owned database function."""

    def append_in_transaction(self, db: Connection, envelope: AuditEnvelope) -> None:
        db.execute(
            text(
                "SELECT audit.append_event("
                ":id, :occurred_at, :actor, :actor_type, :action, :target_type, "
                ":target_id, :result, :reason, :correlation_id, :schema_version)"
            ),
            envelope.model_dump(),
        )
