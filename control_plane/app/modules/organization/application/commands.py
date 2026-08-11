from typing import Any, TypeGuard

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.organization.application.dependencies import (
    OrganizationDependencies,
)
from control_plane.app.modules.organization.domain import (
    InvalidStructure,
    derive_kind,
    validate_structure,
)
from control_plane.app.modules.organization.ports import (
    OrganizationAccountView,
    OrganizationRepository,
)
from control_plane.app.shared.api.request_id import current_request_id


class InvalidParticipant(ValueError):
    """An organization participant is not an effective identity account."""


def _is_effective(
    account: OrganizationAccountView | None,
) -> TypeGuard[OrganizationAccountView]:
    if account is None:
        return False
    status = getattr(account, "status", None)
    return str(status) == "ENABLED" and getattr(account, "initialized", False) is True


def _affected_accounts(edges: dict[str, tuple[str | None, str]], target_id: str) -> tuple[str, ...]:
    affected = {target_id}
    while True:
        descendants = {
            account_id
            for account_id, (superior_id, _kind) in edges.items()
            if superior_id in affected
        }
        expanded = affected | descendants
        if expanded == affected:
            return tuple(sorted(affected))
        affected = expanded


def set_superior(
    repository: OrganizationRepository,
    *,
    account_id: str,
    superior_id: str | None,
    actor: Any,
    reason: str,
    dependencies: OrganizationDependencies,
) -> None:
    repository.lock_structure()
    target = dependencies.identity.get(account_id)
    if not _is_effective(target):
        raise InvalidParticipant("target account is not enabled and initialized")

    superior = None
    if superior_id is not None:
        superior = dependencies.identity.get(superior_id)
        if not _is_effective(superior):
            raise InvalidParticipant("superior account is not enabled and initialized")

    rows = repository.all_edges()
    edges = {row["account_id"]: (row["superior_id"], row["kind"]) for row in rows}
    previous = edges.get(account_id)
    if superior_id is None:
        kind = derive_kind(None)
    else:
        superior_edge = edges.get(superior_id)
        if superior_edge is None:
            raise InvalidStructure("superior must already belong to the organization")
        kind = derive_kind(superior_edge[1])

    proposed = dict(edges)
    proposed[account_id] = (superior_id, kind.value)
    validate_structure(proposed)
    if previous == proposed[account_id]:
        return

    now = dependencies.clock.now()
    repository.upsert_edge(
        account_id=account_id,
        superior_id=superior_id,
        kind=kind.value,
        now=now,
    )
    old_summary = "absent" if previous is None else f"{previous[1]}:{previous[0] or 'none'}"
    new_summary = f"{kind.value}:{superior_id or 'none'}"
    audit_reason = f"{reason}; old={old_summary}; new={new_summary}"
    record_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=now,
            actor=actor.employee_id,
            actor_type="HUMAN" if actor.employee_id != "SYSTEM" else "SYSTEM",
            action="organization.structure.changed",
            target_type="ACCOUNT",
            target_id=account_id,
            result="SUCCESS",
            reason=audit_reason,
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.audit,
    )
    dependencies.on_membership_change(_affected_accounts(proposed, account_id))
