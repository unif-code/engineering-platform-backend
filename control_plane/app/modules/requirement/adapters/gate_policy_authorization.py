"""Current session authorization at an explicit owner-independent decision point."""

from sqlalchemy import Engine

from control_plane.app.modules.authorization import (
    PLATFORM_CONFIGURATION_MANAGE,
    AuthorizationDependencies,
    DecisionDependencies,
    Scope,
    authorize,
    principal_version,
)
from control_plane.app.modules.configuration import (
    PolicySnapshotUnavailable,
    PolicyVerificationFailed,
)


class RequirementPolicyAuthorization:
    def __init__(
        self,
        engine: Engine,
        dependencies: AuthorizationDependencies,
        decision_dependencies: DecisionDependencies,
    ) -> None:
        self.engine, self.dependencies, self.decision_dependencies = (
            engine,
            dependencies,
            decision_dependencies,
        )

    def check(self, *, raw_session: str, actor_id: str) -> tuple[int, int]:
        try:
            with self.engine.begin() as db:
                before = principal_version(db, account_id=actor_id, dependencies=self.dependencies)
                if before is None or before.dirty_generation is not None:
                    raise PolicyVerificationFailed("Current authorization unavailable")
                decision = authorize(
                    db,
                    raw_token=raw_session,
                    capability=PLATFORM_CONFIGURATION_MANAGE,
                    scope=Scope.platform(),
                    dependencies=self.dependencies,
                    decision_dependencies=self.decision_dependencies,
                )
                after = principal_version(db, account_id=actor_id, dependencies=self.dependencies)
                principal = decision.principal
                if (
                    not decision.allowed
                    or principal is None
                    or principal.account_id != actor_id
                    or not principal.is_super_admin
                    or after is None
                    or after.dirty_generation is not None
                    or (before.version, before.fence_generation)
                    != (after.version, after.fence_generation)
                    or principal.authorization_version != after.version
                ):
                    raise PolicyVerificationFailed("Current authorization denied")
                return after.version, after.fence_generation
        except PolicyVerificationFailed:
            raise
        except Exception:
            raise PolicySnapshotUnavailable("Current authorization unavailable") from None
