import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text


class SqlAlchemyRequirementRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

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
        return list(
            self.db.execute(
                text(
                    "SELECT * FROM requirement.requirement "
                    "WHERE workspace_id=:workspace_id AND ("
                    ":after_created_at IS NULL OR (created_at, id) > "
                    "(:after_created_at, CAST(:after_id AS UUID))) "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {
                    "workspace_id": workspace_id,
                    "after_created_at": after_created_at,
                    "after_id": after_id,
                    "limit": limit,
                },
            ).mappings()
        )

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
