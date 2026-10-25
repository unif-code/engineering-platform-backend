from datetime import datetime
from typing import Any

from sqlalchemy import text

from control_plane.app.modules.model_gateway.adapters import SqlAlchemyDeploymentRepository
from control_plane.app.modules.model_gateway.adapters.checks import connection_busy
from control_plane.app.modules.model_gateway.domain.metadata import MetadataObservation


class SqlAlchemyMetadataRepository(SqlAlchemyDeploymentRepository):
    def observation(
        self, observation_id: str, *, for_update: bool = False
    ) -> MetadataObservation | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            self.db.execute(
                text(f"SELECT * FROM model_gateway.metadata_observation WHERE id=:id{suffix}"),
                {"id": observation_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _observation(row)

    def insert_observation(self, value: MetadataObservation) -> bool:
        return (
            self.db.execute(
                text("""
            INSERT INTO model_gateway.metadata_observation
                (id,deployment_id,revision,requested_by,requested_at,input,connection_ref,state,
                 reason,attempt,execution_token,started_at,deadline_at,finished_at,elapsed_ms,
                 response_sha256,response_bytes,provider_request_id,report,material_currentness)
            VALUES (:id,:deployment_id,:revision,:requested_by,:requested_at,CAST(:input AS JSONB),
                 :connection_ref,:state,:reason,:attempt,:execution_token,:started_at,:deadline_at,
                 :finished_at,:elapsed_ms,:response_sha256,:response_bytes,:provider_request_id,
                 CAST(:report AS JSONB),:material_currentness)
            ON CONFLICT (deployment_id) WHERE state IN ('QUEUED','RUNNING') DO NOTHING RETURNING id
        """),
                _parameters(value),
            ).scalar_one_or_none()
            is not None
        )

    def active_observation(self, deployment_id: str) -> bool:
        return bool(
            self.db.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM model_gateway.metadata_observation "
                    "WHERE deployment_id=:id AND state IN ('QUEUED','RUNNING'))"
                ),
                {"id": deployment_id},
            ).scalar_one()
        )

    def connection_busy(self, reference: str) -> bool:
        return connection_busy(self.db, reference)

    def queued_ids(self, *, limit: int) -> list[str]:
        return [
            str(x)
            for x in self.db.execute(
                text(
                    "SELECT id FROM model_gateway.metadata_observation WHERE state='QUEUED' "
                    "ORDER BY requested_at,id LIMIT :limit"
                ),
                {"limit": limit},
            ).scalars()
        ]

    def expired_ids(self, *, now: datetime, limit: int) -> list[str]:
        return [
            str(x)
            for x in self.db.execute(
                text(
                    "SELECT id FROM model_gateway.metadata_observation WHERE state='RUNNING' "
                    "AND deadline_at<=:now ORDER BY deadline_at,id LIMIT :limit"
                ),
                {"now": now, "limit": limit},
            ).scalars()
        ]

    def list_observations(
        self, deployment_id: str, *, before_at: datetime | None, before_id: str | None, limit: int
    ) -> list[MetadataObservation]:
        rows = self.db.execute(
            text("""
            SELECT * FROM model_gateway.metadata_observation WHERE deployment_id=:id
              AND (CAST(:before_at AS TIMESTAMPTZ) IS NULL
                   OR (requested_at,id)<(:before_at,CAST(:before_id AS UUID)))
            ORDER BY requested_at DESC,id DESC LIMIT :limit
        """),
            {"id": deployment_id, "before_at": before_at, "before_id": before_id, "limit": limit},
        ).mappings()
        return [_observation(row) for row in rows]

    def save_observation(self, value: MetadataObservation, *, expected_revision: int) -> bool:
        result = self.db.execute(
            text("""
            UPDATE model_gateway.metadata_observation
            SET revision=:revision,state=:state,reason=:reason,
                attempt=:attempt,execution_token=:execution_token,started_at=:started_at,
                deadline_at=:deadline_at,finished_at=:finished_at,elapsed_ms=:elapsed_ms,
                response_sha256=:response_sha256,response_bytes=:response_bytes,
                provider_request_id=:provider_request_id,report=CAST(:report AS JSONB),
                material_currentness=:material_currentness
            WHERE id=:id AND revision=:expected_revision AND state IN ('QUEUED','RUNNING')
                AND (:state<>'RUNNING' OR EXISTS(SELECT 1 FROM model_gateway.deployment d
                    WHERE d.id=metadata_observation.deployment_id AND d.state='DRAFT'
                    AND d.revision=(metadata_observation.input->>'deployment_revision')::integer))
        """),
            _parameters(value) | {"expected_revision": expected_revision},
        )
        return result.rowcount == 1


def _parameters(value: MetadataObservation) -> dict[str, Any]:
    return value.model_dump() | {
        "input": value.input.model_dump_json(),
        "connection_ref": value.input.connection_ref,
        "report": value.report.model_dump_json() if value.report else None,
    }


def _observation(row: Any) -> MetadataObservation:
    values = dict(row)
    values.pop("connection_ref")
    for key in ("id", "deployment_id", "execution_token"):
        if values[key] is not None:
            values[key] = str(values[key])
    return MetadataObservation.model_validate(values)
