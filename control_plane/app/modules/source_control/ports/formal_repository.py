from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class SourceControlFormalRepository(Protocol):
    db: Connection

    def formal_request(self, message_id: str, *, for_update: bool = False) -> Any: ...

    def formal_request_for_effect(
        self,
        *,
        operation: str,
        work_item_id: str,
        requirement_id: str,
        repository_id: str,
        request_fingerprint: str,
    ) -> Any: ...

    def insert_formal_request(self, **values: Any) -> Any: ...

    def claim_formal_request(
        self,
        message_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any: ...

    def pending_formal_request_candidates(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> list[Any]: ...

    def complete_formal_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
    ) -> Any: ...

    def complete_formal_request_blocked(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        reason_code: str,
        now: datetime,
    ) -> Any: ...

    def fail_formal_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Any: ...

    def repository_by_id(self, repository_id: str) -> Any: ...

    def branch_binding_by_work_item(self, work_item_id: str) -> Any: ...

    def formal_binding_by_work_item(self, work_item_id: str) -> Any: ...

    def merge_request_binding_by_id(self, binding_id: str) -> Any: ...

    def insert_merge_request_binding(self, **values: Any) -> Any: ...

    def append_merge_request_observation(self, **values: Any) -> Any: ...

    def latest_merge_request_observation(self, binding_id: str) -> Any: ...

    def effect_by_operation_subject(
        self,
        operation: str,
        subject_key: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def effect_by_id(self, effect_id: str, *, for_update: bool = False) -> Any: ...

    def insert_effect(self, **values: Any) -> Any: ...

    def transition_effect(
        self,
        effect_id: str,
        *,
        expected_state: str,
        expected_attempts: int,
        values: Mapping[str, object],
    ) -> Any: ...

    def claim_effect(
        self,
        effect_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any: ...

    def claim_effects(
        self,
        *,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def pending_callback_effects(self, *, limit: int) -> list[Any]: ...

    def insert_formal_review_assignment(self, **values: Any) -> Any: ...

    def current_formal_review_assignment(
        self,
        binding_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def formal_review_assignment_by_acceptance(
        self,
        binding_id: str,
        acceptance_decision_id: str,
    ) -> Any: ...

    def supersede_formal_review_assignment(
        self,
        assignment_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any: ...


class SourceControlFormalRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> SourceControlFormalRepository: ...
