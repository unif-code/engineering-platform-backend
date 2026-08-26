import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text


class SqlAlchemyRequirementRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def claim_idempotency(self, **values: Any) -> bool:
        result = self.db.execute(
            text(
                "INSERT INTO requirement.idempotency_record "
                "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                "created_at, updated_at) VALUES "
                "(:id, :actor, :operation, :idempotency_key, :request_fingerprint, "
                "'IN_PROGRESS', :now, :now) "
                "ON CONFLICT (actor, operation, idempotency_key) DO NOTHING RETURNING id"
            ),
            values,
        )
        return result.scalar_one_or_none() is not None

    def idempotency_by_scope(
        self,
        actor: str,
        operation: str,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.idempotency_record "
                    "WHERE actor=:actor AND operation=:operation "
                    f"AND idempotency_key=:idempotency_key{suffix}"
                ),
                {
                    "actor": actor,
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                },
            )
            .mappings()
            .one_or_none()
        )

    def complete_idempotency(
        self,
        record_id: str,
        *,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool:
        result = self.db.execute(
            text(
                "UPDATE requirement.idempotency_record SET state='COMPLETED', "
                "http_status=:http_status, result_metadata=CAST(:result_metadata AS JSONB), "
                "sealed_response=:sealed_response, completed_at=:now, updated_at=:now "
                "WHERE id=:id AND state='IN_PROGRESS'"
            ),
            {
                "id": record_id,
                "http_status": http_status,
                "result_metadata": json.dumps(result_metadata, separators=(",", ":")),
                "sealed_response": sealed_response,
                "now": now,
            },
        )
        return result.rowcount == 1

    def insert_requirement(self, **values: Any) -> Any:
        parameters = {
            **values,
            "acceptance_criteria": json.dumps(
                values["acceptance_criteria"],
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.requirement "
                    "(id, workspace_id, type, title, description, acceptance_criteria, "
                    "created_by, initial_repository_id, route_snapshot_version, "
                    "route_snapshot_hash, state, record_state, requirement_version, "
                    "required_work_item_set_version, required_work_item_set_hash, revision, "
                    "created_at, updated_at) VALUES "
                    "(:id, :workspace_id, :type, :title, :description, "
                    "CAST(:acceptance_criteria AS JSONB), :created_by, :initial_repository_id, "
                    ":route_snapshot_version, :route_snapshot_hash, :state, :record_state, "
                    ":requirement_version, :required_work_item_set_version, "
                    ":required_work_item_set_hash, :revision, :now, :now) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def requirement_by_id(
        self,
        requirement_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.requirement WHERE id=:id{suffix}"),
                {"id": requirement_id},
            )
            .mappings()
            .one_or_none()
        )

    def list_requirements(
        self,
        *,
        workspace_id: str,
        after_created_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[Any]:
        if after_created_at is None:
            statement = text(
                "SELECT * FROM requirement.requirement WHERE workspace_id=:workspace_id "
                "ORDER BY created_at, id LIMIT :limit"
            )
            parameters: dict[str, object] = {
                "workspace_id": workspace_id,
                "limit": limit,
            }
        else:
            statement = text(
                "SELECT * FROM requirement.requirement WHERE workspace_id=:workspace_id "
                "AND (created_at, id) > (:after_created_at, CAST(:after_id AS UUID)) "
                "ORDER BY created_at, id LIMIT :limit"
            )
            parameters = {
                "workspace_id": workspace_id,
                "after_created_at": after_created_at,
                "after_id": after_id,
                "limit": limit,
            }
        return list(self.db.execute(statement, parameters).mappings())

    def insert_work_item(self, **values: Any) -> Any:
        parameters = {
            **values,
            "required_capabilities": json.dumps(
                values["required_capabilities"],
                separators=(",", ":"),
            ),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.work_item "
                    "(id, requirement_id, created_by, human_owner_id, executor_type, "
                    "executor_id, required_capabilities, assignment_state, repository_state, "
                    "state, repository_id, revision, created_at, updated_at) VALUES "
                    "(:id, :requirement_id, :created_by, :human_owner_id, :executor_type, "
                    ":executor_id, CAST(:required_capabilities AS JSONB), :assignment_state, "
                    ":repository_state, :state, :repository_id, :revision, :now, :now) "
                    "RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def work_items(self, requirement_id: str) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM requirement.work_item "
                    "WHERE requirement_id=:requirement_id ORDER BY created_at, id"
                ),
                {"requirement_id": requirement_id},
            ).mappings()
        )

    def work_item_by_id(
        self,
        work_item_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.work_item WHERE id=:id{suffix}"),
                {"id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def bind_work_item(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        base_commit_sha: str,
        task_branch: str,
        state: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET repository_state='BOUND', "
                    "base_commit_sha=:base_commit_sha, task_branch=:task_branch, state=:state, "
                    "repository_blocked_reason_code=NULL, repository_blocked_at=NULL, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision "
                    "AND repository_state IN ('WAITING_REPOSITORY', 'BLOCKED') RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "base_commit_sha": base_commit_sha,
                    "task_branch": task_branch,
                    "state": state,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def block_work_item(
        self,
        work_item_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET repository_state='BLOCKED', "
                    "base_commit_sha=NULL, task_branch=NULL, state='DRAFT', "
                    "repository_blocked_reason_code=:reason_code, "
                    "repository_blocked_at=:now, revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision "
                    "AND repository_state IN ('WAITING_REPOSITORY', 'BLOCKED') RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "reason_code": reason_code,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

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
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.work_item SET state=:state, "
                    "integration_delivery_state=:delivery_state, "
                    "integration_merge_request_binding_id=CAST(:binding_id AS UUID), "
                    "integration_blocked_reason_code=:blocked_reason, "
                    "integration_updated_at=:now, revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": work_item_id,
                    "expected_revision": expected_revision,
                    "state": state,
                    "delivery_state": delivery_state,
                    "binding_id": binding_id,
                    "blocked_reason": blocked_reason,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def required_work_item_states(self, requirement_id: str) -> tuple[str, ...]:
        return tuple(
            self.db.execute(
                text(
                    "SELECT state FROM requirement.work_item "
                    "WHERE requirement_id=:requirement_id ORDER BY created_at, id"
                ),
                {"requirement_id": requirement_id},
            ).scalars()
        )

    def insert_outbox(self, **values: Any) -> Any:
        parameters = {
            **values,
            "payload": json.dumps(values["payload"], separators=(",", ":")),
        }
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.outbox_message "
                    "(id, topic, aggregate_type, aggregate_id, aggregate_version, payload, "
                    "state, attempts, available_at, created_at) VALUES "
                    "(:id, :topic, :aggregate_type, :aggregate_id, :aggregate_version, "
                    "CAST(:payload AS JSONB), 'PENDING', 0, :now, :now) RETURNING *"
                ),
                parameters,
            )
            .mappings()
            .one()
        )

    def claim_binding_requests(
        self,
        *,
        limit: int,
        available_before: datetime,
        lease_until: datetime,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "WITH candidates AS ("
                    "SELECT id FROM requirement.outbox_message "
                    "WHERE topic='requirement.repository-binding.requested' "
                    "AND state IN ('PENDING', 'FAILED') "
                    "AND available_at <= :available_before "
                    "ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT :limit"
                    ") UPDATE requirement.outbox_message AS message "
                    "SET attempts=message.attempts + 1, available_at=:lease_until "
                    "FROM candidates WHERE message.id=candidates.id RETURNING message.*"
                ),
                {
                    "available_before": available_before,
                    "lease_until": lease_until,
                    "limit": limit,
                },
            ).mappings()
        )

    def outbox_by_id(self, message_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.outbox_message WHERE id=:id{suffix}"),
                {"id": message_id},
            )
            .mappings()
            .one_or_none()
        )

    def repository_binding_context(self, work_item_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT requirement.id AS requirement_id, "
                    "requirement.type AS requirement_type, "
                    "requirement.title AS requirement_title, "
                    "requirement.workspace_id, work_item.id AS work_item_id, "
                    "work_item.revision AS work_item_revision, work_item.repository_id, "
                    "work_item.assignment_state, work_item.human_owner_id, "
                    "work_item.required_capabilities "
                    "FROM requirement.work_item AS work_item "
                    "JOIN requirement.requirement AS requirement "
                    "ON requirement.id=work_item.requirement_id "
                    "WHERE work_item.id=:work_item_id"
                ),
                {"work_item_id": work_item_id},
            )
            .mappings()
            .one_or_none()
        )

    def publish_outbox(self, message_id: str, *, now: datetime) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.outbox_message SET state='PUBLISHED', "
                    "published_at=:now, last_error_code=NULL "
                    "WHERE id=:id AND state IN ('PENDING', 'FAILED') RETURNING *"
                ),
                {"id": message_id, "now": now},
            )
            .mappings()
            .one_or_none()
        )

    def release_outbox(
        self,
        message_id: str,
        *,
        error_code: str,
        available_at: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.outbox_message SET state='FAILED', "
                    "last_error_code=:error_code, available_at=:available_at, "
                    "published_at=NULL WHERE id=:id "
                    "AND state IN ('PENDING', 'FAILED') RETURNING *"
                ),
                {
                    "id": message_id,
                    "error_code": error_code,
                    "available_at": available_at,
                },
            )
            .mappings()
            .one_or_none()
        )

    def outbox_by_aggregate(
        self,
        aggregate_id: str,
        *,
        aggregate_version: int,
    ) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM requirement.outbox_message "
                    "WHERE aggregate_id=:aggregate_id "
                    "AND aggregate_version=:aggregate_version ORDER BY created_at, id"
                ),
                {
                    "aggregate_id": aggregate_id,
                    "aggregate_version": aggregate_version,
                },
            ).mappings()
        )

    def update_requirement_state(
        self,
        requirement_id: str,
        *,
        expected_revision: int,
        state: str,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET state=:state, revision=revision + 1, "
                    "updated_at=:now WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": requirement_id,
                    "expected_revision": expected_revision,
                    "state": state,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_sdd_baseline(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.sdd_baseline "
                    "(id, requirement_id, requirement_version, artifact_id, "
                    "artifact_version, artifact_hash, route_snapshot_version, "
                    "route_snapshot_hash, created_by, created_at) VALUES "
                    "(:id, :requirement_id, :requirement_version, :artifact_id, "
                    ":artifact_version, :artifact_hash, :route_snapshot_version, "
                    ":route_snapshot_hash, :created_by, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def set_current_sdd_baseline(
        self,
        requirement_id: str,
        *,
        baseline_id: str,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.requirement SET current_sdd_baseline_id=:baseline_id, "
                    "revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision RETURNING *"
                ),
                {
                    "id": requirement_id,
                    "baseline_id": baseline_id,
                    "expected_revision": expected_revision,
                    "now": now,
                },
            )
            .mappings()
            .one_or_none()
        )

    def sdd_baseline_by_id(self, baseline_id: str) -> Any:
        return (
            self.db.execute(
                text("SELECT * FROM requirement.sdd_baseline WHERE id=:id"),
                {"id": baseline_id},
            )
            .mappings()
            .one_or_none()
        )

    def sdd_baseline_by_artifact(
        self,
        requirement_id: str,
        artifact_id: str,
        artifact_version: str,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.sdd_baseline "
                    "WHERE requirement_id=:requirement_id AND artifact_id=:artifact_id "
                    "AND artifact_version=:artifact_version ORDER BY created_at, id LIMIT 1"
                ),
                {
                    "requirement_id": requirement_id,
                    "artifact_id": artifact_id,
                    "artifact_version": artifact_version,
                },
            )
            .mappings()
            .one_or_none()
        )

    def insert_gate(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.gate_instance "
                    "(id, gate_type, requirement_id, requirement_version, sdd_baseline_id, "
                    "artifact_id, artifact_version, artifact_hash, route_snapshot_version, "
                    "route_snapshot_hash, policy_version, state, revision, created_at) VALUES "
                    "(:id, :gate_type, :requirement_id, :requirement_version, "
                    ":sdd_baseline_id, :artifact_id, :artifact_version, :artifact_hash, "
                    ":route_snapshot_version, :route_snapshot_hash, :policy_version, "
                    ":state, :revision, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def gate_by_id(self, gate_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM requirement.gate_instance WHERE id=:id{suffix}"),
                {"id": gate_id},
            )
            .mappings()
            .one_or_none()
        )

    def gate_by_baseline_id(self, baseline_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.gate_instance "
                    "WHERE sdd_baseline_id=:baseline_id ORDER BY created_at, id LIMIT 1"
                ),
                {"baseline_id": baseline_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_gate_assignment(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.gate_assignment "
                    "(id, gate_instance_id, default_reviewer_id, current_reviewer_id, "
                    "revision, assigned_at) VALUES "
                    "(:id, :gate_instance_id, :default_reviewer_id, "
                    ":current_reviewer_id, :revision, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def current_gate_assignment(self, gate_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(
                    "SELECT * FROM requirement.gate_assignment "
                    "WHERE gate_instance_id=:gate_id AND superseded_at IS NULL"
                    f"{suffix}"
                ),
                {"gate_id": gate_id},
            )
            .mappings()
            .one_or_none()
        )

    def insert_decision(self, **values: Any) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO requirement.decision "
                    "(id, gate_instance_id, gate_assignment_id, reviewer_id, outcome, "
                    "reason, subject_revision, decided_at) VALUES "
                    "(:id, :gate_instance_id, :gate_assignment_id, :reviewer_id, :outcome, "
                    ":reason, :subject_revision, :now) RETURNING *"
                ),
                values,
            )
            .mappings()
            .one()
        )

    def close_gate(
        self,
        gate_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE requirement.gate_instance SET state='DECIDED', "
                    "revision=revision + 1, decided_at=:now "
                    "WHERE id=:id AND revision=:expected_revision AND state='OPEN' "
                    "RETURNING *"
                ),
                {"id": gate_id, "expected_revision": expected_revision, "now": now},
            )
            .mappings()
            .one_or_none()
        )
