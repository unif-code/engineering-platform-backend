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
                    "revision=revision + 1, updated_at=:now "
                    "WHERE id=:id AND revision=:expected_revision "
                    "AND repository_state='WAITING_REPOSITORY' RETURNING *"
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
