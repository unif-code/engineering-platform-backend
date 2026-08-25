from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class RequirementRepository(Protocol):
    db: Connection

    def insert_requirement(self, **values: Any) -> Any: ...

    def requirement_by_id(
        self,
        requirement_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def list_requirements(
        self,
        *,
        workspace_id: str,
        after_created_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[Any]: ...

    def insert_work_item(self, **values: Any) -> Any: ...

    def work_items(self, requirement_id: str) -> list[Any]: ...

    def insert_outbox(self, **values: Any) -> Any: ...

    def outbox_by_aggregate(
        self,
        aggregate_id: str,
        *,
        aggregate_version: int,
    ) -> list[Any]: ...

    def update_requirement_state(
        self,
        requirement_id: str,
        *,
        expected_revision: int,
        state: str,
        now: datetime,
    ) -> Any: ...


class RequirementRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> RequirementRepository: ...
