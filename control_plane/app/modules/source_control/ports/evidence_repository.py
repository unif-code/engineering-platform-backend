from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Connection


class SourceControlEvidenceRepository(Protocol):
    db: Connection

    def external_validation_by_id(self, validation_id: str) -> Any: ...

    def external_validation_receipt(self, message_id: str) -> Any: ...

    def external_validation_by_hash(
        self,
        *,
        work_item_id: str,
        reference_hash: str,
    ) -> Any: ...

    def insert_external_validation(self, **values: Any) -> Any: ...

    def insert_external_validation_receipt(self, **values: Any) -> Any: ...

    def latest_external_validation(
        self,
        *,
        work_item_id: str,
        binding_id: str,
        target_commit_sha: str,
        integration_merge_commit_sha: str,
    ) -> Any: ...

    def integration_evidence_context(self, work_item_id: str) -> Any: ...

    def evidence_request(
        self,
        message_id: str,
        *,
        for_update: bool = False,
    ) -> Any: ...

    def insert_evidence_request(self, **values: Any) -> Any: ...

    def claim_evidence_request(
        self,
        message_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any: ...

    def pending_evidence_request_ids(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> list[str]: ...

    def complete_evidence_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
    ) -> Any: ...

    def fail_evidence_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Any: ...

    def insert_integration_baseline_evidence(self, **values: Any) -> Any: ...

    def insert_integration_baseline_evidence_item(self, **values: Any) -> Any: ...

    def integration_baseline_evidence_by_id(self, evidence_id: str) -> Any: ...

    def integration_baseline_evidence_by_snapshot(
        self,
        delivery_snapshot_id: str,
        delivery_snapshot_hash: str,
    ) -> Any: ...

    def integration_baseline_evidence_items(self, evidence_id: str) -> list[Any]: ...


class SourceControlEvidenceRepositoryFactory(Protocol):
    def __call__(self, db: Connection) -> SourceControlEvidenceRepository: ...
