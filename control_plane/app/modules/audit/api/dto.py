from datetime import datetime

from control_plane.app.shared.api.camel import CamelModel


class AuditEventResponseDto(CamelModel):
    id: str
    occurred_at: datetime
    actor: str
    actor_type: str
    action: str
    target_type: str
    target_id: str
    result: str
    reason: str | None
    correlation_id: str
    request_id: str | None
    schema_version: int

    @classmethod
    def from_domain(cls, envelope: object) -> "AuditEventResponseDto":
        return cls.model_validate(envelope, from_attributes=True)


class AuditEventListResponseDto(CamelModel):
    items: list[AuditEventResponseDto]
    next_cursor: str | None
