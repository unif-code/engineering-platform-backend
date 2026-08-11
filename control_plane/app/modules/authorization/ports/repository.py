from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class AuthorizationRepository(Protocol):
    db: Connection

    def claim_idempotency(self, **values: Any) -> bool: ...

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool: ...

    def insert_grant(self, **values: Any) -> Any: ...

    def grant_by_id(self, grant_id: str, *, for_update: bool = False) -> Any: ...

    def list_grants(self) -> list[Any]: ...

    def effective_grants(
        self,
        *,
        principal_id: str,
        capability: str | None,
        scope_type: str | None,
        scope_id: str | None,
        now: datetime,
    ) -> list[Any]: ...

    def revoke_grant(
        self,
        *,
        grant_id: str,
        expected_version: int,
        actor_id: str,
        reason: str,
        now: datetime,
    ) -> Any: ...

    def bump_principal_version(self, account_id: str, now: datetime) -> Any: ...

    def principal_version(self, account_id: str, *, for_update: bool = False) -> Any: ...

    def mark_fence(self, account_id: str, reason: str, now: datetime) -> int: ...

    def clear_fence(self, account_id: str, generation: int, now: datetime) -> bool: ...

    def converge_fence(self, account_id: str, generation: int, now: datetime) -> Any: ...

    def principal_ids(self) -> list[str]: ...

    def lock_convergence_source(
        self,
        source_module: str,
        actor: str,
        operation: str,
        idempotency_key: str,
        idempotency_claim_id: str | None,
    ) -> None: ...

    def convergence_work_by_source(
        self,
        source_module: str,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        idempotency_claim_id: str | None = None,
        for_update: bool = False,
    ) -> Any: ...

    def convergence_work_by_id(
        self,
        work_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def convergence_status_for_claim(
        self,
        source_module: str,
        idempotency_claim_id: str,
    ) -> str | None: ...

    def insert_convergence_work(self, **values: Any) -> Any: ...

    def insert_pending_principal(self, **values: Any) -> None: ...

    def source_transaction_status(self, source_transaction_id: str) -> str: ...

    def update_convergence_effects(
        self,
        work_id: str,
        *,
        affected_account_ids: list[str],
        affected_workspace_ids: list[str],
        recompute_membership: bool,
        now: datetime,
    ) -> Any: ...

    def update_convergence_phase(
        self,
        work_id: str,
        phase: str,
        now: datetime,
    ) -> None: ...

    def complete_convergence_work(self, work_id: str, now: datetime) -> None: ...

    def cancel_convergence_work(self, work_id: str, now: datetime) -> None: ...

    def settle_pending_principal(
        self,
        work_id: str,
        account_id: str,
        *,
        bump_version: bool,
        now: datetime,
    ) -> Any: ...

    def pending_convergence_for_account(self, account_id: str) -> list[str]: ...

    def pending_convergence_work_ids(self) -> list[str]: ...

    def route_registry(self) -> list[Any]: ...


class AuthorizationRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> AuthorizationRepository: ...
