import hashlib
import json
from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.domain import (
    ConfigurationError,
    Draft,
    DraftArchived,
    DraftNotFound,
    DraftOwnerRequired,
    DraftValidation,
    InvalidPolicyValue,
    StaleDraftBase,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.ports import PolicyOwnerPort
from control_plane.app.shared.api.request_id import current_request_id


def _content_hash(content: dict[str, Any]) -> str:
    canonical = json.dumps(
        content,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _audit(
    db: Connection,
    *,
    dependencies: ConfigurationDependencies,
    actor_id: str,
    action: str,
    draft_id: str,
    result: str,
    reason: str,
    target_type: str = "configuration_draft",
) -> None:
    record_in_transaction(
        db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor_id,
            actor_type="human",
            action=action,
            target_type=target_type,
            target_id=draft_id,
            result=result,
            reason=reason,
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.audit,
    )


def _require_editable(
    owner: PolicyOwnerPort,
    *,
    draft_id: str,
    namespace: str,
    actor_id: str,
    expected_revision: int,
) -> Draft:
    draft = owner.draft(draft_id, for_update=True)
    if draft is None or draft.namespace != namespace:
        raise DraftNotFound(draft_id)
    if draft.owner_id != actor_id:
        raise DraftOwnerRequired(draft_id)
    if draft.status == "ARCHIVED":
        raise DraftArchived(draft_id)
    if draft.revision != expected_revision:
        raise StaleDraftRevision(draft_id)
    active = owner.active_snapshot(namespace)
    if draft.base_version != active.version:
        raise StaleDraftBase(draft_id)
    return draft


def _denial_reason_code(error: ConfigurationError) -> str:
    if isinstance(error, DraftNotFound):
        return "DRAFT_NOT_FOUND"
    if isinstance(error, DraftOwnerRequired):
        return "DRAFT_OWNER_REQUIRED"
    if isinstance(error, DraftArchived):
        return "DRAFT_ARCHIVED"
    if isinstance(error, StaleDraftRevision):
        return "STALE_REVISION"
    if isinstance(error, StaleDraftBase):
        return "STALE_BASE"
    if isinstance(error, InvalidPolicyValue):
        return "UNREGISTERED_KEY"
    return "CONFIGURATION_CONFLICT"


def _audit_denial(
    db: Connection,
    *,
    dependencies: ConfigurationDependencies,
    actor_id: str,
    operation: str,
    namespace: str,
    draft_id: str | None,
    error: ConfigurationError,
) -> None:
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action=f"configuration.draft.{operation}_denied",
        draft_id=draft_id or namespace,
        target_type=("configuration_draft" if draft_id is not None else "configuration_namespace"),
        result="DENIED",
        reason=f"namespace={namespace}; reasonCode={_denial_reason_code(error)}",
    )


def _known_values(
    owner: PolicyOwnerPort,
    namespace: str,
    values: dict[str, Any],
) -> set[str]:
    known = {item.key for item in owner.catalog(namespace)}
    if not known or set(values) - known:
        raise InvalidPolicyValue("candidate contains an unregistered policy key")
    return known


def create_draft(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    values: dict[str, Any],
    actor_id: str,
    dependencies: ConfigurationDependencies,
) -> Draft:
    try:
        _known_values(owner, namespace, values)
    except InvalidPolicyValue as error:
        _audit_denial(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            operation="create",
            namespace=namespace,
            draft_id=None,
            error=error,
        )
        raise
    active = owner.active_snapshot(namespace)
    content = {**active.values, **values}
    draft_id = str(dependencies.random.uuid4())
    draft = owner.create_draft(
        id=draft_id,
        namespace=namespace,
        scope="PLATFORM",
        content=content,
        base_version=active.version,
        owner_id=actor_id,
        now=dependencies.clock.now(),
        schema_revision=active.schema_revision,
        content_hash=_content_hash(content),
    )
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.draft.created",
        draft_id=draft.id,
        result="SUCCESS",
        reason=(
            f"namespace={namespace}; baseVersion={draft.base_version}; "
            f"revision={draft.revision}; contentHash={draft.content_hash}"
        ),
    )
    return draft


def update_draft(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    values: dict[str, Any],
    actor_id: str,
    expected_revision: int,
    dependencies: ConfigurationDependencies,
) -> Draft:
    try:
        draft = _require_editable(
            owner,
            draft_id=draft_id,
            namespace=namespace,
            actor_id=actor_id,
            expected_revision=expected_revision,
        )
        _known_values(owner, namespace, values)
        content = {**draft.content, **values}
        updated = owner.update_draft(
            draft_id,
            expected_revision=expected_revision,
            content=content,
            content_hash=_content_hash(content),
            stale=False,
            now=dependencies.clock.now(),
        )
        if updated is None:
            raise StaleDraftRevision(draft_id)
    except ConfigurationError as error:
        _audit_denial(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            operation="update",
            namespace=namespace,
            draft_id=draft_id,
            error=error,
        )
        raise
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.draft.updated",
        draft_id=draft_id,
        result="SUCCESS",
        reason=(
            f"namespace={namespace}; previousRevision={expected_revision}; "
            f"revision={updated.revision}; contentHash={updated.content_hash}"
        ),
    )
    return updated


def validate_draft(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    draft_id: str,
    actor_id: str,
    expected_revision: int,
    dependencies: ConfigurationDependencies,
) -> DraftValidation:
    try:
        draft = _require_editable(
            owner,
            draft_id=draft_id,
            namespace=namespace,
            actor_id=actor_id,
            expected_revision=expected_revision,
        )
        issues = owner.validate_candidate(
            namespace,
            schema_revision=draft.schema_revision,
            values=draft.content,
        )
        evidence = {
            "valid": not issues,
            "issues": [issue.model_dump() for issue in issues],
        }
        updated = owner.save_validation(
            draft_id,
            expected_revision=expected_revision,
            evidence=evidence,
            dependency_versions={},
            now=dependencies.clock.now(),
        )
        if updated is None:
            raise StaleDraftRevision(draft_id)
    except ConfigurationError as error:
        _audit_denial(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            operation="validate",
            namespace=namespace,
            draft_id=draft_id,
            error=error,
        )
        raise
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action=(
            "configuration.draft.validated"
            if not issues
            else "configuration.draft.validation_failed"
        ),
        draft_id=draft_id,
        result="SUCCESS" if not issues else "REJECTED",
        reason=(
            f"namespace={namespace}; revision={updated.revision}; "
            f"contentHash={updated.content_hash}; issueCodes="
            + ",".join(issue.code for issue in issues)
        ),
    )
    return DraftValidation(
        draft_id=draft_id,
        revision=updated.revision,
        content_hash=updated.content_hash,
        valid=not issues,
        issues=issues,
    )
