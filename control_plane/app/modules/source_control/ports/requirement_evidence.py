from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.app.modules.source_control.domain import (
    ExternalValidationRequestEnvelope,
    IntegrationBaselineRequestEnvelope,
)


class RelayEvidenceRequestsResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    claimed: int = Field(ge=0)
    accepted: int = Field(ge=0)
    released: int = Field(ge=0)


class RequirementEvidencePort(Protocol):
    def validate_snapshot(
        self,
        *,
        requirement_id: str,
        requirement_version: int,
        required_work_item_set_version: int,
        required_work_item_set_hash: str,
        work_item_ids: tuple[str, ...],
    ) -> None: ...

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[
        ExternalValidationRequestEnvelope | IntegrationBaselineRequestEnvelope,
        ...,
    ]: ...

    def acknowledge_request(self, message_id: str) -> None: ...

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None: ...
