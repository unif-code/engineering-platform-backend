import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from control_plane.app.modules.model_gateway.adapters import SqlAlchemyDeploymentRepository
from control_plane.app.modules.model_gateway.adapters.checks import SqlAlchemyCheckRepository
from control_plane.app.modules.model_gateway.domain import Deployment
from control_plane.app.modules.model_gateway.domain.checks import ConnectionCheck
from control_plane.app.modules.model_gateway.domain.dossiers import ValidationDossier


class SqlAlchemyValidationDossierRepository:
    def __init__(self, db: Connection) -> None:
        self.db = db
        self._common = SqlAlchemyDeploymentRepository(db)

    def get(self, deployment_id: str, *, for_update: bool = False) -> Deployment | None:
        return self._common.get(deployment_id, for_update=for_update)

    def check(self, check_id: str, *, for_update: bool = False) -> ConnectionCheck | None:
        return SqlAlchemyCheckRepository(self.db).check(check_id, for_update=for_update)

    def claim_idempotency(self, **values: Any) -> bool:
        return self._common.claim_idempotency(**values)

    def idempotency_by_scope(
        self, actor: str, operation: str, idempotency_key: str, *, for_update: bool = False
    ) -> Any:
        return self._common.idempotency_by_scope(
            actor, operation, idempotency_key, for_update=for_update
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
        return self._common.complete_idempotency(
            record_id,
            http_status=http_status,
            result_metadata=result_metadata,
            sealed_response=sealed_response,
            now=now,
        )

    def insert_dossier(self, value: ValidationDossier) -> None:
        payload = value.model_dump(mode="json", exclude={"snapshot_hash"})
        self.db.execute(
            text("""
            INSERT INTO model_gateway.validation_dossier
                (id,deployment_id,candidate_revision,created_by,created_at,snapshot_hash,snapshot_text)
            VALUES (:id,:deployment_id,:candidate_revision,:created_by,:created_at,
                    :snapshot_hash,:snapshot_text)
        """),
            {
                "id": value.id,
                "deployment_id": value.deployment_id,
                "candidate_revision": value.candidate_revision,
                "created_by": value.created_by,
                "created_at": value.created_at,
                "snapshot_hash": value.snapshot_hash,
                "snapshot_text": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            },
        )

    def dossier(self, dossier_id: str) -> ValidationDossier | None:
        row = (
            self.db.execute(
                text(
                    "SELECT snapshot_text,snapshot_hash FROM model_gateway.validation_dossier "
                    "WHERE id=:id"
                ),
                {"id": dossier_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _dossier(row)

    def list_dossiers(
        self, deployment_id: str, *, before_at: datetime | None, before_id: str | None, limit: int
    ) -> list[ValidationDossier]:
        cursor = " AND (created_at,id)<(:before_at,:before_id)" if before_at is not None else ""
        rows = self.db.execute(
            text(
                "SELECT snapshot_text,snapshot_hash FROM model_gateway.validation_dossier "
                "WHERE deployment_id=:id"
                + cursor
                + " ORDER BY created_at DESC,id DESC LIMIT :limit"
            ),
            {"id": deployment_id, "before_at": before_at, "before_id": before_id, "limit": limit},
        ).mappings()
        return [_dossier(row) for row in rows]


def _dossier(row: Any) -> ValidationDossier:
    return ValidationDossier.model_validate(
        json.loads(row["snapshot_text"]) | {"snapshot_hash": row["snapshot_hash"]}
    )
