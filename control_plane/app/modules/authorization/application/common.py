from typing import Any

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.authorization.application.dependencies import (
    AuthorizationDependencies,
)
from control_plane.app.modules.authorization.domain import (
    GrantDto,
    GrantStatus,
    PrincipalVersionDto,
    Scope,
    ScopeType,
)
from control_plane.app.modules.authorization.ports import AuthorizationRepository
from control_plane.app.shared.api.request_id import current_request_id


def actor_id(actor: Any) -> str:
    value = getattr(actor, "account_id", None)
    if not isinstance(value, str) or not value:
        raise ValueError("authorization actor requires a stable account id")
    return value


def grant_dto(row: Any) -> GrantDto:
    return GrantDto(
        id=str(row["id"]),
        principal_id=str(row["principal_id"]),
        capability=str(row["capability"]),
        scope=Scope(
            scope_type=ScopeType(row["scope_type"]),
            scope_id=row["scope_id"],
        ),
        source=str(row["source"]),
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        status=GrantStatus(row["status"]),
        version=int(row["version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def principal_version_dto(row: Any) -> PrincipalVersionDto:
    return PrincipalVersionDto(
        account_id=str(row["account_id"]),
        version=int(row["version"]),
        fence_generation=int(row["fence_generation"]),
        dirty_generation=(
            int(row["dirty_generation"]) if row["dirty_generation"] is not None else None
        ),
        dirty_reason=row["dirty_reason"],
        updated_at=row["updated_at"],
    )


def audit(
    repository: AuthorizationRepository,
    *,
    dependencies: AuthorizationDependencies,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    result: str,
    reason: str,
) -> None:
    record_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor,
            actor_type="HUMAN" if actor != "SYSTEM" else "SYSTEM",
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            reason=reason,
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.audit,
    )
