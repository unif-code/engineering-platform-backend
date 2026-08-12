from datetime import datetime

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope
from control_plane.app.modules.audit.ports.repository import AuditEventQueryRepository


def list_events(
    repository: AuditEventQueryRepository,
    *,
    actor: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    request_id: str | None = None,
    after_occurred_at: datetime | None = None,
    after_id: str | None = None,
    limit: int = 20,
) -> list[AuditEnvelope]:
    return repository.list_events(
        actor=actor,
        target_type=target_type,
        target_id=target_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        request_id=request_id,
        after_occurred_at=after_occurred_at,
        after_id=after_id,
        limit=limit,
    )
