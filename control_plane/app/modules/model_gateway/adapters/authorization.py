from dataclasses import dataclass

from sqlalchemy import Engine

import control_plane.app.modules.authorization as authorization
import control_plane.app.modules.identity as identity
from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked, CheckReason


@dataclass(frozen=True)
class CurrentModelManager:
    identity_engine: Engine
    identity_dependencies: identity.IdentityDependencies
    authorization_engine: Engine
    authorization_dependencies: authorization.AuthorizationDependencies

    def require_manager(self, account_id: str) -> None:
        try:
            with self.authorization_engine.connect() as db:
                before = authorization.principal_version(
                    db, account_id=account_id, dependencies=self.authorization_dependencies
                )
            with self.identity_engine.connect() as db:
                account = identity.get_account(
                    db, account_id=account_id, dependencies=self.identity_dependencies
                )
            if (
                not account.is_super_admin
                or str(account.status) != "ENABLED"
                or account.password_set_at is None
                or account.totp_confirmed_at is None
            ):
                raise CheckBlocked(CheckReason.ACTOR_INELIGIBLE)
            with self.authorization_engine.begin() as db:
                after = authorization.principal_version(
                    db, account_id=account_id, dependencies=self.authorization_dependencies
                )
                if before is None or after != before or after.dirty_generation is not None:
                    raise CheckBlocked(CheckReason.AUTHORIZATION_UNAVAILABLE)
                principal = authorization.AuthorizationPrincipal(
                    account_id=account.id,
                    employee_id=account.employee_no,
                    name=account.display_name,
                    is_super_admin=account.is_super_admin,
                    authorization_version=after.version,
                    capabilities=(),
                )
                if not authorization.principal_has_capability(
                    db,
                    principal=principal,
                    capability="platform.model.manage",
                    scope=authorization.Scope.platform(),
                    dependencies=self.authorization_dependencies,
                ):
                    raise CheckBlocked(CheckReason.ACTOR_INELIGIBLE)
        except CheckBlocked:
            raise
        except identity.AccountNotFound:
            raise CheckBlocked(CheckReason.ACTOR_INELIGIBLE) from None
        except Exception:
            raise CheckBlocked(CheckReason.AUTHORIZATION_UNAVAILABLE) from None
