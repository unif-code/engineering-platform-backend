import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

_AGENT_PUSH_UPDATE_COLUMNS = frozenset(
    {
        "attempts",
        "completed_at",
        "consumed_at",
        "last_error_code",
        "next_reconcile_at",
        "observed_at",
        "remote_head_sha",
        "state",
        "updated_at",
    }
)
_REVOCATION_UPDATE_COLUMNS = frozenset(
    {
        "next_revoke_at",
        "revocation_state",
        "revoke_attempts",
        "updated_at",
    }
)


class SqlAlchemyAgentDeliveryRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def workspace_repository(self, repository_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.workspace_repository "
                    f"WHERE id=:repository_id{suffix}"
                ),
                {"repository_id": repository_id},
            )
            .mappings()
            .one_or_none()
        )

    def branch_binding(self, binding_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.repository_branch_binding "
                    f"WHERE id=:binding_id{suffix}"
                ),
                {"binding_id": binding_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_agent_push(self, **values: Any) -> Any:
        parameters = {
            **values,
            "artifact_refs": json.dumps(values["artifact_refs"], separators=(",", ":")),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.agent_push_request "
                    "(id, idempotency_key, request_fingerprint, attempt_id, "
                    "attempt_generation, execution_binding_digest, requirement_id, "
                    "work_item_id, workspace_id, repository_id, branch_binding_id, "
                    "branch_name, expected_remote_head_sha, target_commit_sha, "
                    "content_digest, artifact_refs, grant_digest, correlation_id, state, "
                    "attempts, issued_at, expires_at, next_reconcile_at, consumed_at, "
                    "observed_at, completed_at, remote_head_sha, last_error_code, "
                    "created_at, updated_at) VALUES "
                    "(:id, :idempotency_key, :request_fingerprint, :attempt_id, "
                    ":attempt_generation, :execution_binding_digest, :requirement_id, "
                    ":work_item_id, :workspace_id, :repository_id, :branch_binding_id, "
                    ":branch_name, :expected_remote_head_sha, :target_commit_sha, "
                    ":content_digest, CAST(:artifact_refs AS JSONB), :grant_digest, "
                    ":correlation_id, :state, :attempts, :issued_at, :expires_at, "
                    ":next_reconcile_at, :consumed_at, :observed_at, :completed_at, "
                    ":remote_head_sha, :last_error_code, :created_at, :updated_at) "
                    "RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def agent_push_by_id(self, request_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    f"SELECT * FROM source_control.agent_push_request WHERE id=:request_id{suffix}"
                ),
                {"request_id": request_id},
            )
            .mappings()
            .one_or_none()
        )

    def agent_push_by_idempotency(
        self,
        workspace_id: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.agent_push_request "
                    "WHERE workspace_id=:workspace_id AND idempotency_key=:idempotency_key"
                    f"{suffix}"
                ),
                {
                    "workspace_id": workspace_id,
                    "idempotency_key": idempotency_key,
                },
            )
            .mappings()
            .one_or_none()
        )

    def consume_agent_push(
        self,
        *,
        request_id: str,
        grant_digest: str,
        now: datetime,
        next_reconcile_at: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.agent_push_request AS request "
                    "SET state='IN_FLIGHT', attempts=request.attempts + 1, "
                    "consumed_at=:now, next_reconcile_at=:next_reconcile_at, "
                    "updated_at=:now "
                    "WHERE request.id=:request_id AND request.state='AUTHORIZED' "
                    "AND request.grant_digest=:grant_digest AND request.expires_at > :now "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM source_control.agent_delivery_fence AS fence "
                    "WHERE fence.attempt_id=request.attempt_id "
                    "AND fence.fenced_generation >= request.attempt_generation"
                    ") RETURNING request.*"
                ),
                {
                    "request_id": request_id,
                    "grant_digest": grant_digest,
                    "now": now,
                    "next_reconcile_at": next_reconcile_at,
                },
            )
            .mappings()
            .one_or_none()
        )

    def transition_agent_push(
        self,
        request_id: str,
        *,
        expected_state: str,
        expected_attempts: int | None = None,
        values: Mapping[str, object],
    ) -> Any:
        unexpected = set(values) - _AGENT_PUSH_UPDATE_COLUMNS
        if not values or unexpected:
            raise ValueError(f"Invalid Agent push update columns: {sorted(unexpected)}")
        assignments = ", ".join(f"{column}=:{column}" for column in sorted(values))
        attempt_guard = "" if expected_attempts is None else " AND attempts=:expected_attempts"
        return (
            self.db.execute(
                text(
                    f"UPDATE source_control.agent_push_request SET {assignments} "
                    "WHERE id=:request_id AND state=:expected_state"
                    f"{attempt_guard} RETURNING *"
                ),
                {
                    "request_id": request_id,
                    "expected_state": expected_state,
                    "expected_attempts": expected_attempts,
                    **values,
                },
            )
            .mappings()
            .one_or_none()
        )

    def claim_reconcilable(
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
                    "SELECT id FROM source_control.agent_push_request "
                    "WHERE state IN ('IN_FLIGHT', 'UNKNOWN', 'RECONCILIATION') "
                    "AND next_reconcile_at <= :now "
                    "ORDER BY next_reconcile_at, id FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE source_control.agent_push_request AS request "
                    "SET state='RECONCILIATION', attempts=request.attempts + 1, "
                    "last_error_code=COALESCE(request.last_error_code, 'RESULT_UNKNOWN'), "
                    "next_reconcile_at=:lease_until, updated_at=:now "
                    "FROM candidates WHERE request.id=candidates.id RETURNING request.*"
                ),
                {"limit": limit, "now": now, "lease_until": lease_until},
            ).mappings()
        )

    def attempt_fence(self, attempt_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.agent_delivery_fence "
                    f"WHERE attempt_id=:attempt_id{suffix}"
                ),
                {"attempt_id": attempt_id},
            )
            .mappings()
            .one_or_none()
        )

    def upsert_attempt_fence(self, **values: Any) -> Any:
        row = (
            self.db.execute(
                text(
                    "INSERT INTO source_control.agent_delivery_fence "
                    "(attempt_id, fenced_generation, reason_code, correlation_id, "
                    "revocation_state, revoke_attempts, next_revoke_at, created_at, "
                    "updated_at) VALUES (:attempt_id, :fenced_generation, :reason_code, "
                    ":correlation_id, 'PENDING', 0, :next_revoke_at, :now, :now) "
                    "ON CONFLICT (attempt_id) DO UPDATE SET "
                    "fenced_generation=EXCLUDED.fenced_generation, "
                    "reason_code=EXCLUDED.reason_code, "
                    "correlation_id=EXCLUDED.correlation_id, "
                    "revocation_state='PENDING', revoke_attempts=0, "
                    "next_revoke_at=EXCLUDED.next_revoke_at, updated_at=EXCLUDED.updated_at "
                    "WHERE source_control.agent_delivery_fence.fenced_generation "
                    "< EXCLUDED.fenced_generation RETURNING *"
                ),
                values,
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return row
        return self.attempt_fence(str(values["attempt_id"]), for_update=True)

    def fence_open_agent_pushes(
        self,
        *,
        attempt_id: str,
        fenced_generation: int,
        reason_code: str,
        now: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "UPDATE source_control.agent_push_request SET state='FENCED', "
                    "completed_at=:now, next_reconcile_at=NULL, "
                    "last_error_code=:reason_code, updated_at=:now "
                    "WHERE attempt_id=:attempt_id "
                    "AND attempt_generation <= :fenced_generation "
                    "AND state IN ('AUTHORIZED', 'IN_FLIGHT', 'UNKNOWN', 'RECONCILIATION') "
                    "RETURNING *"
                ),
                {
                    "attempt_id": attempt_id,
                    "fenced_generation": fenced_generation,
                    "reason_code": reason_code,
                    "now": now,
                },
            ).mappings()
        )

    def agent_pushes_for_attempt(
        self,
        attempt_id: str,
        *,
        fenced_generation: int,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM source_control.agent_push_request "
                    "WHERE attempt_id=:attempt_id "
                    "AND attempt_generation <= :fenced_generation "
                    "ORDER BY id"
                ),
                {
                    "attempt_id": attempt_id,
                    "fenced_generation": fenced_generation,
                },
            ).mappings()
        )

    def record_fenced_observation(
        self,
        request_id: str,
        *,
        remote_head_sha: str,
        observed_at: datetime,
        reason_code: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE source_control.agent_push_request "
                    "SET remote_head_sha=:remote_head_sha, observed_at=:observed_at, "
                    "last_error_code=:reason_code, updated_at=:now "
                    "WHERE id=:request_id AND state='FENCED' RETURNING *"
                ),
                {
                    "request_id": request_id,
                    "remote_head_sha": remote_head_sha,
                    "observed_at": observed_at,
                    "reason_code": reason_code,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def claim_due_revocations(
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
                    "SELECT attempt_id FROM source_control.agent_delivery_fence "
                    "WHERE revocation_state IN ('PENDING', 'UNKNOWN') "
                    "AND next_revoke_at <= :now "
                    "ORDER BY next_revoke_at, attempt_id "
                    "FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE source_control.agent_delivery_fence AS fence "
                    "SET revocation_state='UNKNOWN', "
                    "revoke_attempts=fence.revoke_attempts + 1, "
                    "next_revoke_at=:lease_until, updated_at=:now "
                    "FROM candidates WHERE fence.attempt_id=candidates.attempt_id "
                    "RETURNING fence.*"
                ),
                {"limit": limit, "now": now, "lease_until": lease_until},
            ).mappings()
        )

    def transition_revocation(
        self,
        attempt_id: str,
        *,
        expected_generation: int,
        expected_state: str,
        values: Mapping[str, object],
    ) -> Any:
        unexpected = set(values) - _REVOCATION_UPDATE_COLUMNS
        if not values or unexpected:
            raise ValueError(f"Invalid revocation update columns: {sorted(unexpected)}")
        assignments = ", ".join(f"{column}=:{column}" for column in sorted(values))
        return (
            self.db.execute(
                text(
                    f"UPDATE source_control.agent_delivery_fence SET {assignments} "
                    "WHERE attempt_id=:attempt_id "
                    "AND fenced_generation=:expected_generation "
                    "AND revocation_state=:expected_state "
                    "RETURNING *"
                ),
                {
                    "attempt_id": attempt_id,
                    "expected_generation": expected_generation,
                    "expected_state": expected_state,
                    **values,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_fact(self, **values: Any) -> Any:
        parameters = {
            **values,
            "payload": json.dumps(values["payload"], separators=(",", ":")),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO source_control.agent_delivery_fact "
                    "(id, push_request_id, topic, payload, correlation_id, occurred_at) "
                    "VALUES (:id, :push_request_id, :topic, CAST(:payload AS JSONB), "
                    ":correlation_id, :occurred_at) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def fact_by_request(self, request_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM source_control.agent_delivery_fact "
                    "WHERE push_request_id=:request_id"
                ),
                {"request_id": request_id},
            )
            .mappings()
            .one_or_none()
        )

    def delivery_for_workspace(self, request_id: str, workspace_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT id, attempt_id, attempt_generation, requirement_id, work_item_id, "
                    "workspace_id, repository_id, branch_binding_id, branch_name, "
                    "expected_remote_head_sha, target_commit_sha, content_digest, "
                    "artifact_refs, state, issued_at, expires_at, consumed_at, observed_at, "
                    "completed_at, remote_head_sha, last_error_code, correlation_id "
                    "FROM source_control.agent_push_request "
                    "WHERE id=:request_id AND workspace_id=:workspace_id"
                ),
                {"request_id": request_id, "workspace_id": workspace_id},
            )
            .mappings()
            .one_or_none()
        )
