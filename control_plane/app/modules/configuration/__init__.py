"""Public configuration Facade."""

from datetime import datetime

from sqlalchemy import Connection

from control_plane.app.modules.configuration.adapters import IdentityPolicyOwner
from control_plane.app.modules.configuration.application import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application import active_snapshot as _active_snapshot
from control_plane.app.modules.configuration.application import (
    archive_stale_drafts as _archive_stale_drafts,
)
from control_plane.app.modules.configuration.application import catalog as _catalog
from control_plane.app.modules.configuration.application import create_draft as _create_draft
from control_plane.app.modules.configuration.application import policy_versions as _policy_versions
from control_plane.app.modules.configuration.application import preview as _preview
from control_plane.app.modules.configuration.application import publish as _publish
from control_plane.app.modules.configuration.application import rollback as _rollback
from control_plane.app.modules.configuration.application import update_draft as _update_draft
from control_plane.app.modules.configuration.application import validate_draft as _validate_draft
from control_plane.app.modules.configuration.domain import (
    ConfigurationError,
    Draft,
    DraftArchived,
    DraftNotFound,
    DraftOwnerRequired,
    DraftValidation,
    InvalidPolicyValue,
    PolicyKey,
    PolicySnapshot,
    PolicySnapshotUnavailable,
    PolicyVerificationFailed,
    PolicyVersionNotFound,
    Preview,
    PreviewItem,
    PublishedVersion,
    SourceStale,
    StaleDraftBase,
    StaleDraftRevision,
    ValidationIssue,
)


def catalog(db: Connection, namespace: str = "identity") -> list[PolicyKey]:
    return _catalog(IdentityPolicyOwner(db), namespace)


def archive_stale_drafts(
    db: Connection,
    *,
    now: datetime,
    dependencies: ConfigurationDependencies,
    namespace: str = "identity",
    scope: str = "PLATFORM",
) -> int:
    return _archive_stale_drafts(
        db,
        IdentityPolicyOwner(db),
        now=now,
        dependencies=dependencies,
        namespace=namespace,
        scope=scope,
    )


def active_snapshot(db: Connection, namespace: str) -> PolicySnapshot:
    return _active_snapshot(IdentityPolicyOwner(db), namespace)


def create_draft(
    db: Connection,
    *,
    namespace: str,
    values: dict[str, object],
    actor_id: str,
    dependencies: ConfigurationDependencies,
) -> Draft:
    return _create_draft(
        db,
        IdentityPolicyOwner(db),
        namespace=namespace,
        values=values,
        actor_id=actor_id,
        dependencies=dependencies,
    )


def update_draft(
    db: Connection,
    *,
    namespace: str,
    draft_id: str,
    values: dict[str, object],
    actor_id: str,
    expected_revision: int,
    dependencies: ConfigurationDependencies,
) -> Draft:
    return _update_draft(
        db,
        IdentityPolicyOwner(db),
        namespace=namespace,
        draft_id=draft_id,
        values=values,
        actor_id=actor_id,
        expected_revision=expected_revision,
        dependencies=dependencies,
    )


def validate_draft(
    db: Connection,
    *,
    namespace: str,
    draft_id: str,
    actor_id: str,
    expected_revision: int,
    dependencies: ConfigurationDependencies,
) -> DraftValidation:
    return _validate_draft(
        db,
        IdentityPolicyOwner(db),
        namespace=namespace,
        draft_id=draft_id,
        actor_id=actor_id,
        expected_revision=expected_revision,
        dependencies=dependencies,
    )


def preview(
    db: Connection,
    *,
    namespace: str,
    draft_id: str,
    actor_id: str,
    expected_revision: int,
    dependencies: ConfigurationDependencies,
) -> Preview:
    return _preview(
        db,
        IdentityPolicyOwner(db),
        namespace=namespace,
        draft_id=draft_id,
        actor_id=actor_id,
        expected_revision=expected_revision,
        dependencies=dependencies,
    )


def publish(
    db: Connection,
    *,
    namespace: str,
    draft_id: str,
    actor_id: str,
    expected_revision: int,
    reason: str,
    totp_code: str,
    dependencies: ConfigurationDependencies,
) -> PublishedVersion:
    return _publish(
        db,
        IdentityPolicyOwner(db),
        namespace=namespace,
        draft_id=draft_id,
        actor_id=actor_id,
        expected_revision=expected_revision,
        reason=reason,
        totp_code=totp_code,
        dependencies=dependencies,
    )


def rollback(
    db: Connection,
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
    return _rollback(
        db,
        IdentityPolicyOwner(db),
        namespace=namespace,
        scope=scope,
        to_version=to_version,
        actor_id=actor_id,
        expected_version=expected_version,
        reason=reason,
        totp_code=totp_code,
        dependencies=dependencies,
    )


def policy_versions(
    db: Connection,
    namespace: str,
    *,
    scope: str = "PLATFORM",
    before_version: int | None = None,
    limit: int = 50,
) -> tuple[list[PublishedVersion], int | None]:
    return _policy_versions(
        IdentityPolicyOwner(db),
        namespace,
        scope=scope,
        before_version=before_version,
        limit=limit,
    )


__all__ = [
    "PolicyKey",
    "ConfigurationDependencies",
    "ConfigurationError",
    "Draft",
    "DraftArchived",
    "DraftNotFound",
    "DraftOwnerRequired",
    "DraftValidation",
    "InvalidPolicyValue",
    "PolicySnapshot",
    "PolicySnapshotUnavailable",
    "PolicyVerificationFailed",
    "PolicyVersionNotFound",
    "Preview",
    "PreviewItem",
    "PublishedVersion",
    "SourceStale",
    "StaleDraftBase",
    "StaleDraftRevision",
    "ValidationIssue",
    "active_snapshot",
    "archive_stale_drafts",
    "catalog",
    "create_draft",
    "preview",
    "policy_versions",
    "publish",
    "rollback",
    "update_draft",
    "validate_draft",
]
