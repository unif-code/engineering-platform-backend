from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Connection

from control_plane.app.modules.source_control.domain.agent_delivery import (
    AgentExecutionBindingSnapshot,
    AgentPushRequestSpec,
    ExactCommitSha,
    Sha256Digest,
)


class AgentDeliveryDependencyUnavailable(RuntimeError):
    pass


class AgentPushResultUnknown(RuntimeError):
    pass


class AgentPushDenied(RuntimeError):
    pass


class AgentPushHeadConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedAgentPushGrant:
    raw: str = field(repr=False)
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class BrokerPushLocator:
    request_id: str
    attempt_id: str
    attempt_generation: int
    repository_id: str
    branch_name: str
    expected_remote_head_sha: ExactCommitSha
    target_commit_sha: ExactCommitSha
    content_digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class BrokerPushRequest(BrokerPushLocator):
    execution_binding_digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class BrokerPushObservation:
    remote_head_sha: ExactCommitSha
    observed_at: datetime
    write_applied: bool = False


@dataclass(frozen=True, slots=True)
class BrokerGrantLocator:
    attempt_id: str
    fenced_generation: int
    repository_id: str
    branch_name: str


@dataclass(frozen=True, slots=True)
class BrokerRevocationResult:
    revoked: bool


@dataclass(frozen=True, slots=True)
class BrokerFreezeResult:
    frozen: bool


class AgentExecutionBindingPort(Protocol):
    def validate(
        self,
        spec: AgentPushRequestSpec,
        *,
        raw_fencing_token: str,
    ) -> AgentExecutionBindingSnapshot: ...


class AgentPushGrantIssuerPort(Protocol):
    def issue(self) -> IssuedAgentPushGrant: ...


class AgentDeliveryPolicyPort(Protocol):
    def max_push_grant_ttl(self) -> timedelta: ...

    def next_reconcile_at(self, *, now: datetime, attempts: int) -> datetime: ...


class AgentPushBrokerPort(Protocol):
    def push_and_verify(self, request: BrokerPushRequest) -> BrokerPushObservation: ...

    def observe(self, locator: BrokerPushLocator) -> BrokerPushObservation: ...

    def revoke(self, locator: BrokerGrantLocator) -> BrokerRevocationResult: ...

    def freeze_branch(self, locator: BrokerPushLocator) -> BrokerFreezeResult: ...


class AgentDeliveryRepository(Protocol):
    db: Connection

    def workspace_repository(self, repository_id: str, *, for_update: bool = False) -> Any: ...

    def branch_binding(self, binding_id: str, *, for_update: bool = False) -> Any: ...

    def insert_agent_push(self, **values: Any) -> Any: ...

    def agent_push_by_id(self, request_id: str, *, for_update: bool = False) -> Any: ...

    def agent_push_by_idempotency(
        self,
        workspace_id: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def consume_agent_push(
        self,
        *,
        request_id: str,
        grant_digest: str,
        now: datetime,
        next_reconcile_at: datetime,
    ) -> Any: ...

    def transition_agent_push(
        self,
        request_id: str,
        *,
        expected_state: str,
        expected_attempts: int | None = None,
        values: Mapping[str, object],
    ) -> Any: ...

    def claim_reconcilable(
        self,
        *,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def attempt_fence(self, attempt_id: str, *, for_update: bool = False) -> Any: ...

    def upsert_attempt_fence(self, **values: Any) -> Any: ...

    def fence_open_agent_pushes(
        self,
        *,
        attempt_id: str,
        fenced_generation: int,
        reason_code: str,
        now: datetime,
    ) -> list[Any]: ...

    def agent_pushes_for_attempt(
        self,
        attempt_id: str,
        *,
        fenced_generation: int,
    ) -> list[Any]: ...

    def record_fenced_observation(
        self,
        request_id: str,
        *,
        remote_head_sha: str,
        observed_at: datetime,
        reason_code: str,
        now: datetime,
    ) -> Any: ...

    def claim_due_revocations(
        self,
        *,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def transition_revocation(
        self,
        attempt_id: str,
        *,
        expected_generation: int,
        expected_state: str,
        values: Mapping[str, object],
    ) -> Any: ...

    def insert_fact(self, **values: Any) -> Any: ...

    def fact_by_request(self, request_id: str) -> Any: ...

    def delivery_for_workspace(self, request_id: str, workspace_id: str) -> Any: ...


class AgentDeliveryRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> AgentDeliveryRepository: ...
