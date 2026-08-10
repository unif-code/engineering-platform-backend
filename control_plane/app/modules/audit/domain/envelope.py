from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


class AuditEnvelope(BaseModel):
    """08 的 Audit Envelope 业务摘要形状；绝不携带凭据或敏感值。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: str
    actor_type: str
    action: str
    target_type: str
    target_id: str
    result: str
    reason: str | None = None
    correlation_id: str
    schema_version: int = 1
