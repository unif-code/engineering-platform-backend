"""Current Authorization facade decision, without holding foreign locks at owner commit."""

from sqlalchemy import Engine

from control_plane.app.modules.authorization import (
    PLATFORM_CONFIGURATION_MANAGE,
    AuthorizationDependencies,
    DecisionCode,
    DecisionDependencies,
    Scope,
    authorize,
    principal_version,
)
from control_plane.app.modules.configuration.domain import (
    PolicySnapshotUnavailable,
    RebaseAuthorizationDenied,
)


class CurrentRebaseAuthorization:
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

    def check(self, *, raw_session: str, actor_id: str) -> None:
        try:
            with self.engine.begin() as db:
                before = principal_version(db, account_id=actor_id, dependencies=self.dependencies)
                decision = authorize(
                    db,
                    raw_token=raw_session,
                    capability=PLATFORM_CONFIGURATION_MANAGE,
                    scope=Scope.platform(),
                    dependencies=self.dependencies,
                    decision_dependencies=self.decision_dependencies,
                )
                after = principal_version(db, account_id=actor_id, dependencies=self.dependencies)
        except Exception:
            raise PolicySnapshotUnavailable("Current rebase authorization unavailable") from None
        if decision.code is DecisionCode.UNAUTHENTICATED:
            raise RebaseAuthorizationDenied(401)
        if decision.code is DecisionCode.DENIED:
            raise RebaseAuthorizationDenied(403)
        principal = decision.principal
        if not decision.allowed or decision.code is not DecisionCode.ALLOW or principal is None:
            raise PolicySnapshotUnavailable("Current rebase authorization unavailable")
        if principal.account_id != actor_id or not principal.is_super_admin:
            raise RebaseAuthorizationDenied(403)
        if (
            before is None
            or after is None
            or before.dirty_generation is not None
            or after.dirty_generation is not None
            or (before.version, before.fence_generation) != (after.version, after.fence_generation)
            or principal.authorization_version != after.version
        ):
            raise PolicySnapshotUnavailable("Current rebase authorization changed")
