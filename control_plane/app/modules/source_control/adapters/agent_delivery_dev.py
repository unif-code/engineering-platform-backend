import secrets
from datetime import UTC, datetime
from enum import StrEnum

from control_plane.app.modules.source_control.domain.agent_delivery import (
    digest_agent_push_grant,
)
from control_plane.app.modules.source_control.ports.agent_delivery import (
    AgentDeliveryDependencyUnavailable,
    AgentPushDenied,
    AgentPushHeadConflict,
    AgentPushResultUnknown,
    BrokerFreezeResult,
    BrokerGrantLocator,
    BrokerPushLocator,
    BrokerPushObservation,
    BrokerPushRequest,
    BrokerRevocationResult,
    IssuedAgentPushGrant,
)


class DevBrokerBehavior(StrEnum):
    SUCCESS = "SUCCESS"
    UNKNOWN_BEFORE_WRITE = "UNKNOWN_BEFORE_WRITE"
    UNKNOWN_AFTER_WRITE = "UNKNOWN_AFTER_WRITE"
    DENIED = "DENIED"


class SecureAgentPushGrantIssuer:
    __slots__ = ()

    def issue(self) -> IssuedAgentPushGrant:
        raw = secrets.token_urlsafe(32)
        return IssuedAgentPushGrant(raw=raw, digest=digest_agent_push_grant(raw))


class RestrictedDevAgentPushBroker:
    def __init__(
        self,
        *,
        mode: str,
        initial_heads: dict[tuple[str, str], str] | None = None,
        behaviors: dict[str, DevBrokerBehavior] | None = None,
    ) -> None:
        if mode != "DEV":
            raise AgentDeliveryDependencyUnavailable("Agent push broker is unavailable")
        self._heads = dict(initial_heads or {})
        self._behaviors = dict(behaviors or {})
        self._revoked_through: dict[tuple[str, str, str], int] = {}
        self._frozen_branches: set[tuple[str, str]] = set()
        self._write_count = 0
        self._push_count = 0
        self._observe_count = 0

    @property
    def write_count(self) -> int:
        return self._write_count

    @property
    def push_count(self) -> int:
        return self._push_count

    @property
    def observe_count(self) -> int:
        return self._observe_count

    @property
    def frozen_branches(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._frozen_branches)

    def seed_remote_head(self, repository_id: str, branch_name: str, head_sha: str) -> None:
        self._heads[(repository_id, branch_name)] = head_sha

    def set_behavior(self, request_id: str, behavior: DevBrokerBehavior) -> None:
        self._behaviors[request_id] = behavior

    def push_and_verify(self, request: BrokerPushRequest) -> BrokerPushObservation:
        self._push_count += 1
        key = (request.repository_id, request.branch_name)
        revoked_through = self._revoked_through.get(
            (request.attempt_id, request.repository_id, request.branch_name),
            0,
        )
        if request.attempt_generation <= revoked_through or key in self._frozen_branches:
            raise AgentPushDenied("Agent push was denied")
        behavior = self._behaviors.get(request.request_id, DevBrokerBehavior.SUCCESS)
        if behavior is DevBrokerBehavior.DENIED:
            raise AgentPushDenied("Agent push was denied")
        if behavior is DevBrokerBehavior.UNKNOWN_BEFORE_WRITE:
            raise AgentPushResultUnknown("Agent push result is unknown")
        current = self._heads.get(key)
        if current is None:
            raise AgentPushDenied("Agent push branch is unavailable")
        if current == request.target_commit_sha:
            return BrokerPushObservation(
                remote_head_sha=request.target_commit_sha,
                observed_at=datetime.now(UTC),
            )
        if current != request.expected_remote_head_sha:
            raise AgentPushHeadConflict("Agent push remote head changed")
        self._heads[key] = request.target_commit_sha
        self._write_count += 1
        if behavior is DevBrokerBehavior.UNKNOWN_AFTER_WRITE:
            raise AgentPushResultUnknown("Agent push result is unknown")
        return BrokerPushObservation(
            remote_head_sha=request.target_commit_sha,
            observed_at=datetime.now(UTC),
            write_applied=True,
        )

    def observe(self, locator: BrokerPushLocator) -> BrokerPushObservation:
        self._observe_count += 1
        head = self._heads.get((locator.repository_id, locator.branch_name))
        if head is None:
            raise AgentPushResultUnknown("Agent push observation is unknown")
        return BrokerPushObservation(
            remote_head_sha=head,
            observed_at=datetime.now(UTC),
        )

    def revoke(self, locator: BrokerGrantLocator) -> BrokerRevocationResult:
        key = (locator.attempt_id, locator.repository_id, locator.branch_name)
        self._revoked_through[key] = max(
            self._revoked_through.get(key, 0),
            locator.fenced_generation,
        )
        return BrokerRevocationResult(revoked=True)

    def freeze_branch(self, locator: BrokerPushLocator) -> BrokerFreezeResult:
        self._frozen_branches.add((locator.repository_id, locator.branch_name))
        return BrokerFreezeResult(frozen=True)
