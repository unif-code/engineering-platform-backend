from sqlalchemy import Connection

from control_plane.app.modules.configuration.domain import PolicySnapshotUnavailable
from control_plane.app.modules.identity import (
    EffectiveIdentityPolicy,
    OwnedPolicySnapshotUnavailable,
    effective_identity_policy,
)


class IdentityEffectivePolicy:
    """Production adapter delegating typed semantics to the Identity owner."""

    def get_identity_policy(self, db: Connection) -> EffectiveIdentityPolicy:
        try:
            return effective_identity_policy(db)
        except OwnedPolicySnapshotUnavailable as exc:
            raise PolicySnapshotUnavailable("identity") from exc
