from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

_EFFECT_UPDATE_COLUMNS = frozenset(
    {
        "attempts",
        "base_commit_sha",
        "branch_name",
        "completed_at",
        "last_error_code",
        "next_reconcile_at",
        "requirement_callback_state",
        "state",
        "updated_at",
    }
)


class SqlAlchemySourceControlRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def insert_workspace_repository(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.workspace_repository "
                    "(id, workspace_id, provider, project_id, project_path, default_branch, "
                    "connection_ref, credential_secret_ref, webhook_signing_secret_ref, "
                    "status, revision, created_at, updated_at) VALUES "
                    "(:id, :workspace_id, :provider, :project_id, :project_path, "
                    ":default_branch, :connection_ref, :credential_secret_ref, "
                    ":webhook_signing_secret_ref, :status, :revision, :now, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def workspace_repository(
        self,
        repository_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM source_control.workspace_repository WHERE id=:id{suffix}"),
                {"id": repository_id},
            )
            .mappings()
            .one_or_none()
        )

    def remove_workspace_repository(
        self,
        repository_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.workspace_repository "
                    "SET status='REMOVED', revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision AND status='AUTHORIZED' "
                    "RETURNING *"
                ),
                {
                    "id": repository_id,
                    "expected_revision": expected_revision,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def accept_binding_request(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.binding_request_inbox "
                    "(message_id, payload_hash, requirement_id, requirement_version, "
                    "work_item_id, repository_id, state, attempts, available_at, "
                    "received_at, updated_at) VALUES "
                    "(:message_id, :payload_hash, :requirement_id, :requirement_version, "
                    ":work_item_id, :repository_id, 'RECEIVED', 0, :now, :now, :now) "
                    "ON CONFLICT (message_id) DO NOTHING RETURNING *"
                ),
                values,
            )
            .mappings()
            .one_or_none()
        )

    def binding_request(self, message_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.binding_request_inbox "
                    f"WHERE message_id=:message_id{suffix}"
                ),
                {"message_id": message_id},
            )
            .mappings()
            .one_or_none()
        )

    def claim_binding_requests(
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
                    "SELECT message_id FROM source_control.binding_request_inbox "
                    "WHERE state IN ('RECEIVED', 'FAILED') AND available_at <= :now "
                    "ORDER BY available_at, message_id FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE source_control.binding_request_inbox AS inbox "
                    "SET state='PROCESSING', attempts=inbox.attempts + 1, "
                    "available_at=:lease_until, updated_at=:now, last_error_code=NULL "
                    "FROM candidates WHERE inbox.message_id=candidates.message_id "
                    "RETURNING inbox.*"
                ),
                {"limit": limit, "now": now, "lease_until": lease_until},
            ).mappings()
        )

    def pending_binding_request_ids(self, *, limit: int, now: datetime) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text(
                    "SELECT message_id FROM source_control.binding_request_inbox "
                    "WHERE (state IN ('RECEIVED', 'FAILED') OR "
                    "(state='PROCESSING' AND available_at <= :now)) "
                    "AND available_at <= :now "
                    "ORDER BY available_at, message_id LIMIT :limit"
                ),
                {"limit": limit, "now": now},
            ).scalars()
        ]

    def complete_binding_request(self, message_id: str, *, now: datetime) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.binding_request_inbox "
                    "SET state='PROCESSED', processed_at=:now, updated_at=:now, "
                    "last_error_code=NULL WHERE message_id=:message_id "
                    "AND state IN ('RECEIVED', 'PROCESSING', 'FAILED') RETURNING *"
                ),
                {"message_id": message_id, "now": now},
            )
            .mappings()
            .one_or_none()
        )

    def claim_binding_request(
        self,
        message_id: str,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.binding_request_inbox "
                    "SET state='PROCESSING', attempts=attempts + 1, "
                    "available_at=:lease_until, updated_at=:now, last_error_code=NULL "
                    "WHERE message_id=:message_id AND ("
                    "state IN ('RECEIVED', 'FAILED') "
                    "OR (state='PROCESSING' AND available_at <= :now)) RETURNING *"
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

    def next_work_item_number(self) -> int:
        return int(
            self.db.execute(
                text("SELECT nextval('source_control.work_item_number_seq')")
            ).scalar_one()
        )

    def insert_effect(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.source_control_effect "
                    "(id, effect_key, operation, work_item_id, requirement_id, repository_id, "
                    "work_item_number, branch_name, base_commit_sha, request_fingerprint, "
                    "attempts, next_reconcile_at, state, requirement_callback_state, "
                    "created_at, updated_at) VALUES "
                    "(:id, :effect_key, :operation, :work_item_id, :requirement_id, "
                    ":repository_id, :work_item_number, :branch_name, :base_commit_sha, "
                    ":request_fingerprint, :attempts, :next_reconcile_at, :state, "
                    ":requirement_callback_state, :now, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def effect_by_work_item(
        self,
        work_item_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.source_control_effect "
                    f"WHERE work_item_id=:work_item_id{suffix}"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def effect_by_id(self, effect_id: str) -> Any:
        return (
            self.db.execute(
                text("SELECT * FROM source_control.source_control_effect WHERE id=:id"),
                {"id": effect_id},
            )
            .mappings()
            .one_or_none()
        )

    def transition_effect(
        self,
        effect_id: str,
        *,
        expected_state: str,
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
                    "WHERE id=:effect_id AND state=:expected_state RETURNING *"
                ),
                {"effect_id": effect_id, "expected_state": expected_state, **values},
            )
            .mappings()
            .one_or_none()
        )

    def claim_unknown_effects(
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
                    "WHERE state IN ('UNKNOWN', 'IN_FLIGHT', 'RECONCILIATION') "
                    "AND next_reconcile_at <= :now "
                    "ORDER BY next_reconcile_at, id FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE source_control.source_control_effect AS effect "
                    "SET state='RECONCILIATION', attempts=effect.attempts + 1, "
                    "next_reconcile_at=:lease_until, updated_at=:now "
                    "FROM candidates WHERE effect.id=candidates.id RETURNING effect.*"
                ),
                {"limit": limit, "now": now, "lease_until": lease_until},
            ).mappings()
        )

    def pending_callback_effects(self, *, limit: int) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM source_control.source_control_effect "
                    "WHERE state IN ('SUCCEEDED', 'BLOCKED') "
                    "AND requirement_callback_state <> 'ACKED' "
                    "ORDER BY updated_at, id LIMIT :limit"
                ),
                {"limit": limit},
            ).mappings()
        )

    def insert_binding(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.repository_branch_binding "
                    "(id, work_item_id, requirement_id, workspace_id, repository_id, "
                    "work_item_number, base_commit_sha, branch_name, effect_id, created_at) "
                    "VALUES (:id, :work_item_id, :requirement_id, :workspace_id, "
                    ":repository_id, :work_item_number, :base_commit_sha, :branch_name, "
                    ":effect_id, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def binding_by_work_item(self, work_item_id: str) -> Any:
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

    def accept_webhook(self, **values: Any) -> Any:
        parameters = {
            "state": "RECEIVED",
            "processed_at": None,
            **values,
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.webhook_inbox "
                    "(id, repository_id, webhook_id, webhook_timestamp, payload_digest, "
                    "provider_event_uuid, event_type, object_kind, project_id, ref, "
                    "before_sha, after_sha, checkout_sha, state, received_at, updated_at, "
                    "processed_at) "
                    "VALUES (:id, :repository_id, :webhook_id, :webhook_timestamp, "
                    ":payload_digest, :provider_event_uuid, :event_type, :object_kind, "
                    ":project_id, :ref, :before_sha, :after_sha, :checkout_sha, "
                    ":state, :now, :now, :processed_at) "
                    "ON CONFLICT (repository_id, webhook_id) DO NOTHING RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one_or_none()
        )

    def webhook_by_message(self, repository_id: str, webhook_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.webhook_inbox "
                    "WHERE repository_id=:repository_id AND webhook_id=:webhook_id"
                ),
                {"repository_id": repository_id, "webhook_id": webhook_id},
            )
            .mappings()
            .one_or_none()
        )

    def webhook_by_id(self, inbox_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM source_control.webhook_inbox WHERE id=:id{suffix}"),
                {"id": inbox_id},
            )
            .mappings()
            .one_or_none()
        )

    def pending_webhook_ids(self, *, limit: int) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text(
                    "SELECT id FROM source_control.webhook_inbox WHERE state='RECEIVED' "
                    "ORDER BY received_at, id LIMIT :limit"
                ),
                {"limit": limit},
            ).scalars()
        ]

    def make_unknown_effect_due(
        self,
        *,
        repository_id: str,
        branch_name: str,
        now: datetime,
    ) -> int:
        result = self.db.execute(
            text(
                "UPDATE source_control.source_control_effect SET next_reconcile_at=:now, "
                "updated_at=:now WHERE repository_id=:repository_id "
                "AND branch_name=:branch_name AND state='UNKNOWN'"
            ),
            {
                "repository_id": repository_id,
                "branch_name": branch_name,
                "now": now,
            },
        )
        return result.rowcount

    def complete_webhook(self, inbox_id: str, *, now: datetime) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.webhook_inbox SET state='PROCESSED', "
                    "processed_at=:now, updated_at=:now, last_error_code=NULL "
                    "WHERE id=:id AND state='RECEIVED' RETURNING *"
                ),
                {"id": inbox_id, "now": now},
            )
            .mappings()
            .one_or_none()
        )
