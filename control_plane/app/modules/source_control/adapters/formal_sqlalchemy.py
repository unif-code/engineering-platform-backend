import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, text

_EFFECT_UPDATE_COLUMNS = frozenset(
    {
        "attempts",
        "completed_at",
        "last_error_code",
        "next_reconcile_at",
        "requirement_callback_state",
        "state",
        "updated_at",
    }
)


class SqlAlchemySourceControlFormalRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def formal_request(self, message_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.formal_delivery_request_inbox "
                    f"WHERE message_id=:message_id{suffix}"
                ),
                {"message_id": message_id},
            )
            .mappings()
            .one_or_none()
        )

    def formal_request_for_effect(
        self,
        *,
        operation: str,
        work_item_id: str,
        requirement_id: str,
        repository_id: str,
        request_fingerprint: str,
    ) -> Any:
        topic = (
            "requirement.formal-merge-request.requested"
            if operation == "CREATE_FORMAL_MR"
            else "requirement.formal-merge.requested"
        )
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.formal_delivery_request_inbox "
                    "WHERE topic=:topic AND work_item_id=:work_item_id "
                    "AND requirement_id=:requirement_id "
                    "AND repository_id=:repository_id "
                    "AND payload_hash=:request_fingerprint "
                    "ORDER BY received_at, message_id LIMIT 1"
                ),
                {
                    "topic": topic,
                    "work_item_id": work_item_id,
                    "requirement_id": requirement_id,
                    "repository_id": repository_id,
                    "request_fingerprint": request_fingerprint,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_formal_request(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.formal_delivery_request_inbox "
                    "(message_id, topic, payload_hash, requirement_id, "
                    "requirement_revision, work_item_id, work_item_revision, repository_id, "
                    "actor_id, acceptance_decision_id, "
                    "formal_merge_request_binding_id, formal_review_decision_id, "
                    "requested_head_sha, state, attempts, available_at, received_at, "
                    "updated_at) VALUES (:message_id, :topic, :payload_hash, "
                    ":requirement_id, :requirement_revision, :work_item_id, "
                    ":work_item_revision, :repository_id, :actor_id, "
                    ":acceptance_decision_id, :formal_merge_request_binding_id, "
                    ":formal_review_decision_id, :requested_head_sha, 'RECEIVED', 0, "
                    ":now, :now, :now) ON CONFLICT (message_id) DO NOTHING RETURNING *"
                ),
                values,
            )
            .mappings()
            .one_or_none()
        )

    def claim_formal_request(
        self,
        message_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.formal_delivery_request_inbox "
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

    def pending_formal_request_candidates(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT message_id, topic "
                    "FROM source_control.formal_delivery_request_inbox "
                    "WHERE state IN ('RECEIVED', 'FAILED', 'PROCESSING') "
                    "AND available_at <= :now "
                    "ORDER BY available_at, message_id LIMIT :limit"
                ),
                {"limit": limit, "now": now},
            ).mappings()
        )

    def complete_formal_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.formal_delivery_request_inbox "
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

    def complete_formal_request_blocked(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        reason_code: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.formal_delivery_request_inbox "
                    "SET state='PROCESSED', processed_at=:now, updated_at=:now, "
                    "last_error_code=:reason_code WHERE message_id=:message_id "
                    "AND state='PROCESSING' AND attempts=:expected_attempts RETURNING *"
                ),
                {
                    "message_id": message_id,
                    "expected_attempts": expected_attempts,
                    "reason_code": reason_code,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def fail_formal_request(
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
                    "UPDATE source_control.formal_delivery_request_inbox "
                    "SET state='FAILED', available_at=:retry_at, "
                    "last_error_code=:error_code, updated_at=:now, processed_at=NULL "
                    "WHERE message_id=:message_id AND state='PROCESSING' "
                    "AND attempts=:expected_attempts RETURNING *"
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

    def repository_by_id(self, repository_id: str) -> Any:
        return (
            self.db.execute(
                text("SELECT * FROM source_control.workspace_repository WHERE id=:id"),
                {"id": repository_id},
            )
            .mappings()
            .one_or_none()
        )

    def branch_binding_by_work_item(self, work_item_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.repository_branch_binding "
                    "WHERE work_item_id=:work_item_id"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def formal_binding_by_work_item(self, work_item_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.merge_request_binding "
                    "WHERE work_item_id=:work_item_id AND kind='FORMAL' "
                    "AND superseded_at IS NULL"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def merge_request_binding_by_id(self, binding_id: str) -> Any:
        return (
            self.db.execute(
                text("SELECT * FROM source_control.merge_request_binding WHERE id=:id"),
                {"id": binding_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_merge_request_binding(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.merge_request_binding "
                    "(id, kind, work_item_id, requirement_id, workspace_id, repository_id, "
                    "branch_binding_id, external_project_id, merge_request_iid, "
                    "source_branch, target_branch, create_effect_id, head_sha, "
                    "creation_origin, created_at) VALUES "
                    "(:id, :kind, :work_item_id, :requirement_id, :workspace_id, "
                    ":repository_id, :branch_binding_id, :external_project_id, "
                    ":merge_request_iid, :source_branch, :target_branch, :create_effect_id, "
                    ":head_sha, :creation_origin, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def append_merge_request_observation(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.merge_request_observation "
                    "(id, binding_id, head_sha, state, merge_commit_sha, "
                    "external_merge_user_id, merged_at, observation_digest, observed_at) "
                    "VALUES (:id, :binding_id, :head_sha, :state, :merge_commit_sha, "
                    ":external_merge_user_id, :merged_at, :observation_digest, "
                    ":observed_at) ON CONFLICT (binding_id, observation_digest) "
                    "DO NOTHING RETURNING *"
                ),
                values,
            )
            .mappings()
            .one_or_none()
        )

    def latest_merge_request_observation(self, binding_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.merge_request_observation "
                    "WHERE binding_id=:binding_id ORDER BY observed_at DESC, id DESC LIMIT 1"
                ),
                {"binding_id": binding_id},
            )
            .mappings()
            .one_or_none()
        )

    def effect_by_operation_subject(
        self,
        operation: str,
        subject_key: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.source_control_effect "
                    "WHERE operation=:operation AND subject_key=:subject_key"
                    f"{suffix}"
                ),
                {"operation": operation, "subject_key": subject_key},
            )
            .mappings()
            .one_or_none()
        )

    def effect_by_id(self, effect_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.source_control_effect "
                    "WHERE id=:effect_id AND operation IN "
                    "('CREATE_FORMAL_MR', 'MERGE_FORMAL_MR')" + suffix
                ),
                {"effect_id": effect_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_effect(self, **values: Any) -> Any:
        payload = values["payload"]
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json", by_alias=True)
        parameters = {
            "work_item_number": None,
            "branch_name": None,
            "base_commit_sha": None,
            "completed_at": None,
            **values,
            "payload": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.source_control_effect "
                    "(id, effect_key, operation, subject_key, payload, work_item_id, "
                    "requirement_id, repository_id, work_item_number, branch_name, "
                    "base_commit_sha, request_fingerprint, attempts, next_reconcile_at, "
                    "state, requirement_callback_state, created_at, updated_at, "
                    "completed_at) VALUES (:id, :effect_key, :operation, :subject_key, "
                    "CAST(:payload AS JSONB), :work_item_id, :requirement_id, "
                    ":repository_id, :work_item_number, :branch_name, :base_commit_sha, "
                    ":request_fingerprint, :attempts, :next_reconcile_at, :state, "
                    ":requirement_callback_state, :now, :now, :completed_at) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def transition_effect(
        self,
        effect_id: str,
        *,
        expected_state: str,
        expected_attempts: int,
        values: Mapping[str, object],
    ) -> Any:
        unexpected = set(values) - _EFFECT_UPDATE_COLUMNS
        if not values or unexpected:
            raise ValueError(f"Invalid effect update columns: {sorted(unexpected)}")
        assignments = ", ".join(f"{column}=:{column}" for column in sorted(values))
        return (
            self.db.execute(
                text(
                    f"UPDATE source_control.source_control_effect SET {assignments} "
                    "WHERE id=:effect_id AND operation IN "
                    "('CREATE_FORMAL_MR', 'MERGE_FORMAL_MR') "
                    "AND state=:expected_state AND attempts=:expected_attempts RETURNING *"
                ),
                {
                    "effect_id": effect_id,
                    "expected_state": expected_state,
                    "expected_attempts": expected_attempts,
                    **values,
                },
            )
            .mappings()
            .one_or_none()
        )

    def claim_effect(
        self,
        effect_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.source_control_effect "
                    "SET state='RECONCILIATION', attempts=attempts + 1, "
                    "next_reconcile_at=:lease_until, updated_at=:now "
                    "WHERE id=:effect_id AND operation IN "
                    "('CREATE_FORMAL_MR', 'MERGE_FORMAL_MR') "
                    "AND state IN ('UNKNOWN', 'IN_FLIGHT', 'RECONCILIATION') "
                    "AND next_reconcile_at <= :now RETURNING *"
                ),
                {
                    "effect_id": effect_id,
                    "now": now,
                    "lease_until": lease_until,
                },
            )
            .mappings()
            .one_or_none()
        )

    def claim_effects(
        self,
        *,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "WITH candidates AS ("
                    "SELECT id FROM source_control.source_control_effect "
                    "WHERE operation IN ('CREATE_FORMAL_MR', 'MERGE_FORMAL_MR') "
                    "AND state IN ('UNKNOWN', 'IN_FLIGHT', 'RECONCILIATION') "
                    "AND next_reconcile_at <= :now "
                    "ORDER BY next_reconcile_at, id "
                    "FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE source_control.source_control_effect AS effect "
                    "SET state='RECONCILIATION', attempts=effect.attempts + 1, "
                    "next_reconcile_at=:lease_until, updated_at=:now FROM candidates "
                    "WHERE effect.id=candidates.id RETURNING effect.*"
                ),
                {"limit": limit, "now": now, "lease_until": lease_until},
            ).mappings()
        )

    def pending_callback_effects(self, *, limit: int) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM source_control.source_control_effect "
                    "WHERE operation IN ('CREATE_FORMAL_MR', 'MERGE_FORMAL_MR') "
                    "AND state IN ('SUCCEEDED', 'BLOCKED', 'UNKNOWN') "
                    "AND requirement_callback_state <> 'ACKED' "
                    "ORDER BY updated_at, id LIMIT :limit"
                ),
                {"limit": limit},
            ).mappings()
        )

    def insert_formal_review_assignment(self, **values: Any) -> Any:
        parameters = {
            **values,
            "resolution_snapshot": json.dumps(
                values["resolution_snapshot"],
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.formal_review_assignment "
                    "(id, binding_id, acceptance_decision_id, requirement_id, work_item_id, "
                    "subject_head_sha, default_reviewer_id, current_reviewer_id, policy_code, "
                    "policy_version, policy_snapshot_hash, resolution_snapshot, revision, "
                    "assigned_at) VALUES (:id, :binding_id, :acceptance_decision_id, "
                    ":requirement_id, :work_item_id, :subject_head_sha, :default_reviewer_id, "
                    ":current_reviewer_id, :policy_code, :policy_version, "
                    ":policy_snapshot_hash, CAST(:resolution_snapshot AS JSONB), "
                    ":revision, :now) "
                    "RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def current_formal_review_assignment(
        self,
        binding_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.formal_review_assignment "
                    "WHERE binding_id=:binding_id AND superseded_at IS NULL" + suffix
                ),
                {"binding_id": binding_id},
            )
            .mappings()
            .one_or_none()
        )

    def formal_review_assignment_by_acceptance(
        self,
        binding_id: str,
        acceptance_decision_id: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.formal_review_assignment "
                    "WHERE binding_id=:binding_id "
                    "AND acceptance_decision_id=:acceptance_decision_id"
                ),
                {
                    "binding_id": binding_id,
                    "acceptance_decision_id": acceptance_decision_id,
                },
            )
            .mappings()
            .one_or_none()
        )

    def supersede_formal_review_assignment(
        self,
        assignment_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.formal_review_assignment "
                    "SET superseded_at=:now WHERE id=:assignment_id "
                    "AND revision=:expected_revision AND superseded_at IS NULL RETURNING *"
                ),
                {
                    "assignment_id": assignment_id,
                    "expected_revision": expected_revision,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )
