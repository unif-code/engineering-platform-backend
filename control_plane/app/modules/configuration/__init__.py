"""Public configuration Facade."""

from sqlalchemy import Connection

from control_plane.app.modules.configuration.adapters import IdentityPolicyOwner
from control_plane.app.modules.configuration.application import (
    ConfigurationDependencies,
)
from control_plane.app.modules.configuration.application import active_snapshot as _active_snapshot
from control_plane.app.modules.configuration.application import catalog as _catalog
from control_plane.app.modules.configuration.application import create_draft as _create_draft
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
    StaleDraftBase,
    StaleDraftRevision,
    ValidationIssue,
)


def catalog(db: Connection, namespace: str = "identity") -> list[PolicyKey]:
    return _catalog(IdentityPolicyOwner(db), namespace)


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
    "StaleDraftBase",
    "StaleDraftRevision",
    "ValidationIssue",
    "active_snapshot",
    "catalog",
    "create_draft",
    "update_draft",
    "validate_draft",
]
