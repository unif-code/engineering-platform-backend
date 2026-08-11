from sqlalchemy import Engine

import control_plane.app.modules.identity as identity


class SqlAlchemyIdentitySessionValidator:
    def __init__(
        self,
        engine: Engine,
        dependencies: identity.IdentityDependencies,
    ) -> None:
        self.engine = engine
        self.dependencies = dependencies

    def validate(self, raw_token: str) -> identity.SessionPrincipal | None:
        with self.engine.begin() as db:
            return identity.validate_session(
                db,
                raw_token=raw_token,
                dependencies=self.dependencies,
                touch_activity=False,
            )
