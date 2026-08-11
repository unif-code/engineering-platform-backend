from sqlalchemy import Connection, text

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope
from control_plane.app.modules.audit.ports.repository import AuditEventRepository


def record(envelope: AuditEnvelope, repository: AuditEventRepository) -> None:
    repository.append(envelope)


def record_in_transaction(db: Connection, envelope: AuditEnvelope) -> None:
    """Append through the audit-owned least-privilege surface in the caller transaction."""
    db.execute(
        text(
            "SELECT audit.append_event("
            ":id, :occurred_at, :actor, :actor_type, :action, :target_type, "
            ":target_id, :result, :reason, :correlation_id, :schema_version)"
        ),
        envelope.model_dump(),
    )
