from datetime import datetime

from control_plane.app.modules.source_control import (
    AgentDeliveryDto,
    AgentPushState,
    AuthorizedRepositorySummaryDto,
)
from control_plane.app.shared.api.camel import CamelModel


class AuthorizedRepositoryResponseDto(CamelModel):
    repository_id: str
    provider: str
    project_path: str
    default_branch: str

    @classmethod
    def from_domain(
        cls,
        value: AuthorizedRepositorySummaryDto,
    ) -> "AuthorizedRepositoryResponseDto":
        return cls.model_validate(value.model_dump())


class AuthorizedRepositoryListResponseDto(CamelModel):
    items: list[AuthorizedRepositoryResponseDto]


class AgentDeliveryResponseDto(CamelModel):
    delivery_id: str
    attempt_id: str
    attempt_generation: int
    requirement_id: str
    work_item_id: str
    workspace_id: str
    repository_id: str
    branch_binding_id: str
    branch_name: str
    expected_remote_head_sha: str
    target_commit_sha: str
    content_digest: str
    artifact_refs: list[str]
    state: AgentPushState
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    observed_at: datetime | None
    completed_at: datetime | None
    remote_head_sha: str | None
    last_error_code: str | None
    correlation_id: str

    @classmethod
    def from_domain(cls, value: AgentDeliveryDto) -> "AgentDeliveryResponseDto":
        return cls(
            delivery_id=value.id,
            attempt_id=value.attempt_id,
            attempt_generation=value.attempt_generation,
            requirement_id=value.requirement_id,
            work_item_id=value.work_item_id,
            workspace_id=value.workspace_id,
            repository_id=value.repository_id,
            branch_binding_id=value.branch_binding_id,
            branch_name=value.branch_name,
            expected_remote_head_sha=value.expected_remote_head_sha,
            target_commit_sha=value.target_commit_sha,
            content_digest=value.content_digest,
            artifact_refs=list(value.artifact_refs),
            state=value.state,
            issued_at=value.issued_at,
            expires_at=value.expires_at,
            consumed_at=value.consumed_at,
            observed_at=value.observed_at,
            completed_at=value.completed_at,
            remote_head_sha=value.remote_head_sha,
            last_error_code=value.last_error_code,
            correlation_id=value.correlation_id,
        )
