# ruff: noqa: E501

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from control_plane.app.modules.agent.domain import (
    AgentAttempt,
    AgentAuditAppend,
    AgentDefinition,
    AgentIdempotencyRecord,
    AgentQueryUnavailable,
    AgentRun,
    AgentRunBusinessContext,
    AgentRunListItem,
    AttemptMutation,
    CanonicalEventInput,
    CheckpointInput,
    EventAcceptanceReceipt,
    EventCursorAnchorMissing,
    ExecutionBinding,
    IdempotencyCompletion,
    IdempotencyReservation,
    RunMutation,
    RunState,
    WorkflowClaimMode,
    WorkflowCommand,
    WorkflowCommandState,
    WorkflowDispatchMutation,
)
from control_plane.app.modules.agent.domain.errors import EventReplayConflict

T = TypeVar("T")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _json(value: object) -> str:
    return json.dumps(_plain_json(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _event_digest(event: CanonicalEventInput) -> str:
    return "sha256:" + hashlib.sha256(_json(event.model_dump(mode="json")).encode()).hexdigest()


def _dto_values(row: Any) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, UUID) else value for key, value in dict(row).items()
    }


class SqlAlchemyAgentRepository:
    """Keep SQLAlchemy rows and PostgreSQL JSON values inside this adapter."""

    def __init__(self, db: Connection) -> None:
        self.db = db

    def insert_definition(self, definition: AgentDefinition) -> AgentDefinition:
        definition = AgentDefinition.model_validate(definition.model_dump(mode="python"))
        values = definition.model_dump(mode="json")
        row = (
            self.db.execute(
                text(
                    "INSERT INTO agent.agent_definition "
                    "(id, version, name, capability_declarations, skill_declarations, "
                    "runtime_permissions, input_schema, created_at) VALUES "
                    "(:id, :version, :name, CAST(:capabilities AS JSONB), CAST(:skills AS JSONB), "
                    "CAST(:permissions AS JSONB), CAST(:input_schema AS JSONB), :created_at) RETURNING *"
                ),
                {
                    **values,
                    "capabilities": _json(values["capability_declarations"]),
                    "skills": _json(values["skill_declarations"]),
                    "permissions": _json(values["runtime_permissions"]),
                    "input_schema": _json(values["input_schema"]),
                },
            )
            .mappings()
            .one()
        )
        return self._definition(row)

    def definition_by_id(self, definition_id: str, version: int) -> AgentDefinition | None:
        row = (
            self.db.execute(
                text(
                    "SELECT * FROM agent.agent_definition WHERE id=CAST(:id AS UUID) AND version=:version"
                ),
                {"id": definition_id, "version": version},
            )
            .mappings()
            .one_or_none()
        )
        return self._definition(row) if row is not None else None

    def list_definitions(self) -> tuple[AgentDefinition, ...]:
        rows = self.db.execute(
            text("SELECT * FROM agent.agent_definition ORDER BY name, version")
        ).mappings()
        return tuple(self._definition(row) for row in rows)

    def insert_run(self, run: AgentRun) -> AgentRun:
        run = AgentRun.model_validate(run.model_dump(mode="python"))
        if run.business_context is None:
            raise ValueError("New Agent Run requires a complete business context")
        row = (
            self.db.execute(
                text(
                    "INSERT INTO agent.agent_run "
                    "(id, workspace_id, goal_ref, created_by, definition_id, definition_version, "
                    "latest_attempt_id, state, revision, created_at, updated_at, "
                    "requirement_id, work_item_id, assignment_id) VALUES "
                    "(:id, :workspace_id, :goal_ref, :created_by, :definition_id, "
                    ":definition_version, :latest_attempt_id, :state, :revision, "
                    ":created_at, :updated_at, :requirement_id, :work_item_id, :assignment_id) RETURNING *"
                ),
                run.model_dump(mode="json", exclude={"business_context"})
                | run.business_context.model_dump(mode="json"),
            )
            .mappings()
            .one()
        )
        return self._run(row)

    def run_by_id(self, run_id: str, *, for_update: bool = False) -> AgentRun | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            self.db.execute(
                text(f"SELECT * FROM agent.agent_run WHERE id=CAST(:id AS UUID){suffix}"),
                {"id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        return self._run(row) if row is not None else None

    def runs_page(
        self,
        workspace_id: str,
        *,
        state: RunState | None,
        before_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> tuple[AgentRunListItem, ...]:
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("bounded Run repository limit required")
        state_filter = " AND run.state=:state" if state is not None else ""
        cursor_filter = (
            " AND (run.created_at,run.id)<(:before_at,CAST(:before_id AS UUID))"
            if before_at is not None
            else ""
        )
        rows = self.db.execute(
            text(
                "SELECT to_jsonb(run) AS run, to_jsonb(attempt) AS latest_attempt, "
                "to_jsonb(binding) AS binding, to_jsonb(checkpoint) AS checkpoint "
                "FROM agent.agent_run AS run "
                "LEFT JOIN agent.agent_attempt AS attempt ON attempt.id=run.latest_attempt_id AND attempt.run_id=run.id "
                "LEFT JOIN agent.execution_binding AS binding ON binding.attempt_id=attempt.id AND binding.id=attempt.binding_id "
                "LEFT JOIN agent.checkpoint AS checkpoint ON checkpoint.id=attempt.checkpoint_id AND checkpoint.attempt_id=attempt.id "
                "WHERE run.workspace_id=CAST(:workspace_id AS UUID)"
                + state_filter
                + cursor_filter
                + " ORDER BY run.created_at DESC,run.id DESC LIMIT :limit"
            ),
            {
                "workspace_id": workspace_id,
                "state": state.value if state is not None else None,
                "before_at": before_at,
                "before_id": before_id,
                "limit": limit,
            },
        ).mappings()
        result = []
        for row in rows:
            try:
                if row["latest_attempt"] is None or row["binding"] is None:
                    raise ValueError("latest execution association missing")
                attempt_values = _dto_values(row["latest_attempt"])
                checkpoint_id = attempt_values.pop("checkpoint_id", None)
                checkpoint = (
                    self._checkpoint(row["checkpoint"]) if row["checkpoint"] is not None else None
                )
                if checkpoint_id is not None and (
                    checkpoint is None or checkpoint.id != checkpoint_id
                ):
                    raise ValueError("checkpoint association missing")
                result.append(
                    AgentRunListItem(
                        run=self._run(row["run"]),
                        latest_attempt=AgentAttempt.model_validate(
                            attempt_values | {"checkpoint": checkpoint}
                        ),
                        binding=self._binding(row["binding"]),
                    )
                )
            except (ValueError, TypeError, KeyError):
                raise AgentQueryUnavailable("Agent Run associations unavailable") from None
        return tuple(result)

    def compare_and_set_run(
        self, run_id: str, *, expected_revision: int, mutation: RunMutation
    ) -> AgentRun | None:
        mutation = RunMutation.model_validate(mutation.model_dump(mode="python"))
        row = (
            self.db.execute(
                text(
                    "UPDATE agent.agent_run SET state=:state, "
                    "latest_attempt_id=CAST(:latest_attempt_id AS UUID), revision=revision + 1, "
                    "updated_at=:now WHERE id=CAST(:id AS UUID) AND revision=:expected_revision "
                    "RETURNING *"
                ),
                {
                    "id": run_id,
                    "expected_revision": expected_revision,
                    "state": mutation.state.value,
                    "latest_attempt_id": mutation.latest_attempt_id,
                    "now": mutation.now,
                },
            )
            .mappings()
            .one_or_none()
        )
        return self._run(row) if row is not None else None

    def insert_attempt(self, attempt: AgentAttempt) -> AgentAttempt:
        attempt = AgentAttempt.model_validate(attempt.model_dump(mode="python"))
        row = (
            self.db.execute(
                text(
                    "INSERT INTO agent.agent_attempt "
                    "(id, run_id, number, state, binding_id, binding_digest, runner_generation, "
                    "fencing_token, checkpoint_id, event_sequence, waiting_deadline, terminal_evidence, "
                    "revision, created_at, updated_at) "
                    "VALUES (:id, :run_id, :number, :state, :binding_id, :binding_digest, "
                    ":runner_generation, :fencing_token, CAST(:checkpoint_id AS UUID), :event_sequence, "
                    ":waiting_deadline, CAST(:terminal_evidence AS JSONB), :revision, :created_at, "
                    ":updated_at) RETURNING *"
                ),
                {
                    **attempt.model_dump(mode="json"),
                    "checkpoint_id": attempt.checkpoint.id if attempt.checkpoint else None,
                    "terminal_evidence": _json(attempt.terminal_evidence)
                    if attempt.terminal_evidence is not None
                    else None,
                },
            )
            .mappings()
            .one()
        )
        return self._attempt(row)

    def attempt_by_id(self, attempt_id: str, *, for_update: bool = False) -> AgentAttempt | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            self.db.execute(
                text(f"SELECT * FROM agent.agent_attempt WHERE id=CAST(:id AS UUID){suffix}"),
                {"id": attempt_id},
            )
            .mappings()
            .one_or_none()
        )
        return self._attempt(row) if row is not None else None

    def attempts_by_run_id(self, run_id: str) -> tuple[AgentAttempt, ...]:
        rows = self.db.execute(
            text(
                "SELECT * FROM agent.agent_attempt WHERE run_id=CAST(:run_id AS UUID) "
                "ORDER BY number"
            ),
            {"run_id": run_id},
        ).mappings()
        return tuple(self._attempt(row) for row in rows)

    def compare_and_set_attempt(
        self, attempt_id: str, *, expected_revision: int, mutation: AttemptMutation
    ) -> AgentAttempt | None:
        mutation = AttemptMutation.model_validate(mutation.model_dump(mode="python"))
        row = (
            self.db.execute(
                text(
                    "UPDATE agent.agent_attempt SET state=:state, runner_generation=:runner_generation, "
                    "fencing_token=:fencing_token, checkpoint_id=CAST(:checkpoint_id AS UUID), "
                    "event_sequence=:event_sequence, waiting_deadline=:waiting_deadline, "
                    "terminal_evidence=CAST(:terminal_evidence AS JSONB), revision=revision + 1, "
                    "updated_at=:now WHERE id=CAST(:id AS UUID) AND revision=:expected_revision "
                    "RETURNING *"
                ),
                {
                    "id": attempt_id,
                    "expected_revision": expected_revision,
                    "state": mutation.state.value,
                    "runner_generation": mutation.runner_generation,
                    "fencing_token": mutation.fencing_token,
                    "checkpoint_id": mutation.checkpoint.id if mutation.checkpoint else None,
                    "event_sequence": mutation.event_sequence,
                    "waiting_deadline": mutation.waiting_deadline,
                    "terminal_evidence": _json(mutation.terminal_evidence)
                    if mutation.terminal_evidence is not None
                    else None,
                    "now": mutation.now,
                },
            )
            .mappings()
            .one_or_none()
        )
        return self._attempt(row) if row is not None else None

    def insert_binding(self, attempt_id: str, binding: ExecutionBinding) -> ExecutionBinding:
        binding = ExecutionBinding.model_validate(binding.model_dump(mode="python"))
        row = (
            self.db.execute(
                text(
                    "INSERT INTO agent.execution_binding (id, attempt_id, source, digest, snapshot) VALUES "
                    "(:id, CAST(:attempt_id AS UUID), :source, :digest, CAST(:snapshot AS JSONB)) "
                    "RETURNING *"
                ),
                {
                    "id": binding.id,
                    "attempt_id": attempt_id,
                    "source": binding.source.value,
                    "digest": binding.digest,
                    "snapshot": _json(binding.model_dump(mode="json")),
                },
            )
            .mappings()
            .one()
        )
        return self._binding(row)

    def binding_by_attempt_id(self, attempt_id: str) -> ExecutionBinding | None:
        row = (
            self.db.execute(
                text("SELECT * FROM agent.execution_binding WHERE attempt_id=CAST(:id AS UUID)"),
                {"id": attempt_id},
            )
            .mappings()
            .one_or_none()
        )
        return self._binding(row) if row is not None else None

    def append_event_receipt(self, receipt: EventAcceptanceReceipt) -> None:
        receipt = EventAcceptanceReceipt.model_validate(receipt.model_dump(mode="python"))
        self.db.execute(
            text(
                "INSERT INTO agent.event_acceptance_receipt "
                "(event_id, schema_version, attempt, checkpoint) VALUES "
                "(CAST(:event_id AS UUID), :schema_version, CAST(:attempt AS JSONB), CAST(:checkpoint AS JSONB))"
            ),
            {
                "event_id": receipt.event_id,
                "schema_version": receipt.schema_version,
                "attempt": _json(receipt.attempt.model_dump(mode="json")),
                "checkpoint": _json(receipt.checkpoint.model_dump(mode="json"))
                if receipt.checkpoint is not None
                else None,
            },
        )

    def event_receipt_by_id(self, event_id: str) -> EventAcceptanceReceipt | None:
        row = (
            self.db.execute(
                text(
                    "SELECT * FROM agent.event_acceptance_receipt WHERE event_id=CAST(:id AS UUID)"
                ),
                {"id": event_id},
            )
            .mappings()
            .one_or_none()
        )
        return EventAcceptanceReceipt.model_validate(_dto_values(row)) if row is not None else None

    def append_event(self, event: CanonicalEventInput) -> CanonicalEventInput:
        event = CanonicalEventInput.model_validate(event.model_dump(mode="python"))
        digest = _event_digest(event)
        row = (
            self.db.execute(
                text(
                    "INSERT INTO agent.canonical_event "
                    "(event_id, event_type, attempt_id, runner_generation, sequence, correlation_id, "
                    "causation_id, trace_id, span_id, summary, data, payload_digest) VALUES "
                    "(:event_id, :event_type, CAST(:attempt_id AS UUID), :generation, :sequence, "
                    ":correlation_id, :causation_id, :trace_id, :span_id, :summary, "
                    "CAST(:data AS JSONB), :payload_digest) ON CONFLICT DO NOTHING RETURNING *"
                ),
                {
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "attempt_id": event.attempt_id,
                    "generation": event.generation,
                    "sequence": event.sequence,
                    "correlation_id": event.correlation_id,
                    "causation_id": event.causation_id,
                    "trace_id": event.trace_id,
                    "span_id": event.span_id,
                    "summary": event.summary,
                    "data": _json(event.model_dump(mode="json")["data"]),
                    "payload_digest": digest,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return self._event(row)
        existing = self.event_by_id(event.id)
        if existing is not None:
            if _event_digest(existing) == digest:
                return existing
            raise EventReplayConflict(f"canonical event {event.id} changed during replay")
        sequence = (
            self.db.execute(
                text(
                    "SELECT * FROM agent.canonical_event WHERE attempt_id=CAST(:attempt_id AS UUID) "
                    "AND runner_generation=:generation AND sequence=:sequence"
                ),
                {
                    "attempt_id": event.attempt_id,
                    "generation": event.generation,
                    "sequence": event.sequence,
                },
            )
            .mappings()
            .one_or_none()
        )
        if sequence is not None:
            raise EventReplayConflict(
                "canonical event identity already has immutable sequence evidence"
            )
        raise EventReplayConflict("canonical event insert conflicted without replayable evidence")

    def event_by_id(self, event_id: str) -> CanonicalEventInput | None:
        row = (
            self.db.execute(
                text("SELECT * FROM agent.canonical_event WHERE event_id=CAST(:id AS UUID)"),
                {"id": event_id},
            )
            .mappings()
            .one_or_none()
        )
        return self._event(row) if row is not None else None

    def event_by_position(
        self, attempt_id: str, *, generation: int, sequence: int
    ) -> CanonicalEventInput | None:
        row = (
            self.db.execute(
                text(
                    "SELECT * FROM agent.canonical_event WHERE attempt_id=CAST(:attempt_id AS UUID) "
                    "AND runner_generation=:generation AND sequence=:sequence"
                ),
                {"attempt_id": attempt_id, "generation": generation, "sequence": sequence},
            )
            .mappings()
            .one_or_none()
        )
        return self._event(row) if row is not None else None

    def events_page_by_run_id(
        self,
        run_id: str,
        *,
        after_event_id: str | None,
        limit: int,
    ) -> tuple[CanonicalEventInput, ...]:
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("bounded event repository limit required")
        anchor = None
        if after_event_id is not None:
            anchor = (
                self.db.execute(
                    text(
                        "SELECT attempt.number, event.runner_generation, event.sequence, event.event_id "
                        "FROM agent.canonical_event AS event JOIN agent.agent_attempt AS attempt ON attempt.id=event.attempt_id "
                        "WHERE attempt.run_id=CAST(:run_id AS UUID) AND event.event_id=CAST(:event_id AS UUID)"
                    ),
                    {"run_id": run_id, "event_id": after_event_id},
                )
                .mappings()
                .one_or_none()
            )
            if anchor is None:
                raise EventCursorAnchorMissing("event cursor does not belong to Agent Run")
        cursor_filter = (
            (
                " AND (attempt.number,event.runner_generation,event.sequence,event.event_id)>"
                "(:number,:generation,:sequence,CAST(:event_id AS UUID))"
            )
            if anchor is not None
            else ""
        )
        rows = self.db.execute(
            text(
                "SELECT event.* FROM agent.canonical_event AS event "
                "JOIN agent.agent_attempt AS attempt ON attempt.id=event.attempt_id "
                "WHERE attempt.run_id=CAST(:run_id AS UUID) "
                + cursor_filter
                + " ORDER BY attempt.number, event.runner_generation, event.sequence, event.event_id LIMIT :limit"
            ),
            {
                "run_id": run_id,
                "limit": limit,
                **(
                    {
                        "number": anchor["number"],
                        "generation": anchor["runner_generation"],
                        "sequence": anchor["sequence"],
                        "event_id": str(anchor["event_id"]),
                    }
                    if anchor is not None
                    else {}
                ),
            },
        ).mappings()
        return tuple(self._event(row) for row in rows)

    def insert_checkpoint(self, attempt_id: str, checkpoint: CheckpointInput) -> CheckpointInput:
        checkpoint = CheckpointInput.model_validate(checkpoint.model_dump(mode="python"))
        row = (
            self.db.execute(
                text(
                    "INSERT INTO agent.checkpoint "
                    "(id, attempt_id, artifact_id, artifact_version, content_sha256, schema_version, "
                    "adapter_version, classification) VALUES (:id, CAST(:attempt_id AS UUID), :artifact_id, "
                    ":artifact_version, :content_sha256, :schema_version, :adapter_version, :classification) "
                    "RETURNING *"
                ),
                {"attempt_id": attempt_id, **checkpoint.model_dump(mode="json")},
            )
            .mappings()
            .one()
        )
        return self._checkpoint(row)

    def checkpoint_by_id(self, checkpoint_id: str) -> CheckpointInput | None:
        row = (
            self.db.execute(
                text("SELECT * FROM agent.checkpoint WHERE id=CAST(:id AS UUID)"),
                {"id": checkpoint_id},
            )
            .mappings()
            .one_or_none()
        )
        return self._checkpoint(row) if row is not None else None

    def insert_workflow_command(self, command: WorkflowCommand) -> WorkflowCommand:
        row = (
            self.db.execute(
                text(
                    "INSERT INTO agent.workflow_command "
                    "(id, command_key, kind, attempt_id, generation, state, dispatch_attempts, receipt, "
                    "last_error_code, created_at, updated_at, dispatched_at) VALUES "
                    "(:id, :command_key, :kind, CAST(:attempt_id AS UUID), :generation, :state, "
                    ":dispatch_attempts, CAST(:receipt AS JSONB), :last_error_code, :created_at, "
                    ":updated_at, :dispatched_at) RETURNING *"
                ),
                {
                    **command.model_dump(mode="json"),
                    "receipt": _json(command.receipt) if command.receipt is not None else None,
                },
            )
            .mappings()
            .one()
        )
        return self._command(row)

    def workflow_command_by_id(
        self, command_id: str, *, for_update: bool = False
    ) -> WorkflowCommand | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            self.db.execute(
                text(f"SELECT * FROM agent.workflow_command WHERE id=CAST(:id AS UUID){suffix}"),
                {"id": command_id},
            )
            .mappings()
            .one_or_none()
        )
        return self._command(row) if row is not None else None

    def claim_workflow_commands(
        self,
        *,
        limit: int,
        now: datetime,
        claim_owner: str | None,
        claim_token: str | None,
        claim_lease_until: datetime | None,
        claim_mode: WorkflowClaimMode | None,
    ) -> tuple[WorkflowCommand, ...]:
        provided = tuple(
            value is not None for value in (claim_owner, claim_token, claim_lease_until, claim_mode)
        )
        if any(provided) != all(provided) or not any(provided):
            raise ValueError("workflow claim owner, token, lease, and mode are required together")
        assert claim_owner is not None
        assert claim_token is not None
        assert claim_lease_until is not None
        assert claim_mode is not None
        if claim_mode is WorkflowClaimMode.DISPATCH:
            predicate = "state='PLANNED' AND claim_token IS NULL AND dispatch_attempts < 3"
            parameters: dict[str, object] = {"limit": limit}
        elif claim_mode is WorkflowClaimMode.RECONCILE:
            predicate = (
                "(state='UNKNOWN' AND claim_token IS NULL AND dispatch_attempts < 3) OR "
                "(state IN ('PLANNED', 'UNKNOWN') AND claim_token IS NOT NULL "
                "AND claim_lease_until <= :now AND dispatch_attempts <= 3)"
            )
            parameters = {"limit": limit, "now": now}
        else:
            raise ValueError("unsupported workflow claim mode")
        rows = list(
            self.db.execute(
                text(
                    "SELECT * FROM agent.workflow_command WHERE " + predicate + " "
                    "ORDER BY created_at, id LIMIT :limit FOR UPDATE SKIP LOCKED"
                ),
                parameters,
            ).mappings()
        )
        claimed = []
        for row in rows:
            values: dict[str, object] = {"id": row["id"], "now": now}
            attempt_update = "dispatch_attempts"
            if claim_mode is not WorkflowClaimMode.RECONCILE or row["claim_token"] is None:
                attempt_update = "dispatch_attempts + 1"
            claim_update = (
                ", claim_owner=:claim_owner, claim_token=CAST(:claim_token AS UUID), "
                "claim_lease_until=:claim_lease_until, claim_mode=:claim_mode"
            )
            values.update(
                {
                    "claim_owner": claim_owner,
                    "claim_token": claim_token,
                    "claim_lease_until": claim_lease_until,
                    "claim_mode": claim_mode.value,
                }
            )
            updated = (
                self.db.execute(
                    text(
                        "UPDATE agent.workflow_command SET dispatch_attempts="
                        + attempt_update
                        + ", "
                        "updated_at=:now" + claim_update + " WHERE id=:id RETURNING *"
                    ),
                    values,
                )
                .mappings()
                .one()
            )
            claimed.append(self._command(updated))
        return tuple(claimed)

    def park_expired_workflow_claims(self, *, now: datetime) -> int:
        return int(
            self.db.execute(
                text(
                    "UPDATE agent.workflow_command SET state='UNKNOWN', receipt=NULL, "
                    "last_error_code='WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED', claim_owner=NULL, "
                    "claim_token=NULL, claim_lease_until=NULL, claim_mode=NULL, updated_at=:now "
                    "WHERE state='UNKNOWN' AND claim_token IS NULL AND dispatch_attempts >= 3 "
                    "AND last_error_code IS DISTINCT FROM 'WORKFLOW_CLAIM_ATTEMPTS_EXHAUSTED'"
                ),
                {"now": now},
            ).rowcount
        )

    def record_workflow_command_dispatch(
        self,
        command_id: str,
        *,
        expected_state: WorkflowCommandState,
        expected_claim_token: str | None,
        mutation: WorkflowDispatchMutation,
    ) -> WorkflowCommand | None:
        if expected_claim_token is None:
            raise ValueError("workflow completion claim token is required")
        row = (
            self.db.execute(
                text(
                    "UPDATE agent.workflow_command SET state=:state, receipt=CAST(:receipt AS JSONB), "
                    "last_error_code=:error_code, dispatched_at=:dispatched_at, updated_at=:now, "
                    "claim_owner=NULL, claim_token=NULL, claim_lease_until=NULL, claim_mode=NULL "
                    "WHERE id=CAST(:id AS UUID) AND state=:expected_state AND "
                    "claim_token=CAST(:claim_token AS UUID) RETURNING *"
                ),
                {
                    "id": command_id,
                    "expected_state": expected_state.value,
                    "claim_token": expected_claim_token,
                    "state": mutation.state.value,
                    "receipt": _json(mutation.receipt) if mutation.receipt is not None else None,
                    "error_code": mutation.error_code,
                    "dispatched_at": mutation.now
                    if mutation.state is WorkflowCommandState.DISPATCHED
                    else None,
                    "now": mutation.now,
                },
            )
            .mappings()
            .one_or_none()
        )
        return self._command(row) if row is not None else None

    def reserve_idempotency(self, record: AgentIdempotencyRecord) -> IdempotencyReservation:
        row = (
            self.db.execute(
                text(
                    "INSERT INTO agent.idempotency_key "
                    "(id, actor, operation, key, request_fingerprint, state, http_status, "
                    "result_metadata, sealed_response, created_at, updated_at, completed_at) VALUES "
                    "(:id, :actor, :operation, :key, :request_fingerprint, :state, :http_status, "
                    "CAST(:result_metadata AS JSONB), :sealed_response, :created_at, :updated_at, "
                    ":completed_at) ON CONFLICT (actor, operation, key) DO NOTHING RETURNING *"
                ),
                {
                    **record.model_dump(mode="json"),
                    "result_metadata": _json(record.result_metadata)
                    if record.result_metadata is not None
                    else None,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return IdempotencyReservation(record=self._idempotency(row), created=True)
        existing = self.idempotency_by_scope(
            record.actor, record.operation, record.key, for_update=True
        )
        if existing is None:
            raise RuntimeError("idempotency reservation conflict without an existing record")
        return IdempotencyReservation(record=existing, created=False)

    def idempotency_by_scope(
        self, actor: str, operation: str, idempotency_key: str, *, for_update: bool = False
    ) -> AgentIdempotencyRecord | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            self.db.execute(
                text(
                    "SELECT * FROM agent.idempotency_key WHERE actor=:actor "
                    "AND operation=:operation AND key=:key" + suffix
                ),
                {"actor": actor, "operation": operation, "key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        return self._idempotency(row) if row is not None else None

    def complete_idempotency(
        self,
        record_id: str,
        *,
        completion: IdempotencyCompletion,
    ) -> AgentIdempotencyRecord | None:
        row = (
            self.db.execute(
                text(
                    "UPDATE agent.idempotency_key SET state='COMPLETED', http_status=:http_status, "
                    "result_metadata=CAST(:result_metadata AS JSONB), sealed_response=:sealed_response, "
                    "updated_at=:now, completed_at=:now WHERE id=CAST(:id AS UUID) "
                    "AND state='IN_PROGRESS' RETURNING *"
                ),
                {
                    "id": record_id,
                    "http_status": completion.http_status,
                    "result_metadata": _json(completion.result_metadata),
                    "sealed_response": completion.sealed_response,
                    "now": completion.now,
                },
            )
            .mappings()
            .one_or_none()
        )
        return self._idempotency(row) if row is not None else None

    @staticmethod
    def _definition(row: Any) -> AgentDefinition:
        return AgentDefinition.model_validate(_dto_values(row))

    @staticmethod
    def _run(row: Any) -> AgentRun:
        values = _dto_values(row)
        try:
            source = {
                name: values.pop(name)
                for name in ("requirement_id", "work_item_id", "assignment_id")
            }
            values["business_context"] = (
                None
                if all(value is None for value in source.values())
                else AgentRunBusinessContext.model_validate(source)
            )
        except (ValueError, TypeError, KeyError):
            raise AgentQueryUnavailable("Agent Run business context unavailable") from None
        return AgentRun.model_validate(values)

    def _attempt(self, row: Any) -> AgentAttempt:
        values = _dto_values(row)
        checkpoint_id = values.pop("checkpoint_id", None)
        values["checkpoint"] = (
            self.checkpoint_by_id(checkpoint_id) if checkpoint_id is not None else None
        )
        return AgentAttempt.model_validate(values)

    @staticmethod
    def _binding(row: Any) -> ExecutionBinding:
        binding = ExecutionBinding.model_validate(dict(row["snapshot"]))
        if binding.source.value != row["source"] or binding.digest != row["digest"]:
            raise ValueError("execution binding evidence digest mismatch")
        return binding

    @staticmethod
    def _event(row: Any) -> CanonicalEventInput:
        values = _dto_values(row)
        return CanonicalEventInput.model_validate(
            {
                "id": values["event_id"],
                "event_type": values["event_type"],
                "attempt_id": values["attempt_id"],
                "generation": values["runner_generation"],
                "sequence": values["sequence"],
                "correlation_id": values["correlation_id"],
                "causation_id": values["causation_id"],
                "trace_id": values["trace_id"],
                "span_id": values["span_id"],
                "summary": values["summary"],
                "data": values["data"],
            }
        )

    @staticmethod
    def _checkpoint(row: Any) -> CheckpointInput:
        values = _dto_values(row)
        values.pop("attempt_id", None)
        values.pop("created_at", None)
        return CheckpointInput.model_validate(values)

    @staticmethod
    def _command(row: Any) -> WorkflowCommand:
        return WorkflowCommand.model_validate(_dto_values(row))

    @staticmethod
    def _idempotency(row: Any) -> AgentIdempotencyRecord:
        return AgentIdempotencyRecord.model_validate(_dto_values(row))


class SqlAlchemyAgentUnitOfWork:
    """Adapter-private SQL transaction that exposes only Agent platform operations."""

    def __init__(self, db: Connection) -> None:
        self._db = db
        self._repository = SqlAlchemyAgentRepository(db)

    def repository(self) -> SqlAlchemyAgentRepository:
        return self._repository

    def append_audit_event(self, event: AgentAuditAppend) -> None:
        self._db.execute(
            text(
                "SELECT audit.append_event(:id, :occurred_at, :actor, :actor_type, :action, "
                ":target_type, :target_id, :result, :reason, :correlation_id, :schema_version)"
            ),
            event.model_dump(mode="python"),
        )


class SqlAlchemyAgentTransactionRunner:
    """Runs a platform UnitOfWork atomically without leaking SQLAlchemy to callers."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self, operation: Callable[[SqlAlchemyAgentUnitOfWork], T]) -> T:
        with self._engine.begin() as db:
            return operation(SqlAlchemyAgentUnitOfWork(db))
