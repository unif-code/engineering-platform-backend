from datetime import datetime
from typing import Any, Protocol

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope


class AuditEventRepository(Protocol):
    def append(self, envelope: AuditEnvelope) -> None: ...


class AuditEventQueryRepository(Protocol):
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
    ) -> list[AuditEnvelope]: ...


class TransactionalAuditAppender(Protocol):
    def append_in_transaction(self, db: Any, envelope: AuditEnvelope) -> None: ...
