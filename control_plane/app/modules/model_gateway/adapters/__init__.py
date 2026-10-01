import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from control_plane.app.modules.model_gateway.domain import Deployment, DeploymentState


class SqlAlchemyDeploymentRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def claim_idempotency(self, **values: Any) -> bool:
        result = self.db.execute(
            text(
                "INSERT INTO model_gateway.idempotency_record "
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
                    "SELECT * FROM model_gateway.idempotency_record "
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
                "UPDATE model_gateway.idempotency_record SET state='COMPLETED', "
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

    def get(self, deployment_id: str, *, for_update: bool = False) -> Deployment | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            self.db.execute(
                text(f"SELECT * FROM model_gateway.deployment WHERE id=:id{suffix}"),
                {"id": deployment_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _deployment(row)

    def insert(self, deployment: Deployment) -> Deployment | None:
        row = (
            self.db.execute(
                text("""
            INSERT INTO model_gateway.deployment
                (id, deployment_key, display_name, provider_kind, provider_model_id,
                 connection_ref, declared_capabilities, declared_context_window,
                 declared_max_output_tokens, description, state, revision,
                 created_by, created_at, updated_by, updated_at)
            VALUES (:id, :deployment_key, :display_name, :provider_kind, :provider_model_id,
                    :connection_ref, :declared_capabilities, :declared_context_window,
                    :declared_max_output_tokens, :description, :state, :revision,
                    :created_by, :created_at, :updated_by, :updated_at)
            ON CONFLICT (deployment_key) DO NOTHING RETURNING *
        """),
                deployment.model_dump(),
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _deployment(row)

    def save(self, deployment: Deployment, *, expected_revision: int) -> bool:
        result = self.db.execute(
            text("""
            UPDATE model_gateway.deployment SET
                display_name=:display_name, provider_kind=:provider_kind,
                provider_model_id=:provider_model_id, connection_ref=:connection_ref,
                declared_capabilities=:declared_capabilities,
                declared_context_window=:declared_context_window,
                declared_max_output_tokens=:declared_max_output_tokens,
                description=:description, state=:state, revision=:revision,
                updated_by=:updated_by, updated_at=:updated_at,
                archived_by=:archived_by, archived_at=:archived_at, archive_reason=:archive_reason
            WHERE id=:id AND revision=:expected_revision AND state='DRAFT'
        """),
            deployment.model_dump() | {"expected_revision": expected_revision},
        )
        return result.rowcount == 1

    def list(
        self, *, state: DeploymentState | None, query: str, after_key: str | None, limit: int
    ) -> list[Deployment]:
        clauses = [
            "(strpos(lower(display_name), lower(:query)) > 0 OR "
            "strpos(deployment_key, lower(:query)) > 0)"
        ]
        if state is not None:
            clauses.append("state=:state")
        if after_key is not None:
            clauses.append("deployment_key > :after_key")
        rows = self.db.execute(
            text(
                "SELECT * FROM model_gateway.deployment WHERE "
                + " AND ".join(clauses)
                + " ORDER BY deployment_key LIMIT :limit"
            ),
            {"state": state, "query": query, "after_key": after_key, "limit": limit},
        ).mappings()
        return [_deployment(row) for row in rows]


def _deployment(row: Any) -> Deployment:
    return Deployment.model_validate(dict(row) | {"id": str(row["id"])})
