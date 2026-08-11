import hashlib
from uuid import uuid4

from sqlalchemy import Connection

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.domain.models import Principal
from control_plane.app.shared.api.request_id import current_request_id


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def audit(
    db: Connection,
    *,
    dependencies: IdentityDependencies,
    actor: Principal,
    action: str,
    target_type: str,
    target_id: str,
    result: str,
    reason: str | None,
) -> None:
    record_in_transaction(
        db,
        AuditEnvelope(
            actor=actor.employee_id,
            actor_type="HUMAN" if actor.employee_id != "SYSTEM" else "SYSTEM",
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            reason=reason,
            correlation_id=current_request_id() or uuid4().hex,
        ),
        dependencies.audit,
    )
