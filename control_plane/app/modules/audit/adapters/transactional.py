from sqlalchemy import Connection, text

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope
from control_plane.app.shared.api.request_id import current_request_id


class SqlAlchemyTransactionalAuditAppender:
    """Least-privilege append adapter using the audit-owned database function."""

    def append_in_transaction(self, db: Connection, envelope: AuditEnvelope) -> None:
        request_id = current_request_id()
        if request_id is not None:
            db.execute(
                text("SELECT set_config('app.request_id', :request_id, true)"),
                {"request_id": request_id},
            )
        db.execute(
            text(
                "SELECT audit.append_event("
                ":id, :occurred_at, :actor, :actor_type, :action, :target_type, "
                ":target_id, :result, :reason, :correlation_id, :schema_version)"
            ),
            envelope.model_dump(),
        )
