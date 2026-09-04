"""Read fence, not a cross-owner commit lock or HTTP session decision."""

import hashlib
import json

from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
)
from control_plane.app.modules.authorization.application.fence import principal_version
from control_plane.app.modules.authorization.application.grants import effective_grants
from control_plane.app.modules.authorization.domain import GrantDto, GrantStatus, Scope
from control_plane.app.modules.authorization.domain.qualification import ActorQualificationSnapshot
from control_plane.app.modules.authorization.ports import AuthorizationRepository
from control_plane.app.modules.authorization.ports.qualification import ActorFactsPort


def evaluate(
    repository: AuthorizationRepository,
    *,
    actor_id: str,
    workspace_id: str,
    required_capabilities: tuple[str, ...],
    facts: ActorFactsPort,
    dependencies: AuthorizationDependencies,
) -> ActorQualificationSnapshot:
    account = None
    membership = None
    principal = None
    grants: tuple[GrantDto, ...] = ()
    reason = "FACTS_UNAVAILABLE"
    checked_at = dependencies.clock.now()
    try:
        principal = principal_version(repository, account_id=actor_id)
        account = facts.account(actor_id)
        membership = facts.membership(actor_id, workspace_id)
        scope = Scope.workspace(workspace_id)
        grants = tuple(
            sorted(
                (
                    grant
                    for capability in required_capabilities
                    for grant in effective_grants(
                        repository,
                        principal_id=actor_id,
                        capability=capability,
                        scope=scope,
                        dependencies=dependencies,
                    )
                ),
                key=lambda grant: (grant.capability, grant.id),
            )
        )
        account_after = facts.account(actor_id)
        membership_after = facts.membership(actor_id, workspace_id)
        principal_after = principal_version(repository, account_id=actor_id)
        checked_at = dependencies.clock.now()
        stable = (
            account == account_after
            and membership == membership_after
            and principal == principal_after
        )
        effective = (
            principal is not None
            and principal.account_id == actor_id
            and principal.version > 0
            and principal.dirty_generation is None
            and principal.dirty_reason is None
            and account.account_id == actor_id
            and account.version > 0
            and account.status == "ENABLED"
            and account.initialized
            and membership.workspace_id == workspace_id
            and membership.version > 0
            and not membership.archived
            and membership.member_source is not None
            and membership.member_computed_at is not None
        )
        matching = tuple(
            grant
            for grant in grants
            if (
                grant.principal_id == actor_id
                and grant.scope == scope
                and grant.status is GrantStatus.ACTIVE
                and grant.version > 0
                and (grant.valid_from is None or grant.valid_from <= checked_at)
                and (grant.valid_to is None or checked_at < grant.valid_to)
            )
        )
        allowed = (
            stable
            and effective
            and all(
                any(grant.capability == cap for grant in matching) for cap in required_capabilities
            )
        )
        reason = "ALLOW" if allowed else "CURRENT_FACTS_DENIED"
    except Exception:
        checked_at = dependencies.clock.now()
    snapshot = ActorQualificationSnapshot(
        eligible=reason == "ALLOW",
        reason=reason,
        actor_id=actor_id,
        workspace_id=workspace_id,
        required_capabilities=required_capabilities,
        account=account,
        workspace=membership,
        principal=principal,
        grants=grants,
        checked_at=checked_at,
        snapshot_hash="",
    )
    digest = hashlib.sha256(
        json.dumps(
            snapshot.model_dump(mode="json", exclude={"snapshot_hash"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    return snapshot.model_copy(update={"snapshot_hash": f"sha256:{digest.hexdigest()}"})
