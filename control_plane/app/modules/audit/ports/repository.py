from typing import Any, Protocol

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope


class AuditEventRepository(Protocol):
    def append(self, envelope: AuditEnvelope) -> None: ...


class TransactionalAuditAppender(Protocol):
    def append_in_transaction(self, db: Any, envelope: AuditEnvelope) -> None: ...
