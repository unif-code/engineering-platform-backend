from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class RequirementRepository(Protocol):
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

    def work_item_by_id(
        self,
        work_item_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def bind_work_item(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        base_commit_sha: str,
        task_branch: str,
        state: str,
        now: datetime,
    ) -> Any: ...

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

    def set_current_sdd_baseline(
        self,
        requirement_id: str,
        *,
        baseline_id: str,
        expected_revision: int,
        now: datetime,
    ) -> Any: ...

    def insert_sdd_baseline(self, **values: Any) -> Any: ...

    def sdd_baseline_by_id(self, baseline_id: str) -> Any: ...

    def sdd_baseline_by_artifact(
        self,
        requirement_id: str,
        artifact_id: str,
        artifact_version: str,
    ) -> Any: ...

    def insert_gate(self, **values: Any) -> Any: ...

    def gate_by_id(self, gate_id: str, *, for_update: bool = False) -> Any: ...

    def gate_by_baseline_id(self, baseline_id: str) -> Any: ...

    def insert_gate_assignment(self, **values: Any) -> Any: ...

    def current_gate_assignment(self, gate_id: str, *, for_update: bool = False) -> Any: ...

    def insert_decision(self, **values: Any) -> Any: ...

    def close_gate(
        self,
        gate_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any: ...


class RequirementRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> RequirementRepository: ...
