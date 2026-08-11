from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class OrganizationRepository(Protocol):
    db: Connection

    def lock_structure(self) -> None: ...

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

    def all_edges(self) -> list[Any]: ...

    def upsert_edge(
        self,
        *,
        account_id: str,
        superior_id: str | None,
        kind: str,
        now: datetime,
    ) -> None: ...


class OrganizationRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> OrganizationRepository: ...
