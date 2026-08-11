import hashlib
import json
from typing import Any

from sqlalchemy import Connection

from control_plane.app.modules.audit import AuditEnvelope, record_in_transaction
from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.domain import (
    Draft,
    DraftArchived,
    DraftNotFound,
    DraftOwnerRequired,
    DraftValidation,
    InvalidPolicyValue,
    StaleDraftBase,
    StaleDraftRevision,
    ValidationIssue,
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
) -> None:
    record_in_transaction(
        db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor_id,
            actor_type="human",
            action=action,
            target_type="configuration_draft",
            target_id=draft_id,
            result=result,
            reason=reason,
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.audit,
    )


def _require_editable(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    draft_id: str,
    namespace: str,
    actor_id: str,
    expected_revision: int,
    dependencies: ConfigurationDependencies,
    action: str,
) -> Draft:
    draft = owner.draft(draft_id, for_update=True)
    if draft is None or draft.namespace != namespace:
        raise DraftNotFound(draft_id)
    if draft.owner_id != actor_id:
        _audit(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            action=action,
            draft_id=draft_id,
            result="DENIED",
            reason="draft owner mismatch",
        )
        raise DraftOwnerRequired(draft_id)
    if draft.status == "ARCHIVED":
        _audit(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            action=action,
            draft_id=draft_id,
            result="DENIED",
            reason="draft is archived",
        )
        raise DraftArchived(draft_id)
    if draft.revision != expected_revision:
        _audit(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            action=action,
            draft_id=draft_id,
            result="CONFLICT",
            reason=(f"stale revision; expected={expected_revision}; current={draft.revision}"),
        )
        raise StaleDraftRevision(draft_id)
    active = owner.active_snapshot(namespace)
    if draft.base_version != active.version:
        _audit(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            action=action,
            draft_id=draft_id,
            result="CONFLICT",
            reason=(
                f"stale base; baseVersion={draft.base_version}; activeVersion={active.version}"
            ),
        )
        raise StaleDraftBase(draft_id)
    return draft


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
    _known_values(owner, namespace, values)
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
    draft = _require_editable(
        db,
        owner,
        draft_id=draft_id,
        namespace=namespace,
        actor_id=actor_id,
        expected_revision=expected_revision,
        dependencies=dependencies,
        action="configuration.draft.update_denied",
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


def _integer_issue(
    key: str, value: Any, minimum: int, maximum: int | None
) -> ValidationIssue | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return ValidationIssue(
            code="INVALID_TYPE",
            key=key,
            message="Value must use the registered integer type.",
        )
    if value < minimum:
        return ValidationIssue(
            code="BELOW_MINIMUM",
            key=key,
            message="Value is below the permitted minimum.",
        )
    if maximum is not None and value > maximum:
        return ValidationIssue(
            code="ABOVE_MAXIMUM",
            key=key,
            message="Value exceeds the permitted maximum.",
        )
    return None


def _validate(content: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    integer_bounds = {
        "identity.temp_credential_ttl": (1, None),
        "identity.session_cap": (1, 10),
        "identity.session_idle_timeout": (15, 240),
        "identity.totp_attempt_cap": (1, None),
        "identity.draft_archive_after": (1, None),
    }
    for key, (minimum, maximum) in integer_bounds.items():
        issue = _integer_issue(key, content.get(key), minimum, maximum)
        if issue is not None:
            issues.append(issue)

    password_age = content.get("identity.password_max_age")
    if password_age != "NEVER":
        issue = _integer_issue("identity.password_max_age", password_age, 1, None)
        if issue is not None:
            issues.append(issue)

    backoff = content.get("identity.login_backoff")
    expected_fields = {
        "failureThreshold",
        "initialDelaySeconds",
        "maximumDelaySeconds",
        "resetAfterHours",
    }
    if not isinstance(backoff, dict) or set(backoff) != expected_fields:
        issues.append(
            ValidationIssue(
                code="INVALID_OBJECT",
                key="identity.login_backoff",
                message="Value does not match the registered object schema.",
            )
        )
    else:
        invalid_field = any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in backoff.values()
        )
        if invalid_field:
            issues.append(
                ValidationIssue(
                    code="INVALID_OBJECT",
                    key="identity.login_backoff",
                    message="Value does not match the registered object schema.",
                )
            )
        elif backoff["maximumDelaySeconds"] < backoff["initialDelaySeconds"]:
            issues.append(
                ValidationIssue(
                    code="CROSS_FIELD_CONFLICT",
                    key="identity.login_backoff",
                    message="Backoff maximum must not be below its initial delay.",
                )
            )
    return sorted(issues, key=lambda issue: (issue.key, issue.code))


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
    draft = _require_editable(
        db,
        owner,
        draft_id=draft_id,
        namespace=namespace,
        actor_id=actor_id,
        expected_revision=expected_revision,
        dependencies=dependencies,
        action="configuration.draft.validation_denied",
    )
    issues = _validate(draft.content)
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
