from typing import Protocol

from sqlalchemy import Connection

from control_plane.app.modules.identity.domain.policy import EffectiveIdentityPolicy


class EffectivePolicyPort(Protocol):
    def get_identity_policy(self, db: Connection) -> EffectiveIdentityPolicy: ...
