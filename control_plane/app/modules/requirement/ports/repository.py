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

    def completed_idempotency_by_fingerprint(
        self,
        actor: str,
        operation: str,
        request_fingerprint: str,
    ) -> list[Any]: ...

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

    def insert_sdd_artifact_version(self, **values: Any) -> Any: ...

    def sdd_artifact_version(
        self,
        requirement_id: str,
        artifact_id: str,
        version: int,
    ) -> Any: ...

    def sdd_artifact_version_by_identity(
        self,
        artifact_id: str,
        version: int,
    ) -> Any: ...

    def latest_sdd_artifact_version(
        self,
        requirement_id: str,
        artifact_id: str,
    ) -> Any: ...

    def insert_work_item_assignment(self, **values: Any) -> Any: ...

    def current_work_item_assignment(
        self,
        work_item_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def supersede_work_item_assignment(
        self,
        assignment_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any: ...

    def repository_binding_context(self, work_item_id: str) -> Any: ...

    def integration_delivery_context(self, work_item_id: str) -> Any: ...

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

    def block_work_item(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        now: datetime,
    ) -> Any: ...

    def update_work_item_delivery(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        state: str,
        delivery_state: str,
        binding_id: str | None,
        blocked_reason: str | None,
        now: datetime,
    ) -> Any: ...

    def required_work_item_states(self, requirement_id: str) -> tuple[str, ...]: ...

    def insert_outbox(self, **values: Any) -> Any: ...

    def claim_binding_requests(
        self,
        *,
        limit: int,
        available_before: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def claim_delivery_requests(
        self,
        *,
        limit: int,
        available_before: datetime,
        lease_until: datetime,
    ) -> list[Any]: ...

    def outbox_by_id(self, message_id: str, *, for_update: bool = False) -> Any: ...

    def publish_outbox(self, message_id: str, *, now: datetime) -> Any: ...

    def release_outbox(
        self,
        message_id: str,
        *,
        error_code: str,
        available_at: datetime,
    ) -> Any: ...

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
