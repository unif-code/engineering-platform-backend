import json
from datetime import datetime
from typing import Any

from sqlalchemy import text

from control_plane.app.modules.model_gateway.adapters import SqlAlchemyDeploymentRepository
from control_plane.app.modules.model_gateway.domain.checks import ConnectionCheck
from control_plane.app.modules.model_gateway.domain.connections import CheckKind


class SqlAlchemyCheckRepository(SqlAlchemyDeploymentRepository):
    def check(self, check_id: str, *, for_update: bool = False) -> ConnectionCheck | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            self.db.execute(
                text(f"SELECT * FROM model_gateway.connection_check WHERE id=:id{suffix}"),
                {"id": check_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _check(row)

    def insert_check(self, value: ConnectionCheck) -> bool:
        return (
            self.db.execute(
                text("""
            INSERT INTO model_gateway.connection_check
                (id,deployment_id,revision,requested_by,requested_at,input,connection_ref,check_kind,
                 state,reason,attempt,material_currentness,finished_at)
            VALUES (:id,:deployment_id,:revision,:requested_by,:requested_at,CAST(:input AS JSONB),
                    :connection_ref,:check_kind,:state,:reason,:attempt,:material_currentness,:finished_at)
            ON CONFLICT DO NOTHING RETURNING id
        """),
                _parameters(value),
            ).scalar_one_or_none()
            is not None
        )

    def active_check(self, deployment_id: str) -> ConnectionCheck | None:
        row = (
            self.db.execute(
                text(
                    "SELECT * FROM model_gateway.connection_check WHERE deployment_id=:id "
                    "AND state IN ('QUEUED','RUNNING')"
                ),
                {"id": deployment_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _check(row)

    def list_checks(
        self, deployment_id: str, *, before_at: datetime | None, before_id: str | None, limit: int
    ) -> list[ConnectionCheck]:
        cursor = " AND (requested_at,id) < (:before_at,:before_id)" if before_at else ""
        rows = self.db.execute(
            text(
                "SELECT * FROM model_gateway.connection_check WHERE deployment_id=:id"
                + cursor
                + " ORDER BY requested_at DESC,id DESC LIMIT :limit"
            ),
            {"id": deployment_id, "before_at": before_at, "before_id": before_id, "limit": limit},
        ).mappings()
        return [_check(row) for row in rows]

    def queued_ids(self, *, limit: int) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text(
                    "SELECT id FROM model_gateway.connection_check WHERE state='QUEUED' "
                    "ORDER BY requested_at,id LIMIT :limit"
                ),
                {"limit": limit},
            ).scalars()
        ]

    def expired_ids(self, *, now: datetime, limit: int) -> list[str]:
        return [
            str(value)
            for value in self.db.execute(
                text(
                    "SELECT id FROM model_gateway.connection_check WHERE state='RUNNING' "
                    "AND deadline_at<=:now ORDER BY deadline_at,id LIMIT :limit"
                ),
                {"now": now, "limit": limit},
            ).scalars()
        ]

    def connection_busy(self, reference: str) -> bool:
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:reference,0))"),
            {"reference": f"model-gateway:{reference}"},
        )
        return bool(
            self.db.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM model_gateway.connection_check "
                    "WHERE connection_ref=:reference AND state='RUNNING')"
                ),
                {"reference": reference},
            ).scalar_one()
        )

    def save_check(self, value: ConnectionCheck, *, expected_revision: int) -> bool:
        result = self.db.execute(
            text("""
            UPDATE model_gateway.connection_check SET
                revision=:revision,state=:state,reason=:reason,attempt=:attempt,
                execution_token=:execution_token,started_at=:started_at,deadline_at=:deadline_at,
                finished_at=:finished_at,elapsed_ms=:elapsed_ms,provider_request_id=:provider_request_id,
                reported_model_id=:reported_model_id,usage=CAST(:usage AS JSONB),
                material_currentness=:material_currentness,
                observed_bytes=:observed_bytes,observed_text=:observed_text,
                observed_normal_completion=:observed_normal_completion,observed_data_events=:observed_data_events,
                observed_text_deltas=:observed_text_deltas,observed_text_bytes=:observed_text_bytes,
                observed_completion_marker=:observed_completion_marker,observed_local_closed=:observed_local_closed,
                provider_cancellation=:provider_cancellation,
                observed_reasoning=:observed_reasoning,observed_reasoning_deltas=:observed_reasoning_deltas,
                observed_reasoning_bytes=:observed_reasoning_bytes,
                observed_search_calls=:observed_search_calls,observed_source_signal=:observed_source_signal,
                provider_search_call_count=:provider_search_call_count,
                search_sources=CAST(:search_sources AS JSONB),
                search_queries=CAST(:search_queries AS JSONB)
            WHERE id=:id AND revision=:expected_revision AND state IN ('QUEUED','RUNNING')
                AND (:state <> 'RUNNING' OR EXISTS (
                    SELECT 1 FROM model_gateway.deployment d
                    WHERE d.id=connection_check.deployment_id AND d.state='DRAFT'
                      AND d.revision=(connection_check.input->>'deployment_revision')::integer))
        """),
            _parameters(value) | {"expected_revision": expected_revision},
        )
        return result.rowcount == 1


_OBSERVATION_FIELDS = {
    "observed_bytes": "consumed_bytes",
    "observed_text": "text_observed",
    "observed_normal_completion": "normal_completion_observed",
    "observed_data_events": "data_event_count",
    "observed_text_deltas": "text_delta_count",
    "observed_text_bytes": "text_bytes",
    "observed_completion_marker": "completion_marker_observed",
    "observed_local_closed": "local_stream_closed",
    "provider_cancellation": "provider_cancellation",
    "observed_reasoning": "reasoning_observed",
    "observed_reasoning_deltas": "reasoning_delta_count",
    "observed_reasoning_bytes": "reasoning_bytes",
    "observed_search_calls": "completed_search_call_count",
    "observed_source_signal": "source_signal_observed",
    "provider_search_call_count": "provider_search_call_count",
    "search_sources": "sources",
    "search_queries": "queries",
}


def _parameters(value: ConnectionCheck) -> dict[str, Any]:
    observation = {} if value.observation is None else value.observation.model_dump()
    if value.check_kind is CheckKind.SEARCH_SOURCES and value.observation is not None:
        observation["local_stream_closed"] = observation.pop("local_response_closed")
    parameters = value.model_dump(exclude={"observation"}) | {
        "input": value.input.model_dump_json(),
        "connection_ref": value.input.connection_ref,
        "usage": value.usage.model_dump_json() if value.usage else None,
        **{column: observation.get(field) for column, field in _OBSERVATION_FIELDS.items()},
    }

    for column in ("search_sources", "search_queries"):
        if parameters[column] is not None:
            parameters[column] = json.dumps(parameters[column], separators=(",", ":"))
    return parameters


def _check(row: Any) -> ConnectionCheck:
    values = dict(row)
    values.pop("connection_ref")
    observation = {field: values.pop(column) for column, field in _OBSERVATION_FIELDS.items()}
    if values["check_kind"] == "SEARCH_SOURCES":
        observation["local_response_closed"] = observation.pop("local_stream_closed")
    values["observation"] = (
        {
            "kind": values["check_kind"],
            **{key: value for key, value in observation.items() if value is not None},
        }
        if observation["consumed_bytes"] is not None
        else None
    )
    return ConnectionCheck.model_validate(
        values
        | {
            "id": str(row["id"]),
            "deployment_id": str(row["deployment_id"]),
            "execution_token": str(row["execution_token"]) if row["execution_token"] else None,
        }
    )
