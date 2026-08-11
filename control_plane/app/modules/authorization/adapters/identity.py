from typing import Any

from sqlalchemy import Engine, text

import control_plane.app.modules.identity as identity


class SqlAlchemyIdentitySessionValidator:
    def __init__(
        self,
        engine: Engine,
        dependencies: identity.IdentityDependencies,
        *,
        security_changes: Any | None = None,
    ) -> None:
        self.engine = engine
        self.dependencies = dependencies
        self.security_changes = security_changes

    def validate(self, raw_token: str) -> identity.SessionPrincipal | None:
        change_source = None
        try:
            with self.engine.begin() as db:
                source_transaction_id = str(
                    db.execute(text("SELECT pg_current_xact_id()")).scalar_one()
                )
                with identity.identity_change_source(
                    actor="SYSTEM",
                    operation="identity_session_validate",
                    idempotency_key=str(self.dependencies.random.uuid4()),
                    source_transaction_id=source_transaction_id,
                ) as change_source:
                    principal = identity.validate_session(
                        db,
                        raw_token=raw_token,
                        dependencies=self.dependencies,
                        touch_activity=False,
                    )
        except Exception:
            self._reconcile(change_source)
            raise
        if not self._reconcile(change_source):
            return None
        return principal

    def _reconcile(self, change_source: Any | None) -> bool:
        if self.security_changes is None:
            return True
        try:
            if change_source is not None:
                for ticket in change_source.tickets:
                    self.security_changes.complete(ticket)
            self.security_changes.reconcile_pending()
        except Exception:
            return False
        return True
