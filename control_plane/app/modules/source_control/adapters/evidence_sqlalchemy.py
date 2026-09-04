import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text


class SqlAlchemySourceControlEvidenceRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def external_validation_by_id(self, validation_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.external_validation_reference "
                    "WHERE id=:validation_id"
                ),
                {"validation_id": validation_id},
            )
            .mappings()
            .one_or_none()
        )

    def external_validation_receipt(self, message_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.external_validation_receipt "
                    "WHERE message_id=:message_id"
                ),
                {"message_id": message_id},
            )
            .mappings()
            .one_or_none()
        )

    def external_validation_by_hash(
        self,
        *,
        work_item_id: str,
        reference_hash: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.external_validation_reference "
                    "WHERE work_item_id=:work_item_id AND reference_hash=:reference_hash"
                ),
                {
                    "work_item_id": work_item_id,
                    "reference_hash": reference_hash,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_external_validation(self, **values: Any) -> Any:
        parameters = {
            **values,
            "artifact_references": json.dumps(
                values["artifact_references"],
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.external_validation_reference "
                    "(id, work_item_id, requirement_id, workspace_id, "
                    "integration_merge_request_binding_id, target_commit_sha, "
                    "integration_merge_commit_sha, reference, notes, artifact_references, "
                    "reference_hash, request_fingerprint, submitted_by, submitted_at) VALUES "
                    "(:id, :work_item_id, :requirement_id, :workspace_id, "
                    ":integration_merge_request_binding_id, :target_commit_sha, "
                    ":integration_merge_commit_sha, :reference, :notes, "
                    "CAST(:artifact_references AS JSONB), :reference_hash, "
                    ":request_fingerprint, :submitted_by, :submitted_at) "
                    "ON CONFLICT (work_item_id, reference_hash) "
                    "DO NOTHING RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one_or_none()
        )

    def insert_external_validation_receipt(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.external_validation_receipt "
                    "(message_id, request_fingerprint, outcome, "
                    "canonical_external_validation_id, requirement_id, work_item_id, "
                    "rejection_reason_code, received_at) VALUES "
                    "(:message_id, :request_fingerprint, :outcome, "
                    ":canonical_external_validation_id, :requirement_id, "
                    ":work_item_id, :rejection_reason_code, :received_at) "
                    "ON CONFLICT (message_id) DO NOTHING RETURNING *"
                ),
                values,
            )
            .mappings()
            .one_or_none()
        )

    def latest_external_validation(
        self,
        *,
        work_item_id: str,
        binding_id: str,
        target_commit_sha: str,
        integration_merge_commit_sha: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.external_validation_reference "
                    "WHERE work_item_id=:work_item_id "
                    "AND integration_merge_request_binding_id=:binding_id "
                    "AND target_commit_sha=:target_commit_sha "
                    "AND integration_merge_commit_sha=:integration_merge_commit_sha "
                    "ORDER BY submitted_at DESC, id DESC LIMIT 1"
                ),
                {
                    "work_item_id": work_item_id,
                    "binding_id": binding_id,
                    "target_commit_sha": target_commit_sha,
                    "integration_merge_commit_sha": integration_merge_commit_sha,
                },
            )
            .mappings()
            .one_or_none()
        )

    def integration_evidence_context(self, work_item_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT binding.id AS binding_id, binding.kind, binding.work_item_id, "
                    "binding.requirement_id, binding.workspace_id, binding.repository_id, "
                    "binding.merge_request_iid, binding.head_sha AS binding_head_sha, "
                    "branch.branch_name AS task_branch, observation.head_sha, "
                    "observation.state AS observation_state, "
                    "observation.merge_commit_sha, observation.observed_at "
                    "FROM source_control.merge_request_binding AS binding "
                    "JOIN source_control.repository_branch_binding AS branch "
                    "ON branch.id=binding.branch_binding_id "
                    "LEFT JOIN LATERAL ("
                    "SELECT head_sha, state, merge_commit_sha, observed_at "
                    "FROM source_control.merge_request_observation "
                    "WHERE binding_id=binding.id "
                    "ORDER BY observed_at DESC, id DESC LIMIT 1"
                    ") AS observation ON TRUE "
                    "WHERE binding.work_item_id=:work_item_id "
                    "AND binding.kind='INTEGRATION' "
                    "AND binding.superseded_at IS NULL"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def evidence_request(
        self,
        message_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.evidence_request_inbox "
                    f"WHERE message_id=:message_id{suffix}"
                ),
                {"message_id": message_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_evidence_request(self, **values: Any) -> Any:
        parameters = {
            **values,
            "work_item_ids": json.dumps(values["work_item_ids"], separators=(",", ":")),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.evidence_request_inbox "
                    "(message_id, topic, payload_hash, delivery_snapshot_id, "
                    "delivery_snapshot_hash, requirement_id, requirement_version, "
                    "required_work_item_set_version, required_work_item_set_hash, "
                    "work_item_ids, state, attempts, available_at, received_at, updated_at) "
                    "VALUES (:message_id, 'requirement.integration-baseline.requested', "
                    ":payload_hash, :delivery_snapshot_id, :delivery_snapshot_hash, "
                    ":requirement_id, :requirement_version, "
                    ":required_work_item_set_version, :required_work_item_set_hash, "
                    "CAST(:work_item_ids AS JSONB), 'RECEIVED', 0, :now, :now, :now) "
                    "ON CONFLICT (message_id) DO NOTHING RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one_or_none()
        )

    def claim_evidence_request(
        self,
        message_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.evidence_request_inbox "
                    "SET state='PROCESSING', attempts=attempts + 1, "
                    "available_at=:lease_until, updated_at=:now "
                    "WHERE message_id=:message_id "
                    "AND state IN ('RECEIVED', 'FAILED', 'PROCESSING') "
                    "AND available_at <= :now RETURNING *"
                ),
                {
                    "message_id": message_id,
                    "now": now,
                    "lease_until": lease_until,
                },
            )
            .mappings()
            .one_or_none()
        )

    def pending_evidence_request_ids(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text(
                    "SELECT message_id "
                    "FROM source_control.evidence_request_inbox "
                    "WHERE state IN ('RECEIVED', 'FAILED', 'PROCESSING') "
                    "AND available_at <= :now "
                    "ORDER BY available_at, message_id LIMIT :limit"
                ),
                {"limit": limit, "now": now},
            ).scalars()
        ]

    def complete_evidence_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.evidence_request_inbox "
                    "SET state='PROCESSED', processed_at=:now, updated_at=:now, "
                    "last_error_code=NULL WHERE message_id=:message_id "
                    "AND state='PROCESSING' AND attempts=:expected_attempts RETURNING *"
                ),
                {
                    "message_id": message_id,
                    "expected_attempts": expected_attempts,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def fail_evidence_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.evidence_request_inbox "
                    "SET state='FAILED', attempts=attempts + 1, available_at=:retry_at, "
                    "last_error_code=:error_code, updated_at=:now, processed_at=NULL "
                    "WHERE message_id=:message_id AND attempts=:expected_attempts "
                    "AND state IN ('RECEIVED', 'FAILED', 'PROCESSING') RETURNING *"
                ),
                {
                    "message_id": message_id,
                    "expected_attempts": expected_attempts,
                    "now": now,
                    "retry_at": retry_at,
                    "error_code": error_code,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_integration_baseline_evidence(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.integration_baseline_evidence "
                    "(id, delivery_snapshot_id, delivery_snapshot_hash, requirement_id, "
                    "requirement_version, required_work_item_set_version, "
                    "required_work_item_set_hash, evidence_hash, generated_by, generated_at) "
                    "VALUES (:id, :delivery_snapshot_id, :delivery_snapshot_hash, "
                    ":requirement_id, :requirement_version, "
                    ":required_work_item_set_version, :required_work_item_set_hash, "
                    ":evidence_hash, :generated_by, :generated_at) "
                    "ON CONFLICT (delivery_snapshot_id, delivery_snapshot_hash) "
                    "DO NOTHING RETURNING *"
                ),
                values,
            )
            .mappings()
            .one_or_none()
        )

    def insert_integration_baseline_evidence_item(self, **values: Any) -> Any:
        parameters = {
            **values,
            "artifact_references": json.dumps(
                values["artifact_references"],
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.integration_baseline_evidence_item "
                    "(evidence_id, requirement_id, work_item_id, repository_id, "
                    "task_branch, task_commit_sha, integration_merge_request_binding_id, "
                    "integration_merge_request_iid, integration_merge_commit_sha, "
                    "executor_type, executor_id, artifact_references, "
                    "external_validation_reference_id, item_hash) VALUES "
                    "(:evidence_id, :requirement_id, :work_item_id, :repository_id, "
                    ":task_branch, :task_commit_sha, "
                    ":integration_merge_request_binding_id, "
                    ":integration_merge_request_iid, :integration_merge_commit_sha, "
                    ":executor_type, :executor_id, CAST(:artifact_references AS JSONB), "
                    ":external_validation_reference_id, :item_hash) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def integration_baseline_evidence_by_id(self, evidence_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.integration_baseline_evidence "
                    "WHERE id=:evidence_id"
                ),
                {"evidence_id": evidence_id},
            )
            .mappings()
            .one_or_none()
        )

    def integration_baseline_evidence_by_snapshot(
        self,
        delivery_snapshot_id: str,
        delivery_snapshot_hash: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.integration_baseline_evidence "
                    "WHERE delivery_snapshot_id=:delivery_snapshot_id "
                    "AND delivery_snapshot_hash=:delivery_snapshot_hash"
                ),
                {
                    "delivery_snapshot_id": delivery_snapshot_id,
                    "delivery_snapshot_hash": delivery_snapshot_hash,
                },
            )
            .mappings()
            .one_or_none()
        )

    def integration_baseline_evidence_items(self, evidence_id: str) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT item.*, validation.reference, validation.notes, "
                    "validation.reference_hash, validation.submitted_by, "
                    "validation.submitted_at "
                    "FROM source_control.integration_baseline_evidence_item AS item "
                    "JOIN source_control.external_validation_reference AS validation "
                    "ON validation.id=item.external_validation_reference_id "
                    "WHERE item.evidence_id=:evidence_id "
                    "ORDER BY item.work_item_id"
                ),
                {"evidence_id": evidence_id},
            ).mappings()
        )
