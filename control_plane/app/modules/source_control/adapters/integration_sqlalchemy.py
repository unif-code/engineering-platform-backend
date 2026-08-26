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


class SqlAlchemySourceControlIntegrationRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def accept_delivery_request(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.delivery_request_inbox "
                    "(message_id, topic, payload_hash, requirement_id, "
                    "requirement_revision, work_item_id, work_item_revision, "
                    "repository_id, actor_id, integration_merge_request_binding_id, "
                    "state, attempts, available_at, received_at, updated_at) VALUES "
                    "(:message_id, :topic, :payload_hash, :requirement_id, "
                    ":requirement_revision, :work_item_id, :work_item_revision, "
                    ":repository_id, :actor_id, :integration_merge_request_binding_id, "
                    "'RECEIVED', 0, :now, :now, :now) "
                    "ON CONFLICT (message_id) DO NOTHING RETURNING *"
                ),
                values,
            )
            .mappings()
            .one_or_none()
        )

    def delivery_request(
        self,
        message_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.delivery_request_inbox "
                    f"WHERE message_id=:message_id{suffix}"
                ),
                {"message_id": message_id},
            )
            .mappings()
            .one_or_none()
        )

    def claim_delivery_requests(
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
                    "SELECT message_id FROM source_control.delivery_request_inbox "
                    "WHERE available_at <= :now AND (state IN ('RECEIVED', 'FAILED') "
                    "OR state='PROCESSING') ORDER BY available_at, message_id "
                    "FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE source_control.delivery_request_inbox AS inbox "
                    "SET state='PROCESSING', attempts=inbox.attempts + 1, "
                    "available_at=:lease_until, updated_at=:now, last_error_code=NULL "
                    "FROM candidates WHERE inbox.message_id=candidates.message_id "
                    "RETURNING inbox.*"
                ),
                {"limit": limit, "now": now, "lease_until": lease_until},
            ).mappings()
        )

    def complete_delivery_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.delivery_request_inbox "
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

    def insert_effect(self, **values: Any) -> Any:
        payload = values.get("payload", {})
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json", by_alias=True)
        parameters = {
            "work_item_number": None,
            "branch_name": None,
            "base_commit_sha": None,
            "completed_at": None,
            **values,
            "payload": json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.source_control_effect "
                    "(id, effect_key, operation, subject_key, payload, work_item_id, "
                    "requirement_id, repository_id, work_item_number, branch_name, "
                    "base_commit_sha, request_fingerprint, attempts, next_reconcile_at, "
                    "state, requirement_callback_state, created_at, updated_at, "
                    "completed_at) VALUES "
                    "(:id, :effect_key, :operation, :subject_key, CAST(:payload AS JSONB), "
                    ":work_item_id, :requirement_id, :repository_id, :work_item_number, "
                    ":branch_name, :base_commit_sha, :request_fingerprint, :attempts, "
                    ":next_reconcile_at, :state, :requirement_callback_state, :now, :now, "
                    ":completed_at) RETURNING *"
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
                    "('CREATE_INTEGRATION_MR', 'MERGE_INTEGRATION_MR') "
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
                    "WHERE operation IN ('CREATE_INTEGRATION_MR', 'MERGE_INTEGRATION_MR') "
                    "AND state IN ('UNKNOWN', 'IN_FLIGHT', 'RECONCILIATION') "
                    "AND next_reconcile_at <= :now ORDER BY next_reconcile_at, id "
                    "FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE source_control.source_control_effect AS effect "
                    "SET state='RECONCILIATION', attempts=effect.attempts + 1, "
                    "next_reconcile_at=:lease_until, updated_at=:now FROM candidates "
                    "WHERE effect.id=candidates.id RETURNING effect.*"
                ),
                {"limit": limit, "now": now, "lease_until": lease_until},
            ).mappings()
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

    def merge_request_binding_by_id(self, binding_id: str) -> Any:
        return (
            self.db.execute(
                text("SELECT * FROM source_control.merge_request_binding WHERE id=:id"),
                {"id": binding_id},
            )
            .mappings()
            .one_or_none()
        )

    def merge_request_binding_by_work_item(self, work_item_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.merge_request_binding "
                    "WHERE work_item_id=:work_item_id"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def append_merge_request_observation(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.merge_request_observation "
                    "(id, binding_id, head_sha, state, merge_commit_sha, "
                    "external_merge_user_id, merged_at, observation_digest, observed_at) "
                    "VALUES (:id, :binding_id, :head_sha, :state, :merge_commit_sha, "
                    ":external_merge_user_id, :merged_at, :observation_digest, :observed_at) "
                    "ON CONFLICT (binding_id, observation_digest) DO NOTHING RETURNING *"
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

    def pending_callback_effects(self, *, limit: int) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM source_control.source_control_effect "
                    "WHERE operation IN ('CREATE_INTEGRATION_MR', 'MERGE_INTEGRATION_MR') "
                    "AND state IN ('SUCCEEDED', 'BLOCKED') "
                    "AND requirement_callback_state <> 'ACKED' "
                    "ORDER BY updated_at, id LIMIT :limit"
                ),
                {"limit": limit},
            ).mappings()
        )
