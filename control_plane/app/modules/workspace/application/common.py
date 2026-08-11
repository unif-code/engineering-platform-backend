from typing import Any, TypeGuard

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.workspace.application.dependencies import WorkspaceDependencies
from control_plane.app.modules.workspace.domain import WorkspaceDto
from control_plane.app.modules.workspace.ports import WorkspaceAccountView, WorkspaceRepository
from control_plane.app.shared.api.request_id import current_request_id


def actor_id(actor: Any) -> str:
    value = getattr(actor, "account_id", None) or getattr(actor, "employee_id", None)
    if not isinstance(value, str) or not value:
        raise ValueError("actor has no stable identity")
    return value


def is_effective(
    account: WorkspaceAccountView | None,
) -> TypeGuard[WorkspaceAccountView]:
    return account is not None and str(account.status) == "ENABLED" and account.initialized is True


def workspace_dto(row: Any) -> WorkspaceDto:
    return WorkspaceDto(
        id=str(row["id"]),
        name=row["name"],
        owner_id=row["owner_id"],
        archived_at=row["archived_at"],
        version=row["version"],
    )


def audit(
    repository: WorkspaceRepository,
    *,
    dependencies: WorkspaceDependencies,
    actor: str,
    action: str,
    workspace_id: str,
    reason: str,
) -> None:
    now = dependencies.clock.now()
    record_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=now,
            actor=actor,
            actor_type="SYSTEM" if actor == "SYSTEM" else "HUMAN",
            action=action,
            target_type="WORKSPACE",
            target_id=workspace_id,
            result="SUCCESS",
            reason=reason,
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.audit,
    )
