from sqlalchemy import Connection

from control_plane.app.modules.configuration.application.dependencies import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application.drafts import _audit, _content_hash
from control_plane.app.modules.configuration.domain import (
    ConfigurationError,
    Draft,
    InvalidPolicyValue,
    PolicyVerificationFailed,
    PolicyVersionNotFound,
    SourceStale,
)
from control_plane.app.modules.configuration.ports import PolicyOwnerPort
from control_plane.app.modules.identity import TotpChallengeFailed, verify_admin_totp


def _denial_code(error: ConfigurationError) -> str:
    if isinstance(error, PolicyVersionNotFound):
        return "VERSION_NOT_FOUND"
    if isinstance(error, SourceStale):
        return "SOURCE_STALE"
    if isinstance(error, PolicyVerificationFailed):
        return "REAUTHENTICATION_FAILED"
    if isinstance(error, InvalidPolicyValue):
        return "INVALID_POLICY"
    return "CONFIGURATION_CONFLICT"


def rollback(
    db: Connection,
    owner: PolicyOwnerPort,
    *,
    namespace: str,
    scope: str,
    to_version: int,
    actor_id: str,
    expected_version: int,
    reason: str,
    totp_code: str,
    dependencies: ConfigurationDependencies,
) -> Draft:
    try:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise InvalidPolicyValue("rollback reason is required")
        active = owner.locked_active_snapshot(namespace)
        if active.scope != scope or active.version != expected_version:
            raise SourceStale(namespace)
        target = owner.version_snapshot(namespace, scope, to_version)
        if target is None:
            raise PolicyVersionNotFound(str(to_version))
        issues = owner.validate_candidate(
            namespace,
            schema_revision=active.schema_revision,
            values=target.values,
        )
        if issues:
            raise InvalidPolicyValue("historical policy is invalid under the current schema")
        if dependencies.identity is None:
            raise RuntimeError("configuration rollback identity dependencies are unavailable")
        try:
            verify_admin_totp(
                db,
                actor_id,
                totp_code,
                purpose="POLICY_ROLLBACK",
                dependencies=dependencies.identity,
            )
        except TotpChallengeFailed as exc:
            raise PolicyVerificationFailed(namespace) from exc
        content = dict(target.values)
        draft = owner.create_draft(
            id=str(dependencies.random.uuid4()),
            namespace=namespace,
            scope=scope,
            content=content,
            base_version=active.version,
            owner_id=actor_id,
            now=dependencies.clock.now(),
            schema_revision=active.schema_revision,
            content_hash=_content_hash(content),
            rollback_from_version=to_version,
        )
    except ConfigurationError as error:
        _audit(
            db,
            dependencies=dependencies,
            actor_id=actor_id,
            action="configuration.policy.rollback_denied",
            draft_id=f"{namespace}:{to_version}",
            target_type="configuration_policy_version",
            result="DENIED",
            reason=f"namespace={namespace}; reasonCode={_denial_code(error)}",
        )
        raise
    _audit(
        db,
        dependencies=dependencies,
        actor_id=actor_id,
        action="configuration.policy.rollback_draft_created",
        draft_id=draft.id,
        result="SUCCESS",
        reason=(
            f"namespace={namespace}; rollbackFromVersion={to_version}; "
            f"baseVersion={active.version}; reason={normalized_reason}"
        ),
    )
    return draft
