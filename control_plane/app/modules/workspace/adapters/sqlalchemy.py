import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text


class SqlAlchemyWorkspaceRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def claim_idempotency(self, **values: Any) -> bool:
        result = self.db.execute(
            text(
                "INSERT INTO workspace.idempotency_record "
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
                    "SELECT * FROM workspace.idempotency_record "
                    "WHERE actor=:actor AND operation=:operation "
                    f"AND idempotency_key=:idempotency_key{suffix}"
                ),
                {"actor": actor, "operation": operation, "idempotency_key": idempotency_key},
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
                "UPDATE workspace.idempotency_record SET state='COMPLETED', "
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

    def insert_workspace(self, *, workspace_id: str, name: str, owner_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "INSERT INTO workspace.workspace (id, name, owner_id) "
                    "VALUES (:id, :name, :owner_id) RETURNING *"
                ),
                {"id": workspace_id, "name": name, "owner_id": owner_id},
            )
            .mappings()
            .one()
        )

    def workspace_by_id(self, workspace_id: str, *, for_update: bool = False) -> Any:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            self.db.execute(
                text(f"SELECT * FROM workspace.workspace WHERE id=:id{suffix}"),
                {"id": workspace_id},
            )
            .mappings()
            .one_or_none()
        )

    def list_workspaces(self) -> list[Any]:
        return list(
            self.db.execute(text("SELECT * FROM workspace.workspace ORDER BY name, id")).mappings()
        )

    def leader_ids(self, workspace_id: str) -> list[str]:
        return list(
            self.db.execute(
                text(
                    "SELECT account_id FROM workspace.leader "
                    "WHERE workspace_id=:workspace_id ORDER BY account_id"
                ),
                {"workspace_id": workspace_id},
            ).scalars()
        )

    def insert_leader(self, *, workspace_id: str, account_id: str, invited_by: str) -> bool:
        result = self.db.execute(
            text(
                "INSERT INTO workspace.leader (workspace_id, account_id, invited_by) "
                "VALUES (:workspace_id, :account_id, :invited_by) "
                "ON CONFLICT (workspace_id, account_id) DO NOTHING RETURNING account_id"
            ),
            {
                "workspace_id": workspace_id,
                "account_id": account_id,
                "invited_by": invited_by,
            },
        )
        return result.scalar_one_or_none() is not None

    def delete_leader(self, *, workspace_id: str, account_id: str) -> bool:
        result = self.db.execute(
            text(
                "DELETE FROM workspace.leader "
                "WHERE workspace_id=:workspace_id AND account_id=:account_id"
            ),
            {"workspace_id": workspace_id, "account_id": account_id},
        )
        return result.rowcount == 1

    def update_owner(self, *, workspace_id: str, owner_id: str) -> None:
        self.db.execute(
            text("UPDATE workspace.workspace SET owner_id=:owner_id WHERE id=:workspace_id"),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        )

    def bump_version(self, workspace_id: str) -> Any:
        return (
            self.db.execute(
                text(
                    "UPDATE workspace.workspace SET version=version+1 "
                    "WHERE id=:workspace_id RETURNING *"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .one()
        )

    def projection_rows(self, workspace_id: str) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT account_id, source, computed_at "
                    "FROM workspace.members_projection "
                    "WHERE workspace_id=:workspace_id ORDER BY account_id"
                ),
                {"workspace_id": workspace_id},
            ).mappings()
        )

    def replace_members(
        self,
        workspace_id: str,
        members: dict[str, str],
        *,
        computed_at: datetime,
    ) -> None:
        self.db.execute(
            text("DELETE FROM workspace.members_projection WHERE workspace_id=:workspace_id"),
            {"workspace_id": workspace_id},
        )
        for account_id, source in sorted(members.items()):
            self.db.execute(
                text(
                    "INSERT INTO workspace.members_projection "
                    "(workspace_id, account_id, source, computed_at) "
                    "VALUES (:workspace_id, :account_id, :source, :computed_at)"
                ),
                {
                    "workspace_id": workspace_id,
                    "account_id": account_id,
                    "source": source,
                    "computed_at": computed_at,
                },
            )

    def active_workspace_ids(self) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text("SELECT id FROM workspace.workspace WHERE archived_at IS NULL ORDER BY id")
            ).scalars()
        ]
