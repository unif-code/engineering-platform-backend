import hmac
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from control_plane.app.modules.agent_run.application.errors import (
    ActiveExecutionConflict,
    CapacityUnavailable,
    EnvironmentConflict,
    PolicyDisabled,
    PolicyLimitReached,
    PolicySnapshotConflict,
    SandboxApplicationError,
    StaleRunnerGeneration,
)
from control_plane.app.modules.agent_run.domain import (
    CanonicalDenial,
    DenialCode,
    EvidenceKind,
    EvidenceRef,
    ExecutionBindingProjection,
    MaterializationGuard,
    MaterializationState,
    MaterializationStatus,
    SandboxEnvironmentRef,
)
from control_plane.app.modules.agent_run.domain.policy import fencing_token_digest
from control_plane.app.modules.agent_run.ports.repository import (
    AdmissionPolicySnapshot,
    CleanupRecord,
    CommandOwnership,
    LockedMaterialization,
    PreviewIntent,
    ProvisionRecovery,
    ReservationRecord,
)
from control_plane.app.shared.idempotency import IdempotencyConflict

_ACTIVE_MATERIALIZATION_STATES = (
    "PROVISIONING",
    "READY",
    "RELEASING",
    "FINALIZING",
    "CANCELING",
    "QUARANTINED",
)
_ACTIVE_EXECUTION_CONSTRAINTS = frozenset(
    {
        "uq_agent_run_active_materialization",
        "uq_agent_run_materialization_execution_generation",
    }
)

__all__ = ["SqlAlchemySandboxRepository", "fencing_token_digest"]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class SqlAlchemySandboxRepository:
    def __init__(self, db: Connection) -> None:
        self._db = db

    def save_command_progress(
        self,
        command_id: str,
        *,
        owner_id: str,
        progress: dict[str, Any],
        now: datetime,
    ) -> None:
        updated = self._db.execute(
            text(
                "UPDATE agent_run.command_receipt SET progress=CAST(:progress AS JSONB), "
                "updated_at=:now WHERE id=:id AND owner_id=:owner_id AND state='IN_PROGRESS'"
            ),
            {"id": command_id, "owner_id": owner_id, "progress": _json(progress), "now": now},
        )
        if updated.rowcount != 1:
            raise IdempotencyConflict("command ownership is unavailable")

    def claim_idempotency(self, **values: Any) -> bool:
        owner_id = values.get("owner_id", values["id"])
        owner_expires_at = values.get(
            "owner_expires_at",
            values["now"] + timedelta(minutes=5),
        )
        inserted = self._db.execute(
            text(
                "INSERT INTO agent_run.command_receipt "
                "(id, actor, operation, idempotency_key, request_fingerprint, state, "
                "owner_id, owner_expires_at, phase, created_at, updated_at) VALUES "
                "(:id, :actor, :operation, :idempotency_key, :request_fingerprint, "
                "'IN_PROGRESS', :owner_id, :owner_expires_at, 'CLAIMED', :now, :now) "
                "ON CONFLICT (actor, operation, idempotency_key) DO NOTHING RETURNING id"
            ),
            {**values, "owner_id": owner_id, "owner_expires_at": owner_expires_at},
        ).scalar_one_or_none()
        return inserted is not None

    def acquire_command(self, **values: Any) -> CommandOwnership:
        inserted = self.claim_idempotency(
            id=values["command_id"],
            owner_id=values["owner_id"],
            actor=values["actor"],
            operation=values["operation"],
            idempotency_key=values["idempotency_key"],
            request_fingerprint=values["request_fingerprint"],
            owner_expires_at=values["owner_expires_at"],
            now=values["now"],
        )
        row = self.idempotency_by_scope(
            values["actor"],
            values["operation"],
            values["idempotency_key"],
            for_update=True,
        )
        if row is None:
            raise IdempotencyConflict("command claim is unavailable")
        if row["request_fingerprint"] != values["request_fingerprint"]:
            raise IdempotencyConflict("Idempotency-Key is bound to a different request")
        if inserted:
            return CommandOwnership(
                command_id=str(row["id"]),
                owner_id=str(row["owner_id"]),
                state=row["state"],
                phase=row["phase"],
                subject_id=(str(row["subject_id"]) if row["subject_id"] is not None else None),
                created=True,
                taken_over=False,
            )
        if row["state"] == "COMPLETED":
            return CommandOwnership(
                command_id=str(row["id"]),
                owner_id=None,
                state=row["state"],
                phase=row["phase"],
                subject_id=(str(row["subject_id"]) if row["subject_id"] is not None else None),
                created=False,
                taken_over=False,
            )
        if row["owner_expires_at"] > values["now"]:
            raise IdempotencyConflict("idempotent command has a live owner")
        updated = self._db.execute(
            text(
                "UPDATE agent_run.command_receipt SET owner_id=:owner_id, "
                "owner_expires_at=:owner_expires_at, updated_at=:now "
                "WHERE id=:id AND state='IN_PROGRESS' AND owner_expires_at <= :now"
            ),
            {
                "id": row["id"],
                "owner_id": values["owner_id"],
                "owner_expires_at": values["owner_expires_at"],
                "now": values["now"],
            },
        )
        if updated.rowcount != 1:
            raise IdempotencyConflict("idempotent command has a live owner")
        return CommandOwnership(
            command_id=str(row["id"]),
            owner_id=values["owner_id"],
            state=row["state"],
            phase=row["phase"],
            subject_id=(str(row["subject_id"]) if row["subject_id"] is not None else None),
            created=False,
            taken_over=True,
        )

    def advance_command(
        self,
        record_id: str,
        *,
        owner_id: str,
        phase: str,
        subject_id: str | None,
        now: datetime,
    ) -> None:
        updated = self._db.execute(
            text(
                "UPDATE agent_run.command_receipt SET phase=:phase, "
                "subject_id=COALESCE(subject_id, :subject_id), updated_at=:now "
                "WHERE id=:id AND state='IN_PROGRESS' AND owner_id=:owner_id "
                "AND (subject_id IS NULL OR subject_id=:subject_id OR :subject_id IS NULL)"
            ),
            {
                "id": record_id,
                "owner_id": owner_id,
                "phase": phase,
                "subject_id": subject_id,
                "now": now,
            },
        )
        if updated.rowcount != 1:
            raise IdempotencyConflict("command ownership is unavailable")

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
            self._db.execute(
                text(
                    "SELECT id, actor, operation, idempotency_key, request_fingerprint, "
                    "state, http_status, result_metadata, sealed_response, subject_id, "
                    "owner_id, owner_expires_at, phase, progress, "
                    "created_at, updated_at, completed_at "
                    "FROM agent_run.command_receipt "
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
        owner_id: str | None = None,
        http_status: int,
        result_metadata: dict[str, object],
        sealed_response: bytes,
        now: datetime,
    ) -> bool:
        owner_clause = "" if owner_id is None else " AND owner_id=:owner_id"
        result = self._db.execute(
            text(
                "UPDATE agent_run.command_receipt SET state='COMPLETED', "
                "http_status=:http_status, result_metadata=CAST(:result_metadata AS JSONB), "
                "sealed_response=:sealed_response, owner_id=NULL, owner_expires_at=NULL, "
                "phase='COMPLETED', completed_at=:now, updated_at=:now "
                f"WHERE id=:id AND state='IN_PROGRESS'{owner_clause}"
            ),
            {
                "id": record_id,
                "owner_id": owner_id,
                "http_status": http_status,
                "result_metadata": _json(result_metadata),
                "sealed_response": sealed_response,
                "now": now,
            },
        )
        return result.rowcount == 1

    def ensure_environment_and_capacity(
        self,
        environment: SandboxEnvironmentRef,
        policy: AdmissionPolicySnapshot,
        *,
        now: datetime,
    ) -> None:
        self._db.execute(
            text(
                "INSERT INTO agent_run.sandbox_environment "
                "(id, workspace_id, requirement_id, trust_tier, state, revision, "
                "created_at, updated_at) VALUES "
                "(:id, :workspace_id, :requirement_id, 'LAB_ONLY', 'ACTIVE', 1, :now, :now) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": environment.environment_id,
                "workspace_id": environment.workspace_id,
                "requirement_id": environment.requirement_id,
                "now": now,
            },
        )
        stored_environment = (
            self._db.execute(
                text(
                    "SELECT workspace_id, requirement_id, state "
                    "FROM agent_run.sandbox_environment WHERE id=:id"
                ),
                {"id": environment.environment_id},
            )
            .mappings()
            .one_or_none()
        )
        if stored_environment is None or (
            stored_environment["workspace_id"] != environment.workspace_id
            or stored_environment["requirement_id"] != environment.requirement_id
            or stored_environment["state"] != "ACTIVE"
        ):
            raise EnvironmentConflict

        self._db.execute(
            text(
                "INSERT INTO agent_run.capacity_ledger "
                "(environment_id, policy_version, policy_enabled, active_attempt_limit, "
                "maximum_units, active_attempts, active_units, revision, created_at, "
                "updated_at) VALUES "
                "(:environment_id, :policy_version, :enabled, :active_attempt_limit, "
                ":maximum_units, 0, 0, 1, :now, :now) "
                "ON CONFLICT (environment_id) DO NOTHING"
            ),
            {
                "environment_id": environment.environment_id,
                "policy_version": policy.policy_version,
                "enabled": policy.enabled,
                "active_attempt_limit": policy.active_attempt_limit,
                "maximum_units": policy.maximum_units,
                "now": now,
            },
        )
        ledger = (
            self._db.execute(
                text(
                    "SELECT policy_version, policy_enabled, active_attempt_limit, "
                    "maximum_units, active_attempts, active_units, revision "
                    "FROM agent_run.capacity_ledger WHERE environment_id=:environment_id "
                    "FOR UPDATE"
                ),
                {"environment_id": environment.environment_id},
            )
            .mappings()
            .one()
        )
        current_snapshot = (
            ledger["policy_version"],
            ledger["policy_enabled"],
            ledger["active_attempt_limit"],
            ledger["maximum_units"],
        )
        requested_snapshot = (
            policy.policy_version,
            policy.enabled,
            policy.active_attempt_limit,
            policy.maximum_units,
        )
        if current_snapshot != requested_snapshot:
            if ledger["active_attempts"] or ledger["active_units"]:
                raise PolicySnapshotConflict
            self._db.execute(
                text(
                    "UPDATE agent_run.capacity_ledger SET policy_version=:policy_version, "
                    "policy_enabled=:enabled, active_attempt_limit=:active_attempt_limit, "
                    "maximum_units=:maximum_units, revision=revision+1, updated_at=:now "
                    "WHERE environment_id=:environment_id"
                ),
                {
                    "environment_id": environment.environment_id,
                    "policy_version": policy.policy_version,
                    "enabled": policy.enabled,
                    "active_attempt_limit": policy.active_attempt_limit,
                    "maximum_units": policy.maximum_units,
                    "now": now,
                },
            )

    def reserve_materialization(
        self,
        *,
        binding: ExecutionBindingProjection,
        policy: AdmissionPolicySnapshot,
        materialization_id: str,
        lease_id: str,
        generation_id: str,
        fencing_token_digest: str,
        now: datetime,
    ) -> ReservationRecord:
        self._db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:execution_id, 0))"),
            {"execution_id": binding.execution.execution_id},
        )
        self.ensure_environment_and_capacity(binding.environment, policy, now=now)
        ledger = (
            self._db.execute(
                text(
                    "SELECT policy_enabled, active_attempt_limit, maximum_units, "
                    "active_attempts, active_units FROM agent_run.capacity_ledger "
                    "WHERE environment_id=:environment_id FOR UPDATE"
                ),
                {"environment_id": binding.environment.environment_id},
            )
            .mappings()
            .one()
        )
        if not ledger["policy_enabled"]:
            raise PolicyDisabled
        active = self._db.execute(
            text(
                "SELECT 1 FROM agent_run.sandbox_materialization "
                "WHERE execution_id=:execution_id AND state = ANY(:active_states) LIMIT 1"
            ),
            {
                "execution_id": binding.execution.execution_id,
                "active_states": list(_ACTIVE_MATERIALIZATION_STATES),
            },
        ).scalar_one_or_none()
        if active is not None:
            raise ActiveExecutionConflict
        if ledger["active_attempts"] >= ledger["active_attempt_limit"]:
            raise PolicyLimitReached
        if ledger["active_units"] + binding.resource_profile.unit_weight > ledger["maximum_units"]:
            raise CapacityUnavailable

        generation = self._db.execute(
            text(
                "SELECT COALESCE(max(generation), 0) + 1 "
                "FROM agent_run.sandbox_materialization WHERE execution_id=:execution_id"
            ),
            {"execution_id": binding.execution.execution_id},
        ).scalar_one()
        common = {
            "materialization_id": materialization_id,
            "environment_id": binding.environment.environment_id,
            "execution_id": binding.execution.execution_id,
            "generation": generation,
            "now": now,
        }
        try:
            with self._db.begin_nested():
                self._db.execute(
                    text(
                        "INSERT INTO agent_run.sandbox_materialization "
                        "(id, environment_id, execution_id, execution_kind, binding_digest, "
                        "deadline_at, state, revision, generation, resource_unit_weight, "
                        "runtime_profile, resource_profile, runner_manifest, boundary_manifest, "
                        "policy_versions, created_at, updated_at) VALUES "
                        "(:materialization_id, :environment_id, :execution_id, :execution_kind, "
                        ":binding_digest, :deadline_at, 'PROVISIONING', 1, :generation, "
                        ":unit_weight, CAST(:runtime_profile AS JSONB), "
                        "CAST(:resource_profile AS JSONB), CAST(:runner_manifest AS JSONB), "
                        "CAST(:boundary_manifest AS JSONB), CAST(:policy_versions AS JSONB), "
                        ":now, :now)"
                    ),
                    {
                        **common,
                        "execution_kind": binding.execution.kind.value,
                        "binding_digest": binding.binding_digest,
                        "deadline_at": binding.deadline_at,
                        "unit_weight": binding.resource_profile.unit_weight,
                        "runtime_profile": _json(binding.runtime_profile.model_dump(mode="json")),
                        "resource_profile": _json(binding.resource_profile.model_dump(mode="json")),
                        "runner_manifest": _json(binding.runner_manifest.model_dump(mode="json")),
                        "boundary_manifest": _json(binding.boundaries.model_dump(mode="json")),
                        "policy_versions": _json(
                            [
                                policy_ref.model_dump(mode="json")
                                for policy_ref in binding.policy_versions
                            ]
                        ),
                    },
                )
        except IntegrityError as error:
            constraint_name = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if constraint_name in _ACTIVE_EXECUTION_CONSTRAINTS:
                raise ActiveExecutionConflict from error
            raise
        self._db.execute(
            text(
                "INSERT INTO agent_run.capacity_lease "
                "(id, environment_id, materialization_id, execution_id, generation, "
                "unit_weight, state, expires_at, revision, acquired_at, updated_at) VALUES "
                "(:lease_id, :environment_id, :materialization_id, :execution_id, "
                ":generation, :unit_weight, 'ACTIVE', :expires_at, 1, :now, :now)"
            ),
            {
                **common,
                "lease_id": lease_id,
                "unit_weight": binding.resource_profile.unit_weight,
                "expires_at": now + timedelta(seconds=policy.lease_ttl_seconds),
            },
        )
        self._db.execute(
            text(
                "INSERT INTO agent_run.runner_generation "
                "(id, materialization_id, execution_id, generation, fencing_token_digest, "
                "binding_digest, protocol_version, deadline_at, state, revision, "
                "created_at, updated_at) VALUES "
                "(:generation_id, :materialization_id, :execution_id, :generation, "
                ":fencing_token_digest, :binding_digest, :protocol_version, :deadline_at, "
                "'ACTIVE', 1, :now, :now)"
            ),
            {
                **common,
                "generation_id": generation_id,
                "fencing_token_digest": fencing_token_digest,
                "binding_digest": binding.binding_digest,
                "protocol_version": binding.runner_manifest.protocol_version,
                "deadline_at": binding.deadline_at,
            },
        )
        self._db.execute(
            text(
                "UPDATE agent_run.capacity_ledger SET "
                "active_attempts=active_attempts+1, active_units=active_units+:unit_weight, "
                "revision=revision+1, updated_at=:now WHERE environment_id=:environment_id"
            ),
            {
                "environment_id": binding.environment.environment_id,
                "unit_weight": binding.resource_profile.unit_weight,
                "now": now,
            },
        )
        return ReservationRecord(
            materialization_id=materialization_id,
            environment_id=binding.environment.environment_id,
            execution_id=binding.execution.execution_id,
            lease_id=lease_id,
            generation=generation,
            revision=1,
            binding_digest=binding.binding_digest,
            deadline_at=binding.deadline_at,
        )

    def lock_current_guard(self, guard: MaterializationGuard) -> LockedMaterialization:
        row = (
            self._db.execute(
                text(
                    "SELECT m.id AS materialization_id, m.environment_id, m.execution_id, "
                    "m.state, m.revision, m.generation, m.binding_digest, m.deadline_at, "
                    "l.id AS lease_id, l.state AS lease_state, g.state AS generation_state, "
                    "g.fencing_token_digest FROM agent_run.sandbox_materialization m "
                    "JOIN agent_run.capacity_lease l ON l.materialization_id=m.id "
                    "JOIN agent_run.runner_generation g ON g.materialization_id=m.id "
                    "WHERE m.id=:materialization_id FOR UPDATE OF m, l, g"
                ),
                {"materialization_id": guard.materialization_id},
            )
            .mappings()
            .one_or_none()
        )
        actual_digest = fencing_token_digest(guard.fencing_token.get_secret_value())
        if (
            row is None
            or str(row["lease_id"]) != guard.lease_id
            or row["generation"] != guard.generation
            or row["revision"] != guard.expected_revision
            or row["lease_state"] != "ACTIVE"
            or row["generation_state"] != "ACTIVE"
            or not hmac.compare_digest(row["fencing_token_digest"], actual_digest)
        ):
            raise StaleRunnerGeneration
        return LockedMaterialization(
            materialization_id=str(row["materialization_id"]),
            environment_id=str(row["environment_id"]),
            execution_id=row["execution_id"],
            lease_id=str(row["lease_id"]),
            generation=row["generation"],
            revision=row["revision"],
            state=row["state"],
            binding_digest=row["binding_digest"],
            deadline_at=row["deadline_at"],
        )

    def bind_command_subject(
        self,
        record_id: str,
        materialization_id: str,
        *,
        owner_id: str,
        phase: str,
        recovery_capsule: bytes,
        now: datetime,
    ) -> None:
        capsule = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET recovery_capsule=:capsule, "
                "updated_at=:now WHERE id=:materialization_id "
                "AND state='PROVISIONING' AND recovery_capsule IS NULL"
            ),
            {
                "materialization_id": materialization_id,
                "capsule": recovery_capsule,
                "now": now,
            },
        )
        result = self._db.execute(
            text(
                "UPDATE agent_run.command_receipt SET subject_id=:materialization_id, "
                "phase=:phase, updated_at=:now WHERE id=:record_id "
                "AND state='IN_PROGRESS' AND owner_id=:owner_id AND subject_id IS NULL"
            ),
            {
                "record_id": record_id,
                "materialization_id": materialization_id,
                "owner_id": owner_id,
                "phase": phase,
                "now": now,
            },
        )
        if capsule.rowcount != 1 or result.rowcount != 1:
            raise StaleRunnerGeneration

    def provision_recovery(self, materialization_id: str) -> ProvisionRecovery:
        row = (
            self._db.execute(
                text(
                    "SELECT m.id AS materialization_id, m.environment_id, m.execution_id, "
                    "l.id AS lease_id, m.generation, m.revision, m.binding_digest, "
                    "m.deadline_at, m.recovery_capsule FROM agent_run.sandbox_materialization m "
                    "JOIN agent_run.capacity_lease l ON l.materialization_id=m.id "
                    "WHERE m.id=:materialization_id"
                ),
                {"materialization_id": materialization_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["recovery_capsule"] is None:
            raise StaleRunnerGeneration
        return ProvisionRecovery(
            reservation=ReservationRecord(
                materialization_id=str(row["materialization_id"]),
                environment_id=str(row["environment_id"]),
                execution_id=row["execution_id"],
                lease_id=str(row["lease_id"]),
                generation=row["generation"],
                revision=row["revision"],
                binding_digest=row["binding_digest"],
                deadline_at=row["deadline_at"],
            ),
            recovery_capsule=bytes(row["recovery_capsule"]),
        )

    def mark_materialization_ready(
        self,
        materialization_id: str,
        *,
        generation: int,
        now: datetime,
    ) -> ReservationRecord:
        row = (
            self._db.execute(
                text(
                    "SELECT m.id AS materialization_id, m.environment_id, m.execution_id, "
                    "m.revision, m.binding_digest, m.deadline_at, l.id AS lease_id "
                    "FROM agent_run.sandbox_materialization m "
                    "JOIN agent_run.capacity_lease l ON l.materialization_id=m.id "
                    "JOIN agent_run.runner_generation g ON g.materialization_id=m.id "
                    "WHERE m.id=:materialization_id AND m.generation=:generation "
                    "AND m.state='PROVISIONING' AND l.state='ACTIVE' AND g.state='ACTIVE' "
                    "FOR UPDATE OF m, l, g"
                ),
                {"materialization_id": materialization_id, "generation": generation},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise StaleRunnerGeneration
        revision = int(row["revision"]) + 1
        self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET state='READY', "
                "revision=:revision, updated_at=:now WHERE id=:materialization_id"
            ),
            {
                "materialization_id": materialization_id,
                "revision": revision,
                "now": now,
            },
        )
        self._db.execute(
            text(
                "UPDATE agent_run.runner_generation SET ready_at=:now, revision=revision+1, "
                "updated_at=:now WHERE materialization_id=:materialization_id "
                "AND state='ACTIVE'"
            ),
            {"materialization_id": materialization_id, "now": now},
        )
        return ReservationRecord(
            materialization_id=str(row["materialization_id"]),
            environment_id=str(row["environment_id"]),
            execution_id=row["execution_id"],
            lease_id=str(row["lease_id"]),
            generation=generation,
            revision=revision,
            binding_digest=row["binding_digest"],
            deadline_at=row["deadline_at"],
        )

    def materialization_status(self, materialization_id: str) -> MaterializationStatus:
        row = (
            self._db.execute(
                text(
                    "SELECT id, environment_id, execution_id, state, revision, generation, "
                    "binding_digest, deadline_at, denial_code, failure_dimension "
                    "FROM agent_run.sandbox_materialization WHERE id=:materialization_id"
                ),
                {"materialization_id": materialization_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise StaleRunnerGeneration
        evidence = tuple(
            EvidenceRef(
                kind=EvidenceKind(evidence_row["kind"]),
                artifact_id=evidence_row["artifact_id"],
                version=evidence_row["artifact_version"],
                sha256=evidence_row["content_sha256"],
                classification=evidence_row["classification"],
            )
            for evidence_row in self._db.execute(
                text(
                    "SELECT kind, artifact_id, artifact_version, content_sha256, "
                    "classification FROM agent_run.evidence_reference "
                    "WHERE materialization_id=:materialization_id ORDER BY sequence"
                ),
                {"materialization_id": materialization_id},
            ).mappings()
        )
        denial = (
            CanonicalDenial(
                code=DenialCode(row["denial_code"]),
                failure_dimension=row["failure_dimension"],
            )
            if row["denial_code"] is not None
            else None
        )
        return MaterializationStatus(
            materialization_id=str(row["id"]),
            environment_id=str(row["environment_id"]),
            execution_id=row["execution_id"],
            state=MaterializationState(row["state"]),
            revision=row["revision"],
            generation=row["generation"],
            binding_digest=row["binding_digest"],
            deadline_at=row["deadline_at"],
            evidence_refs=evidence,
            denial=denial,
        )

    def environment_for_materialization(self, materialization_id: str) -> str:
        environment_id = self._db.execute(
            text(
                "SELECT environment_id FROM agent_run.sandbox_materialization "
                "WHERE id=:materialization_id"
            ),
            {"materialization_id": materialization_id},
        ).scalar_one_or_none()
        if environment_id is None:
            raise StaleRunnerGeneration
        return str(environment_id)

    def environment_for_execution(self, execution_id: str) -> str:
        environment_id = self._db.execute(
            text(
                "SELECT environment_id FROM agent_run.sandbox_materialization "
                "WHERE execution_id=:execution_id "
                "ORDER BY (state = ANY(:active_states)) DESC, generation DESC LIMIT 1"
            ),
            {
                "execution_id": execution_id,
                "active_states": list(_ACTIVE_MATERIALIZATION_STATES),
            },
        ).scalar_one_or_none()
        if environment_id is None:
            raise StaleRunnerGeneration
        return str(environment_id)

    @staticmethod
    def _cleanup_record(row: Any) -> CleanupRecord:
        return CleanupRecord(
            materialization_id=str(row["materialization_id"]),
            environment_id=str(row["environment_id"]),
            execution_id=row["execution_id"],
            lease_id=str(row["lease_id"]),
            generation=row["generation"],
            revision=row["revision"],
            state=MaterializationState(row["state"]),
            deadline_at=row["deadline_at"],
            lease_expires_at=row["lease_expires_at"],
            cleanup_terminal_state=(
                MaterializationState(row["cleanup_terminal_state"])
                if row["cleanup_terminal_state"] is not None
                else None
            ),
            cancellation_reason=row["cancellation_reason"],
            evidence_persisted=row["evidence_persisted_at"] is not None,
            fenced=row["fenced_at"] is not None,
            secret_revoked=row["secret_revoked_at"] is not None,
            lease_released=row["lease_released_at"] is not None,
            destroyed=row["destroyed_at"] is not None,
        )

    @staticmethod
    def _cleanup_select() -> str:
        return (
            "SELECT m.id AS materialization_id, m.environment_id, m.execution_id, "
            "m.generation, m.revision, m.state, m.deadline_at, "
            "m.cleanup_terminal_state, m.cancellation_reason, "
            "m.evidence_persisted_at, m.fenced_at, "
            "m.secret_revoked_at, m.lease_released_at, m.destroyed_at, "
            "l.id AS lease_id, l.expires_at AS lease_expires_at "
            "FROM agent_run.sandbox_materialization m "
            "JOIN agent_run.capacity_lease l ON l.materialization_id=m.id "
        )

    def cleanup_record(self, materialization_id: str) -> CleanupRecord:
        row = (
            self._db.execute(
                text(self._cleanup_select() + "WHERE m.id=:materialization_id"),
                {"materialization_id": materialization_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise StaleRunnerGeneration
        return self._cleanup_record(row)

    def cleanup_candidates(
        self,
        *,
        environment_id: str,
        execution_id: str | None,
        observed_at: datetime,
    ) -> tuple[CleanupRecord, ...]:
        execution_filter = "" if execution_id is None else "AND m.execution_id=:execution_id "
        rows = self._db.execute(
            text(
                self._cleanup_select()
                + "WHERE m.environment_id=:environment_id "
                + execution_filter
                + "AND ("
                "m.state IN ('RELEASING','FINALIZING','CANCELING','QUARANTINED') OR ("
                "m.state='PROVISIONING' AND EXISTS (SELECT 1 "
                "FROM agent_run.command_receipt r WHERE r.subject_id=m.id "
                "AND r.operation='sandbox.provision' AND r.state='IN_PROGRESS' "
                "AND r.owner_expires_at <= :observed_at)) OR ("
                "m.state='READY' AND (m.deadline_at <= :observed_at "
                "OR l.expires_at <= :observed_at))) "
                "ORDER BY l.expires_at, m.id"
            ),
            {
                "environment_id": environment_id,
                "observed_at": observed_at,
                **({} if execution_id is None else {"execution_id": execution_id}),
            },
        ).mappings()
        return tuple(self._cleanup_record(row) for row in rows)

    def _insert_evidence(
        self,
        materialization_id: str,
        evidence: tuple[tuple[str, EvidenceRef], ...],
        *,
        now: datetime,
    ) -> None:
        next_sequence = self._db.execute(
            text(
                "SELECT COALESCE(max(sequence), 0) + 1 "
                "FROM agent_run.evidence_reference WHERE materialization_id=:materialization_id"
            ),
            {"materialization_id": materialization_id},
        ).scalar_one()
        for offset, (evidence_id, reference) in enumerate(evidence):
            self._db.execute(
                text(
                    "INSERT INTO agent_run.evidence_reference "
                    "(id, materialization_id, sequence, kind, artifact_id, artifact_version, "
                    "content_sha256, classification, created_at) VALUES "
                    "(:id, :materialization_id, :sequence, :kind, :artifact_id, "
                    ":artifact_version, :content_sha256, :classification, :now)"
                ),
                {
                    "id": evidence_id,
                    "materialization_id": materialization_id,
                    "sequence": next_sequence + offset,
                    "kind": reference.kind.value,
                    "artifact_id": reference.artifact_id,
                    "artifact_version": reference.version,
                    "content_sha256": reference.sha256,
                    "classification": reference.classification,
                    "now": now,
                },
            )

    def begin_preview_intent(
        self,
        *,
        command_id: str,
        owner_id: str,
        guard: MaterializationGuard,
        metadata: EvidenceRef,
        expires_at: datetime,
        now: datetime,
    ) -> PreviewIntent:
        current = self.lock_current_guard(guard)
        if current.state != "READY":
            raise StaleRunnerGeneration
        self._db.execute(
            text(
                "INSERT INTO agent_run.preview_intent "
                "(command_id, materialization_id, generation, metadata, expires_at, state, "
                "created_at, updated_at) VALUES (:command_id, :materialization_id, "
                ":generation, CAST(:metadata AS JSONB), :expires_at, 'INTENT', :now, :now)"
            ),
            {
                "command_id": command_id,
                "materialization_id": guard.materialization_id,
                "generation": guard.generation,
                "metadata": _json(metadata.model_dump(mode="json")),
                "expires_at": expires_at,
                "now": now,
            },
        )
        revision = current.revision + 1
        updated = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET revision=:revision, "
                "updated_at=:now WHERE id=:id AND state='READY' AND revision=:expected_revision"
            ),
            {
                "id": guard.materialization_id,
                "revision": revision,
                "expected_revision": current.revision,
                "now": now,
            },
        )
        if updated.rowcount != 1:
            raise StaleRunnerGeneration
        self.advance_command(
            command_id,
            owner_id=owner_id,
            phase="PREVIEW_INTENT",
            subject_id=guard.materialization_id,
            now=now,
        )
        return PreviewIntent(
            command_id=command_id,
            materialization_id=guard.materialization_id,
            generation=guard.generation,
            revision=revision,
            state="INTENT",
        )

    def complete_preview_intent(
        self,
        *,
        command_id: str,
        owner_id: str,
        evidence_id: str,
        metadata: EvidenceRef,
        result_capsule: bytes,
        now: datetime,
    ) -> PreviewIntent:
        row = (
            self._db.execute(
                text(
                    "SELECT p.materialization_id, p.generation, p.state, m.revision, m.state "
                    "AS materialization_state FROM agent_run.preview_intent p "
                    "JOIN agent_run.sandbox_materialization m ON m.id=p.materialization_id "
                    "JOIN agent_run.command_receipt r ON r.id=p.command_id "
                    "WHERE p.command_id=:command_id AND r.owner_id=:owner_id "
                    "AND r.state='IN_PROGRESS' FOR UPDATE OF p, m, r"
                ),
                {"command_id": command_id, "owner_id": owner_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["state"] != "INTENT" or row["materialization_state"] != "READY":
            raise StaleRunnerGeneration
        materialization_id = str(row["materialization_id"])
        self._insert_evidence(
            materialization_id,
            ((evidence_id, metadata),),
            now=now,
        )
        updated = self._db.execute(
            text(
                "UPDATE agent_run.preview_intent SET state='PUBLISHED', "
                "result_capsule=:result_capsule, completed_at=:now, updated_at=:now "
                "WHERE command_id=:command_id AND state='INTENT'"
            ),
            {
                "command_id": command_id,
                "result_capsule": result_capsule,
                "now": now,
            },
        )
        if updated.rowcount != 1:
            raise StaleRunnerGeneration
        self.advance_command(
            command_id,
            owner_id=owner_id,
            phase="PREVIEW_PUBLISHED",
            subject_id=materialization_id,
            now=now,
        )
        return PreviewIntent(
            command_id=command_id,
            materialization_id=materialization_id,
            generation=row["generation"],
            revision=row["revision"],
            state="PUBLISHED",
        )

    def begin_cleanup(
        self,
        guard: MaterializationGuard,
        *,
        evidence: tuple[tuple[str, EvidenceRef], ...],
        transition_state: str,
        terminal_state: str,
        cancellation_reason: str | None,
        now: datetime,
    ) -> int:
        expected_transition = {
            "RELEASED": "RELEASING",
            "FINALIZED": "FINALIZING",
            "CANCELED": "CANCELING",
            "TIMED_OUT": "CANCELING",
        }.get(terminal_state)
        if expected_transition is None or transition_state != expected_transition:
            raise ValueError("cleanup transition state is invalid")
        current = self.lock_current_guard(guard)
        if current.state != "READY":
            raise StaleRunnerGeneration
        self._insert_evidence(guard.materialization_id, evidence, now=now)
        revision = current.revision + 1
        updated = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET state=:state, "
                "cleanup_terminal_state=:terminal_state, "
                "cancellation_reason=:cancellation_reason, "
                "revision=:revision, updated_at=:now "
                "WHERE id=:materialization_id AND state='READY' AND revision=:expected_revision"
            ),
            {
                "materialization_id": guard.materialization_id,
                "state": transition_state,
                "terminal_state": terminal_state,
                "cancellation_reason": cancellation_reason,
                "now": now,
                "revision": revision,
                "expected_revision": current.revision,
            },
        )
        if updated.rowcount != 1:
            raise StaleRunnerGeneration
        return revision

    def begin_cancel_cleanup(
        self,
        *,
        execution_id: str,
        authorized_environment_id: str,
        terminal_state: str,
        cancellation_reason: str,
        command_id: str,
        owner_id: str,
        now: datetime,
    ) -> CleanupRecord:
        self._db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:execution_id, 0))"),
            {"execution_id": execution_id},
        )
        row = (
            self._db.execute(
                text(
                    self._cleanup_select() + "WHERE m.execution_id=:execution_id "
                    "ORDER BY (m.state = ANY(:active_states)) DESC, m.generation DESC "
                    "LIMIT 1 FOR UPDATE OF m, l"
                ),
                {
                    "execution_id": execution_id,
                    "active_states": list(_ACTIVE_MATERIALIZATION_STATES),
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise StaleRunnerGeneration
        current = self._cleanup_record(row)
        if current.environment_id != authorized_environment_id:
            raise SandboxApplicationError(DenialCode.RUNTIME_CAPABILITY_DENIED, "workload_scope")
        if current.state is MaterializationState.READY:
            updated = self._db.execute(
                text(
                    "UPDATE agent_run.sandbox_materialization SET state='CANCELING', "
                    "cleanup_terminal_state=:terminal_state, "
                    "cancellation_reason=:cancellation_reason, revision=revision+1, "
                    "updated_at=:now WHERE id=:id AND state='READY' AND revision=:revision"
                ),
                {
                    "id": current.materialization_id,
                    "revision": current.revision,
                    "terminal_state": terminal_state,
                    "cancellation_reason": cancellation_reason,
                    "now": now,
                },
            )
            if updated.rowcount != 1:
                raise StaleRunnerGeneration
        elif current.state not in {
            MaterializationState.RELEASED,
            MaterializationState.FINALIZED,
            MaterializationState.CANCELED,
            MaterializationState.TIMED_OUT,
            MaterializationState.FAILED,
        }:
            raise ActiveExecutionConflict
        self.advance_command(
            command_id,
            owner_id=owner_id,
            phase="CLEANUP_STARTED",
            subject_id=current.materialization_id,
            now=now,
        )
        return self.cleanup_record(current.materialization_id)

    def begin_provision_cleanup(
        self,
        materialization_id: str,
        *,
        denial_code: str,
        failure_dimension: str,
        now: datetime,
    ) -> CleanupRecord:
        updated = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET state='QUARANTINED', "
                "cleanup_terminal_state='FAILED', denial_code=:denial_code, "
                "failure_dimension=:failure_dimension, revision=revision+1, updated_at=:now "
                "WHERE id=:id AND state='PROVISIONING' RETURNING id"
            ),
            {
                "id": materialization_id,
                "denial_code": denial_code,
                "failure_dimension": failure_dimension,
                "now": now,
            },
        ).scalar_one_or_none()
        if updated is None:
            raise StaleRunnerGeneration
        return self.cleanup_record(materialization_id)

    def mark_evidence_persisted(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> int:
        revision = expected_revision + 1
        updated = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET evidence_persisted_at=:now, "
                "revision=:revision, updated_at=:now WHERE id=:materialization_id "
                "AND state IN ('RELEASING','FINALIZING','CANCELING','QUARANTINED') "
                "AND revision=:expected_revision AND cleanup_terminal_state IS NOT NULL "
                "AND evidence_persisted_at IS NULL"
            ),
            {
                "materialization_id": materialization_id,
                "expected_revision": expected_revision,
                "revision": revision,
                "now": now,
            },
        )
        if updated.rowcount != 1:
            raise StaleRunnerGeneration
        return revision

    def mark_fenced(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> int:
        revision = expected_revision + 1
        updated = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET fenced_at=:now, "
                "revision=:revision, updated_at=:now WHERE id=:materialization_id "
                "AND state IN ('RELEASING','FINALIZING','CANCELING','QUARANTINED') "
                "AND revision=:expected_revision AND evidence_persisted_at IS NOT NULL "
                "AND fenced_at IS NULL"
            ),
            {
                "materialization_id": materialization_id,
                "expected_revision": expected_revision,
                "revision": revision,
                "now": now,
            },
        )
        generation = self._db.execute(
            text(
                "UPDATE agent_run.runner_generation SET state='FENCED', fenced_at=:now, "
                "revision=revision+1, updated_at=:now WHERE materialization_id=:materialization_id "
                "AND state='ACTIVE'"
            ),
            {"materialization_id": materialization_id, "now": now},
        )
        if updated.rowcount != 1 or generation.rowcount != 1:
            raise StaleRunnerGeneration
        return revision

    def mark_secret_revoked(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> int:
        revision = expected_revision + 1
        updated = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET secret_revoked_at=:now, "
                "revision=:revision, updated_at=:now WHERE id=:materialization_id "
                "AND revision=:expected_revision AND fenced_at IS NOT NULL "
                "AND secret_revoked_at IS NULL"
            ),
            {
                "materialization_id": materialization_id,
                "expected_revision": expected_revision,
                "revision": revision,
                "now": now,
            },
        )
        if updated.rowcount != 1:
            raise StaleRunnerGeneration
        return revision

    def release_capacity(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        now: datetime,
    ) -> int:
        row = (
            self._db.execute(
                text(
                    "SELECT m.environment_id, m.resource_unit_weight FROM "
                    "agent_run.sandbox_materialization m JOIN agent_run.capacity_lease l "
                    "ON l.materialization_id=m.id WHERE m.id=:materialization_id "
                    "AND m.revision=:expected_revision AND m.secret_revoked_at IS NOT NULL "
                    "AND m.lease_released_at IS NULL AND l.state='ACTIVE' FOR UPDATE OF m, l"
                ),
                {
                    "materialization_id": materialization_id,
                    "expected_revision": expected_revision,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise StaleRunnerGeneration
        revision = expected_revision + 1
        self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET lease_released_at=:now, "
                "revision=:revision, updated_at=:now WHERE id=:materialization_id"
            ),
            {
                "materialization_id": materialization_id,
                "revision": revision,
                "now": now,
            },
        )
        self._db.execute(
            text(
                "UPDATE agent_run.capacity_lease SET state='RELEASED', released_at=:now, "
                "revision=revision+1, updated_at=:now WHERE materialization_id=:materialization_id"
            ),
            {"materialization_id": materialization_id, "now": now},
        )
        self._db.execute(
            text(
                "UPDATE agent_run.runner_generation SET state='RELEASED', released_at=:now, "
                "revision=revision+1, updated_at=:now WHERE materialization_id=:materialization_id "
                "AND state='FENCED'"
            ),
            {"materialization_id": materialization_id, "now": now},
        )
        ledger = self._db.execute(
            text(
                "UPDATE agent_run.capacity_ledger SET active_attempts=active_attempts-1, "
                "active_units=active_units-:unit_weight, revision=revision+1, updated_at=:now "
                "WHERE environment_id=:environment_id AND active_attempts >= 1 "
                "AND active_units >= :unit_weight"
            ),
            {
                "environment_id": row["environment_id"],
                "unit_weight": row["resource_unit_weight"],
                "now": now,
            },
        )
        if ledger.rowcount != 1:
            raise StaleRunnerGeneration
        return revision

    def complete_cleanup(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        terminal_state: str,
        now: datetime,
    ) -> int:
        expected_transition = {
            "RELEASED": "RELEASING",
            "FINALIZED": "FINALIZING",
            "CANCELED": "CANCELING",
            "TIMED_OUT": "CANCELING",
            "FAILED": "PROVISIONING",
        }.get(terminal_state)
        if expected_transition is None:
            raise ValueError("cleanup terminal state is invalid")
        revision = expected_revision + 1
        updated = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET state=:terminal_state, "
                "destroyed_at=:now, terminal_at=:now, denial_code=NULL, "
                "failure_dimension=NULL, revision=:revision, updated_at=:now "
                "WHERE id=:materialization_id "
                "AND state IN (:expected_transition, 'QUARANTINED') "
                "AND cleanup_terminal_state=:terminal_state "
                "AND revision=:expected_revision AND lease_released_at IS NOT NULL "
                "AND destroyed_at IS NULL RETURNING revision"
            ),
            {
                "materialization_id": materialization_id,
                "terminal_state": terminal_state,
                "expected_transition": expected_transition,
                "expected_revision": expected_revision,
                "revision": revision,
                "now": now,
            },
        ).scalar_one_or_none()
        if updated is None:
            raise StaleRunnerGeneration
        return int(updated)

    def quarantine_cleanup(
        self,
        materialization_id: str,
        *,
        expected_revision: int,
        denial_code: str,
        failure_dimension: str,
        now: datetime,
    ) -> int:
        revision = expected_revision + 1
        updated = self._db.execute(
            text(
                "UPDATE agent_run.sandbox_materialization SET state='QUARANTINED', "
                "denial_code=:denial_code, failure_dimension=:failure_dimension, "
                "revision=:revision, updated_at=:now WHERE id=:materialization_id "
                "AND state IN ('RELEASING','FINALIZING','CANCELING','QUARANTINED') "
                "AND revision=:expected_revision AND cleanup_terminal_state IS NOT NULL "
                "RETURNING revision"
            ),
            {
                "materialization_id": materialization_id,
                "expected_revision": expected_revision,
                "denial_code": denial_code,
                "failure_dimension": failure_dimension,
                "revision": revision,
                "now": now,
            },
        ).scalar_one_or_none()
        if updated is None:
            raise StaleRunnerGeneration
        return int(updated)

    def record_reconciliation(
        self,
        *,
        reconciliation_id: str,
        environment_id: str,
        execution_id: str | None,
        actor: str,
        correlation_id: str,
        observed_at: datetime,
        scanned_count: int,
        reconciled_count: int,
        now: datetime,
    ) -> bool:
        inserted = self._db.execute(
            text(
                "INSERT INTO agent_run.reconciliation_run "
                "(id, environment_id, execution_id, actor, correlation_id, observed_at, "
                "state, scanned_count, reconciled_count, created_at, completed_at, updated_at) "
                "SELECT :id, id, :execution_id, :actor, :correlation_id, :observed_at, "
                "'COMPLETED', :scanned_count, :reconciled_count, :now, :now, :now "
                "FROM agent_run.sandbox_environment WHERE id=:environment_id "
                "RETURNING id"
            ),
            {
                "id": reconciliation_id,
                "environment_id": environment_id,
                "execution_id": execution_id,
                "actor": actor,
                "correlation_id": correlation_id,
                "observed_at": observed_at,
                "scanned_count": scanned_count,
                "reconciled_count": reconciled_count,
                "now": now,
            },
        ).scalar_one_or_none()
        return inserted is not None
