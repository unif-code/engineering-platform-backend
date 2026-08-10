from control_plane.app.modules.audit.domain.envelope import AuditEnvelope
from control_plane.app.modules.audit.ports.repository import AuditEventRepository


def record(envelope: AuditEnvelope, repository: AuditEventRepository) -> None:
    repository.append(envelope)
