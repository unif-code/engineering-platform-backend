"""Owner-held lifecycle runtime; no foreign connection enters Requirement."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from sqlalchemy import Connection, Engine

from control_plane.app.modules.configuration import (
    ConfigurationDependencies,
    PolicyLifecycle,
    PolicySnapshotUnavailable,
)
from control_plane.app.modules.requirement.adapters.gate_policy import (
    SqlAlchemyGatePolicyRepository,
)
from control_plane.app.modules.requirement.domain.gate_policy import (
    NAMESPACE,
    GatePolicy,
    ResolvedGatePolicy,
)
from control_plane.app.modules.requirement.ports.gate_policy import (
    PolicyAuthorizationPort,
    PolicyReauthenticationPort,
)
from control_plane.app.shared.idempotency import (
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.shared.security import SecretManagerPort


class RequirementPolicyRuntime:
    namespace = NAMESPACE

    def __init__(
        self,
        engine: Engine,
        dependencies: ConfigurationDependencies,
        *,
        secret_manager: SecretManagerPort | None = None,
        reauthentication: PolicyReauthenticationPort | None = None,
        authorization: PolicyAuthorizationPort | None = None,
    ) -> None:
        self.engine, self.dependencies = engine, dependencies
        self.secret_manager, self.reauthentication, self.authorization = (
            secret_manager,
            reauthentication,
            authorization,
        )

    @staticmethod
    def owner(db: Connection) -> SqlAlchemyGatePolicyRepository:
        return SqlAlchemyGatePolicyRepository(db)

    @contextmanager
    def transaction(self) -> Iterator[PolicyLifecycle]:
        with self.engine.begin() as db:
            yield PolicyLifecycle(db, self.owner(db), self.dependencies)

    def resolved_snapshot(self) -> ResolvedGatePolicy:
        with self.transaction() as lifecycle:
            snapshot = lifecycle.owner.active_snapshot(NAMESPACE)
        return ResolvedGatePolicy(
            namespace=snapshot.namespace,
            scope=snapshot.scope,
            schema_revision=snapshot.schema_revision,
            version=snapshot.version,
            snapshot_hash=snapshot.snapshot_hash,
            policy=GatePolicy.parse(
                snapshot.values,
                namespace=snapshot.namespace,
                scope=snapshot.scope,
                schema_revision=snapshot.schema_revision,
            ),
        )

    def archive(self, *, now: datetime) -> int:
        with self.transaction() as lifecycle:
            return lifecycle.archive(now=now, namespace=NAMESPACE)

    def publish(
        self,
        *,
        actor_id: str,
        namespace: str,
        draft_id: str,
        expected_revision: int,
        reason: str,
        totp_code: str,
        idempotency_key: str,
        raw_session: str,
    ) -> IdempotentResponse:
        return self._command(
            operation="POLICY_PUBLISH",
            actor_id=actor_id,
            namespace=namespace,
            draft_id=draft_id,
            revision=expected_revision,
            reason=reason,
            totp_code=totp_code,
            idempotency_key=idempotency_key,
            raw_session=raw_session,
        )

    def rollback(
        self,
        *,
        actor_id: str,
        namespace: str,
        scope: str,
        to_version: int,
        expected_version: int,
        reason: str,
        totp_code: str,
        idempotency_key: str,
        raw_session: str,
    ) -> IdempotentResponse:
        return self._command(
            operation="POLICY_ROLLBACK",
            actor_id=actor_id,
            namespace=namespace,
            draft_id=None,
            revision=expected_version,
            scope=scope,
            to_version=to_version,
            reason=reason,
            totp_code=totp_code,
            idempotency_key=idempotency_key,
            raw_session=raw_session,
        )

    def _command(
        self,
        *,
        operation: Literal["POLICY_PUBLISH", "POLICY_ROLLBACK"],
        actor_id: str,
        namespace: str,
        draft_id: str | None,
        revision: int,
        reason: str,
        totp_code: str,
        idempotency_key: str,
        raw_session: str,
        scope: str = "PLATFORM",
        to_version: int | None = None,
    ) -> IdempotentResponse:
        from control_plane.app.modules.requirement.application.gate_policy import (
            protected_policy_command,
        )

        if (
            self.secret_manager is None
            or self.reauthentication is None
            or self.authorization is None
        ):
            raise PolicySnapshotUnavailable("Protected policy runtime unavailable")
        material = self.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation=operation,
            method="POST",
            path=f"/api/v1/admin/policies/{namespace}/"
            + (f"drafts/{draft_id}/publish" if draft_id else "rollback"),
            body={
                "scope": scope,
                "toVersion": to_version,
                "reason": reason,
                "totpCode": totp_code,
                "revision": revision,
            },
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        reauthentication, authorization = self.reauthentication, self.authorization
        with self.engine.begin() as db:
            repository = self.owner(db)
            execution = execute_idempotent(
                repository,
                actor=actor_id,
                operation=operation,
                key=idempotency_key,
                fingerprint=fingerprint,
                command=lambda: protected_policy_command(
                    db,
                    repository,
                    dependencies=self.dependencies,
                    reauthentication=reauthentication,
                    authorization=authorization,
                    operation=operation,
                    actor_id=actor_id,
                    namespace=namespace,
                    raw_session=raw_session,
                    totp_code=totp_code,
                    reason=reason,
                    attempt_id=str(self.dependencies.random.uuid4()),
                    fingerprint=fingerprint,
                    draft_id=draft_id or str(self.dependencies.random.uuid4()),
                    revision=revision,
                    scope=scope,
                    to_version=to_version,
                ),
                now=self.dependencies.clock.now,
                new_id=self.dependencies.random.uuid4,
                idempotency_sealing_key=material.idempotency_sealing_key,
            )
        return execution.response
