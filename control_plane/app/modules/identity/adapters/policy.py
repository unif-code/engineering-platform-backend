from sqlalchemy import Connection

from control_plane.app.modules.identity.domain.policy import EffectiveIdentityPolicy


class DefaultEffectivePolicy:
    """Pre-persistence adapter; Task 11 replaces it with the active snapshot."""

    def get_identity_policy(self, db: Connection) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy()
