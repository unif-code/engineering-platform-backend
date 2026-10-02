import json
from datetime import datetime
from typing import Any

from sqlalchemy import text

from control_plane.app.modules.model_gateway.adapters.dossiers import (
    SqlAlchemyValidationDossierRepository,
)
from control_plane.app.modules.model_gateway.domain.source_checks import MaterialSourceCheck


class SqlAlchemyMaterialSourceCheckRepository(SqlAlchemyValidationDossierRepository):
    def insert_source_check(self, value: MaterialSourceCheck) -> None:
        self.db.execute(
            text("""
            INSERT INTO model_gateway.material_source_check
                (id,deployment_id,candidate_revision,dossier_id,material_index,
                 created_by,created_at,snapshot_hash,snapshot_text)
            VALUES (:id,:deployment_id,:candidate_revision,:dossier_id,:material_index,
                    :created_by,:created_at,:snapshot_hash,:snapshot_text)
        """),
            {
                "id": value.id,
                "deployment_id": value.deployment_id,
                "candidate_revision": value.candidate_revision,
                "dossier_id": value.dossier_id,
                "material_index": value.material_index,
                "created_by": value.created_by,
                "created_at": value.created_at,
                "snapshot_hash": value.snapshot_hash,
                "snapshot_text": json.dumps(
                    value.model_dump(mode="json", exclude={"snapshot_hash"}),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )

    def source_check(self, source_check_id: str) -> MaterialSourceCheck | None:
        row = (
            self.db.execute(
                text(
                    "SELECT snapshot_text,snapshot_hash "
                    "FROM model_gateway.material_source_check WHERE id=:id"
                ),
                {"id": source_check_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _source_check(row)

    def list_source_checks(
        self,
        deployment_id: str,
        dossier_id: str,
        *,
        before_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> list[MaterialSourceCheck]:
        cursor = " AND (created_at,id)<(:before_at,:before_id)" if before_at is not None else ""
        rows = self.db.execute(
            text(
                "SELECT snapshot_text,snapshot_hash "
                "FROM model_gateway.material_source_check WHERE deployment_id=:deployment_id "
                "AND dossier_id=:dossier_id"
                + cursor
                + " ORDER BY created_at DESC,id DESC LIMIT :limit"
            ),
            {
                "deployment_id": deployment_id,
                "dossier_id": dossier_id,
                "before_at": before_at,
                "before_id": before_id,
                "limit": limit,
            },
        ).mappings()
        return [_source_check(row) for row in rows]


def _source_check(row: Any) -> MaterialSourceCheck:
    return MaterialSourceCheck.model_validate(
        json.loads(row["snapshot_text"])
        | {
            "snapshot_hash": row["snapshot_hash"],
        }
    )
