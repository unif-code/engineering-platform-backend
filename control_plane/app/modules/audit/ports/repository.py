from typing import Protocol

from control_plane.app.modules.audit.domain.envelope import AuditEnvelope


class AuditEventRepository(Protocol):
    def append(self, envelope: AuditEnvelope) -> None: ...
