from sqlalchemy import Connection

from control_plane.app.modules.identity.domain.policy import EffectiveIdentityPolicy


class DefaultEffectivePolicy:
    """Explicit bootstrap/test fallback; never used by production HTTP assembly."""

    def get_identity_policy(self, db: Connection) -> EffectiveIdentityPolicy:
        del db
        return EffectiveIdentityPolicy()
