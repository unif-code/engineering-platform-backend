import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text


class SqlAlchemyOrganizationRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def lock_structure(self) -> None:
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('organization.structure', 0))")
        )

    def claim_idempotency(self, **values: Any) -> bool:
        result = self.db.execute(
            text(
                "INSERT INTO organization.idempotency_record "
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
                    "SELECT * FROM organization.idempotency_record "
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
                "UPDATE organization.idempotency_record SET state='COMPLETED', "
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

    def all_edges(self) -> list[Any]:
        return list(
            self.db.execute(
                text(
                    "SELECT account_id, superior_id, kind "
                    "FROM organization.org_edge ORDER BY account_id"
                )
            ).mappings()
        )

    def upsert_edge(
        self,
        *,
        account_id: str,
        superior_id: str | None,
        kind: str,
        now: datetime,
    ) -> None:
        self.db.execute(
            text(
                "INSERT INTO organization.org_edge "
                "(account_id, superior_id, kind, created_at, updated_at) "
                "VALUES (:account_id, :superior_id, :kind, :now, :now) "
                "ON CONFLICT (account_id) DO UPDATE SET "
                "superior_id = EXCLUDED.superior_id, kind = EXCLUDED.kind, "
                "updated_at = EXCLUDED.updated_at"
            ),
            {
                "account_id": account_id,
                "superior_id": superior_id,
                "kind": kind,
                "now": now,
            },
        )
