from datetime import datetime
from typing import Any

from sqlalchemy import Engine

from control_plane.app.modules.identity.adapters.configuration_policy import (
    SqlAlchemyIdentityPolicyOwnerRepository,
)
from control_plane.app.modules.identity.application.dependencies import IdentityDependencies
from control_plane.app.modules.identity.application.policy_commands import (
    publish_policy_command,
    rollback_policy_command,
)
from control_plane.app.shared.idempotency import (
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)


class _PolicyIdempotencyRepository:
    def __init__(self, repository: SqlAlchemyIdentityPolicyOwnerRepository) -> None:
        self._repository = repository

    def claim_idempotency(self, **values: Any) -> bool:
        return self._repository.claim_configuration_idempotency(**values)

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any:
        return self._repository.configuration_idempotency_by_scope(
            actor,
            operation,
            idempotency_key,
            for_update=for_update,
        )

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool:
        return self._repository.complete_configuration_idempotency(
            record_id,
            http_status=http_status,
            result_metadata=result_metadata,
            sealed_response=sealed_response,
            now=now,
        )


class IdentityPolicyCommandRuntime:
    """Identity-owned source transaction boundary for protected policy commands."""

    def __init__(self, engine: Engine, dependencies: IdentityDependencies) -> None:
        self._engine = engine
        self._dependencies = dependencies

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
    ) -> IdempotentResponse:
        material = self._dependencies.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation="draft_publish",
            method="POST",
            path=f"/api/v1/admin/policies/{namespace}/drafts/{draft_id}/publish",
            body={
                "reason": reason,
                "totpCode": totp_code,
                "expectedRevision": expected_revision,
            },
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        with self._engine.begin() as db:
            policy_repository = SqlAlchemyIdentityPolicyOwnerRepository(db)
            identity_repository = self._dependencies.repository_factory(db)
            execution = execute_idempotent(
                _PolicyIdempotencyRepository(policy_repository),
                actor=actor_id,
                operation="draft_publish",
                key=idempotency_key,
                fingerprint=fingerprint,
                command=lambda: publish_policy_command(
                    policy_repository,
                    identity_repository,
                    actor_id=actor_id,
                    namespace=namespace,
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    reason=reason,
                    totp_code=totp_code,
                    dependencies=self._dependencies,
                ),
                now=self._dependencies.clock.now,
                new_id=self._dependencies.random.uuid4,
                idempotency_sealing_key=material.idempotency_sealing_key,
            )
        return execution.response

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
    ) -> IdempotentResponse:
        material = self._dependencies.secret_manager.load()
        fingerprint = canonical_request_fingerprint(
            operation="policy_rollback",
            method="POST",
            path=f"/api/v1/admin/policies/{namespace}/rollback",
            body={
                "scope": scope,
                "toVersion": to_version,
                "reason": reason,
                "totpCode": totp_code,
                "expectedVersion": expected_version,
            },
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
        with self._engine.begin() as db:
            policy_repository = SqlAlchemyIdentityPolicyOwnerRepository(db)
            identity_repository = self._dependencies.repository_factory(db)
            execution = execute_idempotent(
                _PolicyIdempotencyRepository(policy_repository),
                actor=actor_id,
                operation="policy_rollback",
                key=idempotency_key,
                fingerprint=fingerprint,
                command=lambda: rollback_policy_command(
                    policy_repository,
                    identity_repository,
                    actor_id=actor_id,
                    namespace=namespace,
                    scope=scope,
                    to_version=to_version,
                    expected_version=expected_version,
                    reason=reason,
                    totp_code=totp_code,
                    dependencies=self._dependencies,
                ),
                now=self._dependencies.clock.now,
                new_id=self._dependencies.random.uuid4,
                idempotency_sealing_key=material.idempotency_sealing_key,
            )
        return execution.response
