from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class SourceControlRepository(Protocol):
    db: Connection

    def insert_workspace_repository(self, **values: Any) -> Any: ...

    def workspace_repository(
        self,
        repository_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def remove_workspace_repository(
        self,
        repository_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any: ...

    def accept_binding_request(self, **values: Any) -> Any: ...

    def binding_request(self, message_id: str, *, for_update: bool = False) -> Any: ...

    def claim_binding_requests(
        self,
        *,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def pending_binding_request_ids(self, *, limit: int, now: datetime) -> list[str]: ...

    def claim_binding_request(
        self,
        message_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any: ...

    def complete_binding_request(self, message_id: str, *, now: datetime) -> Any: ...

    def next_work_item_number(self) -> int: ...

    def insert_effect(self, **values: Any) -> Any: ...

    def effect_by_work_item(
        self,
        work_item_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def effect_by_id(self, effect_id: str) -> Any: ...

    def transition_effect(
        self,
        effect_id: str,
        *,
        expected_state: str,
        values: Mapping[str, object],
    ) -> Any: ...

    def claim_unknown_effects(
        self,
        *,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def pending_callback_effects(self, *, limit: int) -> list[Any]: ...

    def insert_binding(self, **values: Any) -> Any: ...

    def binding_by_work_item(self, work_item_id: str) -> Any: ...

    def accept_webhook(self, **values: Any) -> Any: ...

    def webhook_by_message(self, repository_id: str, webhook_id: str) -> Any: ...

    def webhook_by_id(self, inbox_id: str, *, for_update: bool = False) -> Any: ...

    def pending_webhook_ids(self, *, limit: int) -> list[str]: ...

    def make_unknown_effect_due(
        self,
        *,
        repository_id: str,
        branch_name: str,
        now: datetime,
    ) -> int: ...

    def complete_webhook(self, inbox_id: str, *, now: datetime) -> Any: ...


class SourceControlRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> SourceControlRepository: ...
