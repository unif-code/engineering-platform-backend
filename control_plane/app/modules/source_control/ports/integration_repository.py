from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class SourceControlIntegrationRepository(Protocol):
    db: Connection

    def accept_delivery_request(self, **values: Any) -> Any: ...

    def delivery_request(
        self,
        message_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def claim_delivery_requests(
        self,
        *,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def claim_delivery_request(
        self,
        message_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any: ...

    def record_preflight_outcome(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        reason_code: str,
        now: datetime,
    ) -> Any: ...

    def release_delivery_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        error_code: str,
        retry_at: datetime,
        now: datetime,
    ) -> Any: ...

    def complete_delivery_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
    ) -> Any: ...

    def effect_by_operation_subject(
        self,
        operation: str,
        subject_key: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def effect_by_operation_work_item_fingerprint(
        self,
        operation: str,
        work_item_id: str,
        request_fingerprint: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def insert_effect(self, **values: Any) -> Any: ...

    def transition_effect(
        self,
        effect_id: str,
        *,
        expected_state: str,
        expected_attempts: int,
        values: Mapping[str, object],
    ) -> Any: ...

    def claim_effects(
        self,
        *,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def branch_binding_by_work_item(self, work_item_id: str) -> Any: ...

    def insert_merge_request_binding(self, **values: Any) -> Any: ...

    def merge_request_binding_by_id(self, binding_id: str) -> Any: ...

    def merge_request_binding_by_work_item(self, work_item_id: str) -> Any: ...

    def append_merge_request_observation(self, **values: Any) -> Any: ...

    def latest_merge_request_observation(self, binding_id: str) -> Any: ...

    def pending_callback_effects(self, *, limit: int) -> list[Any]: ...


class SourceControlIntegrationRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> SourceControlIntegrationRepository: ...
