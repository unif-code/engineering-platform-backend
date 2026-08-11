from typing import Any

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope
from control_plane.app.modules.audit.ports.repository import (
    AuditEventRepository,
    TransactionalAuditAppender,
)


def record(envelope: AuditEnvelope, repository: AuditEventRepository) -> None:
    repository.append(envelope)


def record_in_transaction(
    db: Any,
    envelope: AuditEnvelope,
    appender: TransactionalAuditAppender,
) -> None:
    appender.append_in_transaction(db, envelope)
