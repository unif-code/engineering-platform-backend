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

    def route_registry(self) -> list[Any]: ...


class AuthorizationRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> AuthorizationRepository: ...
