from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.ports import SourceControlRepository


def append_lifecycle_audit(
    repository: SourceControlRepository,
    *,
    action: str,
    target_type: str,
    target_id: str,
    dependencies: SourceControlDependencies,
    result: str = "SUCCESS",
    reason: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Append a secret-free Source Control lifecycle fact in the caller's transaction."""
    dependencies.audit.append_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor="SYSTEM:SOURCE_CONTROL",
            actor_type="SYSTEM",
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            reason=reason,
            correlation_id=correlation_id or f"source-control:{target_type}:{target_id}",
        ),
    )
