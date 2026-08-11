from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class WorkspaceRepository(Protocol):
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

    def insert_workspace(self, *, workspace_id: str, name: str, owner_id: str) -> Any: ...

    def workspace_by_id(self, workspace_id: str, *, for_update: bool = False) -> Any: ...

    def list_workspaces(self) -> list[Any]: ...

    def leader_ids(self, workspace_id: str) -> list[str]: ...

    def insert_leader(self, *, workspace_id: str, account_id: str, invited_by: str) -> bool: ...

    def delete_leader(self, *, workspace_id: str, account_id: str) -> bool: ...

    def update_owner(self, *, workspace_id: str, owner_id: str) -> None: ...

    def bump_version(self, workspace_id: str) -> Any: ...

    def projection_rows(self, workspace_id: str) -> list[Any]: ...

    def replace_members(
        self,
        workspace_id: str,
        members: dict[str, str],
        *,
        computed_at: datetime,
    ) -> None: ...

    def active_workspace_ids(self) -> list[str]: ...


class WorkspaceRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> WorkspaceRepository: ...
