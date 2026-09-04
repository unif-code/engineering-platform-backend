import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from cryptography.exceptions import InvalidTag
from pydantic import SecretStr, TypeAdapter, ValidationError
from sqlalchemy import Connection, Engine

from control_plane.app.modules.agent_run.application.errors import (
    SandboxAdmissionError,
    SandboxApplicationError,
    StaleRunnerGeneration,
)
from control_plane.app.modules.agent_run.domain import (
    CancelExecutionCommand,
    CancellationReason,
    CancellationReceipt,
    CanonicalDenial,
    CheckpointAndReleaseCommand,
    CommandContext,
    DenialCode,
    EvidenceRef,
    FinalizeExecutionCommand,
    GetMaterializationStatusQuery,
    HandoffResult,
    HandoffToChildCommand,
    LeaseReconciliationItem,
    LifecycleReceipt,
    MaterializationBlocked,
    MaterializationFailed,
    MaterializationGuard,
    MaterializationHandle,
    MaterializationReady,
    MaterializationState,
    MaterializationStatus,
    PreviewPublished,
    PreviewResult,
    ProvisionMaterializationCommand,
    ProvisionResult,
    PublishPreviewCommand,
    ReconcileLeaseCommand,
    ReconciliationReceipt,
    ReleaseReceipt,
    SandboxDenied,
)
from control_plane.app.modules.agent_run.domain.policy import (
    SandboxPolicyViolation,
    fencing_token_digest,
    validate_binding_for_provision,
)
from control_plane.app.modules.agent_run.ports.repository import (
    CleanupRecord,
    ReservationRecord,
    SandboxRepository,
)
from control_plane.app.modules.agent_run.ports.runtime import (
    RuntimeMaterializationError,
    RuntimeMaterializationRequest,
    RuntimeMaterializerPort,
    RuntimePresence,
)
from control_plane.app.modules.agent_run.ports.services import (
    AdmissionPolicyPort,
    ClockPort,
    DefaultDenyWorkloadAuthorization,
    RandomPort,
    WorkloadAuthorizationPort,
)
from control_plane.app.modules.audit import AuditEnvelope, TransactionalAuditAppender
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotencyReplayUnavailable,
    IdempotentResponse,
    SealedIdempotentEnvelope,
    canonical_request_fingerprint,
)
from control_plane.app.shared.security import seal, unseal

RepositoryFactory = Callable[[Connection], SandboxRepository]
_PROVISION_RESULT: TypeAdapter[ProvisionResult] = TypeAdapter(ProvisionResult)
_PREVIEW_RESULT: TypeAdapter[PreviewResult] = TypeAdapter(PreviewResult)
_LIFECYCLE_RESULT: TypeAdapter[LifecycleReceipt] = TypeAdapter(LifecycleReceipt)
_RECONCILIATION_RESULT: TypeAdapter[ReconciliationReceipt] = TypeAdapter(ReconciliationReceipt)
_REPLAY_METADATA = {"kind": "http-response", "schemaVersion": 1}
_PROVISION_OPERATION = "sandbox.provision"
_SANDBOX_PATH = "/api/v1/internal/sandbox"
_PROVISION_PATH = f"{_SANDBOX_PATH}/materializations"
_COMMAND_OWNER_TTL = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class SandboxDependencies:
    engine: Engine
    repository_factory: RepositoryFactory
    runtime: RuntimeMaterializerPort
    admission: AdmissionPolicyPort
    audit: TransactionalAuditAppender
    clock: ClockPort
    random: RandomPort
    idempotency_sealing_key: bytes = field(repr=False)
    authorization: WorkloadAuthorizationPort = field(
        default_factory=DefaultDenyWorkloadAuthorization
    )


@dataclass(frozen=True, slots=True)
class _OwnedCommand:
    fingerprint: str
    command_id: str
    owner_id: str
    phase: str
    subject_id: str | None
    created: bool
    replay: IdempotentResponse | None
    progress: dict[str, Any]


class SandboxController:
    def __init__(self, dependencies: SandboxDependencies) -> None:
        if len(dependencies.idempotency_sealing_key) != 32:
            raise ValueError("idempotency sealing key must be 32 bytes")
        self._dependencies = dependencies

    def _fingerprint(self, command: ProvisionMaterializationCommand) -> str:
        return canonical_request_fingerprint(
            operation=_PROVISION_OPERATION,
            method="POST",
            path=_PROVISION_PATH,
            body=cast(Mapping[str, object], command.binding.model_dump(mode="json")),
            idempotency_sealing_key=self._dependencies.idempotency_sealing_key,
        )

    @staticmethod
    def _denial(error: SandboxApplicationError | SandboxPolicyViolation) -> CanonicalDenial:
        return CanonicalDenial(
            code=error.code,
            failure_dimension=error.failure_dimension,
            retryable=getattr(error, "retryable", False),
        )

    @staticmethod
    def _result_response(result: ProvisionResult) -> IdempotentResponse:
        body = result.model_dump(mode="json")
        if isinstance(result, MaterializationReady):
            body["handle"]["fencing_token"] = result.handle.fencing_token.get_secret_value()
            status = 201
        elif isinstance(result, MaterializationBlocked):
            status = 409
        else:
            status = 503
        return IdempotentResponse(status_code=status, body=body)

    def _replay_response(self, row: Mapping[str, Any]) -> IdempotentResponse:
        if row["state"] != "COMPLETED" or row["result_metadata"] != _REPLAY_METADATA:
            raise IdempotencyConflict("idempotent command is still in progress")
        try:
            plaintext = unseal(
                row["sealed_response"],
                self._dependencies.idempotency_sealing_key,
            )
            envelope = SealedIdempotentEnvelope.model_validate_json(plaintext)
        except (InvalidTag, ValueError, UnicodeError, ValidationError):
            raise IdempotencyReplayUnavailable("idempotent response is unavailable") from None
        if (
            envelope.actor != row["actor"]
            or envelope.operation != row["operation"]
            or envelope.idempotency_key != row["idempotency_key"]
            or envelope.request_fingerprint != row["request_fingerprint"]
            or envelope.response.status_code != row["http_status"]
        ):
            raise IdempotencyReplayUnavailable("idempotent response is unavailable")
        return envelope.response

    def _replay(self, row: Mapping[str, Any]) -> ProvisionResult:
        return _PROVISION_RESULT.validate_python(self._replay_response(row).body)

    def _complete_response(
        self,
        repository: SandboxRepository,
        row: Mapping[str, Any],
        response: IdempotentResponse,
        *,
        owner_id: str | None = None,
    ) -> None:
        envelope = SealedIdempotentEnvelope(
            actor=row["actor"],
            operation=row["operation"],
            idempotency_key=row["idempotency_key"],
            request_fingerprint=row["request_fingerprint"],
            response=response,
        )
        ciphertext = seal(
            envelope.model_dump_json(by_alias=True).encode("utf-8"),
            self._dependencies.idempotency_sealing_key,
        )
        if not repository.complete_idempotency(
            str(row["id"]),
            owner_id=owner_id,
            http_status=response.status_code,
            result_metadata=_REPLAY_METADATA,
            sealed_response=ciphertext,
            now=self._dependencies.clock.now(),
        ):
            raise IdempotencyReplayUnavailable("idempotency completion is unavailable")

    def _complete_receipt(
        self,
        repository: SandboxRepository,
        row: Mapping[str, Any],
        result: ProvisionResult,
        *,
        owner_id: str | None = None,
    ) -> None:
        response = self._result_response(result)
        self._complete_response(repository, row, response, owner_id=owner_id)

    def _seal_recovery(
        self,
        reservation: ReservationRecord,
        token: str,
    ) -> bytes:
        plaintext = json.dumps(
            {
                "materializationId": reservation.materialization_id,
                "leaseId": reservation.lease_id,
                "executionId": reservation.execution_id,
                "fencingToken": token,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return seal(plaintext, self._dependencies.idempotency_sealing_key)

    def _unseal_recovery(
        self,
        reservation: ReservationRecord,
        capsule: bytes,
    ) -> str:
        try:
            payload = json.loads(
                unseal(capsule, self._dependencies.idempotency_sealing_key).decode("utf-8")
            )
        except (InvalidTag, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdempotencyReplayUnavailable("provision recovery is unavailable") from None
        if not isinstance(payload, dict) or (
            payload.get("materializationId") != reservation.materialization_id
            or payload.get("leaseId") != reservation.lease_id
            or payload.get("executionId") != reservation.execution_id
            or not isinstance(payload.get("fencingToken"), str)
        ):
            raise IdempotencyReplayUnavailable("provision recovery is unavailable")
        return cast(str, payload["fencingToken"])

    def _audit_context(
        self,
        db: Connection,
        context: CommandContext,
        *,
        action: str,
        target_id: str,
        result: str,
        reason: str | None,
    ) -> None:
        self._audit_actor(
            db,
            actor=context.actor,
            correlation_id=context.correlation_id,
            request_id=context.request_id,
            action=action,
            target_id=target_id,
            result=result,
            reason=reason,
        )

    def _audit_actor(
        self,
        db: Connection,
        *,
        actor: str,
        correlation_id: str,
        request_id: str | None,
        action: str,
        target_id: str,
        result: str,
        reason: str | None,
    ) -> None:
        self._dependencies.audit.append_in_transaction(
            db,
            AuditEnvelope(
                id=str(self._dependencies.random.uuid4()),
                occurred_at=self._dependencies.clock.now(),
                actor=actor,
                actor_type="WORKLOAD",
                action=action,
                target_type="SandboxMaterialization",
                target_id=target_id,
                result=result,
                reason=reason,
                correlation_id=correlation_id,
                request_id=request_id,
            ),
        )

    def _authorize(
        self,
        *,
        actor: str,
        operation: str,
        environment_id: str,
        action: str,
        target_id: str,
        correlation_id: str,
        request_id: str | None,
    ) -> None:
        if self._dependencies.authorization.authorize(
            actor=actor,
            operation=operation,
            environment_id=environment_id,
        ):
            return
        error = SandboxApplicationError(
            DenialCode.RUNTIME_CAPABILITY_DENIED,
            "workload_scope",
        )
        with self._dependencies.engine.begin() as db:
            self._audit_actor(
                db,
                actor=actor,
                correlation_id=correlation_id,
                request_id=request_id,
                action=action,
                target_id=target_id,
                result="DENIED",
                reason=f"{error.code.value}:{error.failure_dimension}",
            )
        raise error

    def _authorize_context(
        self,
        context: CommandContext,
        *,
        operation: str,
        environment_id: str | None = None,
        action: str,
        target_id: str,
        execution_scope: bool = False,
    ) -> str:
        if environment_id is None:
            try:
                environment_id = (
                    self._environment_for_execution(target_id)
                    if execution_scope
                    else self._environment_for_materialization(target_id)
                )
            except SandboxApplicationError as error:
                with self._dependencies.engine.begin() as db:
                    self._audit_context(
                        db,
                        context,
                        action=action,
                        target_id=target_id,
                        result="DENIED",
                        reason=f"{error.code.value}:{error.failure_dimension}",
                    )
                raise
        self._authorize(
            actor=context.actor,
            operation=operation,
            environment_id=environment_id,
            action=action,
            target_id=target_id,
            correlation_id=context.correlation_id,
            request_id=context.request_id,
        )
        return environment_id

    def _environment_for_materialization(self, materialization_id: str) -> str:
        with self._dependencies.engine.connect() as db:
            return self._dependencies.repository_factory(db).environment_for_materialization(
                materialization_id
            )

    def _environment_for_execution(self, execution_id: str) -> str:
        with self._dependencies.engine.connect() as db:
            return self._dependencies.repository_factory(db).environment_for_execution(execution_id)

    @contextmanager
    def _command_acquisition(
        self, context: CommandContext, *, action: str, target_id: str
    ) -> Iterator[Connection]:
        try:
            with self._dependencies.engine.begin() as db:
                yield db
        except (IdempotencyConflict, SandboxApplicationError) as error:
            with self._dependencies.engine.begin() as db:
                self._audit_context(
                    db,
                    context,
                    action=action,
                    target_id=target_id,
                    result="DENIED",
                    reason=(
                        "IDEMPOTENCY_CONFLICT:command_acquisition"
                        if isinstance(error, IdempotencyConflict)
                        else f"{error.code.value}:{error.failure_dimension}"
                    ),
                )
            raise

    def _claim_operation(
        self,
        context: CommandContext,
        *,
        operation: str,
        action: str,
        target_id: str,
        path: str,
        body: Mapping[str, object],
        authorize_bound_subject: bool = False,
    ) -> _OwnedCommand:
        fingerprint = canonical_request_fingerprint(
            operation=operation,
            method="POST",
            path=path,
            body=body,
            idempotency_sealing_key=self._dependencies.idempotency_sealing_key,
        )
        with self._command_acquisition(context, action=action, target_id=target_id) as db:
            repository = self._dependencies.repository_factory(db)
            now = self._dependencies.clock.now()
            owner_id = str(self._dependencies.random.uuid4())
            ownership = repository.acquire_command(
                command_id=str(self._dependencies.random.uuid4()),
                owner_id=owner_id,
                actor=context.actor,
                operation=operation,
                idempotency_key=context.idempotency_key,
                request_fingerprint=fingerprint,
                owner_expires_at=now + _COMMAND_OWNER_TTL,
                now=now,
            )
            row = repository.idempotency_by_scope(
                context.actor,
                operation,
                context.idempotency_key,
                for_update=True,
            )
            if row is None:
                raise IdempotencyReplayUnavailable("idempotency claim is unavailable")
            if row["request_fingerprint"] != fingerprint:
                raise IdempotencyConflict("Idempotency-Key is bound to a different request")
            if authorize_bound_subject and ownership.subject_id is not None:
                environment_id = repository.environment_for_materialization(ownership.subject_id)
                if not self._dependencies.authorization.authorize(
                    actor=context.actor,
                    operation=operation,
                    environment_id=environment_id,
                ):
                    raise SandboxApplicationError(
                        DenialCode.RUNTIME_CAPABILITY_DENIED, "workload_scope"
                    )
            return _OwnedCommand(
                fingerprint=fingerprint,
                command_id=ownership.command_id,
                owner_id=owner_id,
                phase=ownership.phase,
                subject_id=ownership.subject_id,
                created=ownership.created,
                replay=(self._replay_response(row) if row["state"] == "COMPLETED" else None),
                progress=dict(row["progress"]),
            )

    def _operation_row(
        self,
        repository: SandboxRepository,
        context: CommandContext,
        operation: str,
        fingerprint: str,
        owner_id: str | None = None,
    ) -> Mapping[str, Any]:
        row = repository.idempotency_by_scope(
            context.actor,
            operation,
            context.idempotency_key,
            for_update=True,
        )
        if (
            row is None
            or row["request_fingerprint"] != fingerprint
            or (owner_id is not None and str(row["owner_id"]) != owner_id)
        ):
            raise IdempotencyReplayUnavailable("idempotency claim is unavailable")
        return cast(Mapping[str, Any], row)

    @staticmethod
    def _guard_body(guard: MaterializationGuard) -> dict[str, object]:
        return {
            "materializationId": guard.materialization_id,
            "leaseId": guard.lease_id,
            "generation": guard.generation,
            "fencingToken": guard.fencing_token.get_secret_value(),
            "expectedRevision": guard.expected_revision,
        }

    def _audit(
        self,
        db: Connection,
        command: ProvisionMaterializationCommand,
        *,
        target_type: str,
        target_id: str,
        result: str,
        reason: str | None,
    ) -> None:
        self._dependencies.audit.append_in_transaction(
            db,
            AuditEnvelope(
                id=str(self._dependencies.random.uuid4()),
                occurred_at=self._dependencies.clock.now(),
                actor=command.context.actor,
                actor_type="WORKLOAD",
                action="sandbox.materialization.provision",
                target_type=target_type,
                target_id=target_id,
                result=result,
                reason=reason,
                correlation_id=command.context.correlation_id,
                request_id=command.context.request_id,
            ),
        )

    def _blocked(
        self,
        command: ProvisionMaterializationCommand,
        error: SandboxApplicationError | SandboxPolicyViolation,
    ) -> MaterializationBlocked:
        return MaterializationBlocked(
            execution=command.binding.execution,
            binding_digest=command.binding.binding_digest,
            denial=self._denial(error),
        )

    def _failed(
        self,
        command: ProvisionMaterializationCommand,
        error: SandboxApplicationError | SandboxPolicyViolation,
    ) -> MaterializationFailed:
        return MaterializationFailed(
            execution=command.binding.execution,
            binding_digest=command.binding.binding_digest,
            denial=self._denial(error),
        )

    def _claim(
        self,
        command: ProvisionMaterializationCommand,
        fingerprint: str,
        validation_error: SandboxPolicyViolation | None,
    ) -> tuple[
        ProvisionResult | None,
        ReservationRecord | None,
        str | None,
        str | None,
        str | None,
    ]:
        policy = self._dependencies.admission.snapshot(command.binding.environment.environment_id)
        now = self._dependencies.clock.now()
        with self._command_acquisition(
            command.context,
            action="sandbox.materialization.provision",
            target_id=command.binding.execution.execution_id,
        ) as db:
            repository = self._dependencies.repository_factory(db)
            owner_id = str(self._dependencies.random.uuid4())
            ownership = repository.acquire_command(
                command_id=str(self._dependencies.random.uuid4()),
                owner_id=owner_id,
                actor=command.context.actor,
                operation=_PROVISION_OPERATION,
                idempotency_key=command.context.idempotency_key,
                request_fingerprint=fingerprint,
                owner_expires_at=now + _COMMAND_OWNER_TTL,
                now=now,
            )
            row = repository.idempotency_by_scope(
                command.context.actor,
                _PROVISION_OPERATION,
                command.context.idempotency_key,
                for_update=True,
            )
            if row is None:
                raise IdempotencyReplayUnavailable("idempotency claim is unavailable")
            if row["request_fingerprint"] != fingerprint:
                raise IdempotencyConflict("Idempotency-Key is bound to a different request")
            if row["state"] == "COMPLETED":
                return self._replay(row), None, None, None, None
            if not ownership.created:
                if ownership.subject_id is None or ownership.phase not in {
                    "RESERVED",
                    "CLEANUP_STARTED",
                    "CLEANUP_QUARANTINED",
                }:
                    raise IdempotencyReplayUnavailable("provision recovery is unavailable")
                recovery = repository.provision_recovery(ownership.subject_id)
                token = self._unseal_recovery(
                    recovery.reservation,
                    recovery.recovery_capsule,
                )
                return (
                    None,
                    recovery.reservation,
                    token,
                    owner_id,
                    ownership.command_id,
                )
            if validation_error is not None:
                result = self._blocked(command, validation_error)
                self._audit(
                    db,
                    command,
                    target_type="AgentExecution",
                    target_id=command.binding.execution.execution_id,
                    result="DENIED",
                    reason=(f"{validation_error.code.value}:{validation_error.failure_dimension}"),
                )
                self._complete_receipt(repository, row, result, owner_id=owner_id)
                return result, None, None, None, None
            try:
                token = self._dependencies.random.token_urlsafe(32)
                reservation = repository.reserve_materialization(
                    binding=command.binding,
                    policy=policy,
                    materialization_id=str(self._dependencies.random.uuid4()),
                    lease_id=str(self._dependencies.random.uuid4()),
                    generation_id=str(self._dependencies.random.uuid4()),
                    fencing_token_digest=fencing_token_digest(token),
                    now=now,
                )
            except SandboxAdmissionError as error:
                result = self._blocked(command, error)
                self._audit(
                    db,
                    command,
                    target_type="AgentExecution",
                    target_id=command.binding.execution.execution_id,
                    result="DENIED",
                    reason=f"{error.code.value}:{error.failure_dimension}",
                )
                self._complete_receipt(repository, row, result, owner_id=owner_id)
                return result, None, None, None, None
            repository.bind_command_subject(
                ownership.command_id,
                reservation.materialization_id,
                owner_id=owner_id,
                phase="RESERVED",
                recovery_capsule=self._seal_recovery(reservation, token),
                now=now,
            )
            self._audit(
                db,
                command,
                target_type="SandboxMaterialization",
                target_id=reservation.materialization_id,
                result="STARTED",
                reason=None,
            )
            return None, reservation, token, owner_id, ownership.command_id

    @staticmethod
    def _validate_readiness(
        command: ProvisionMaterializationCommand,
        reservation: ReservationRecord,
        readiness: object,
    ) -> None:
        expected = (
            reservation.materialization_id,
            command.binding.binding_digest,
            reservation.generation,
            command.binding.runner_manifest.protocol_version,
            command.binding.deadline_at,
        )
        actual = (
            getattr(readiness, "materialization_id", None),
            getattr(readiness, "binding_digest", None),
            getattr(readiness, "generation", None),
            getattr(readiness, "protocol_version", None),
            getattr(readiness, "deadline_at", None),
        )
        if actual != expected:
            raise SandboxApplicationError(
                DenialCode.RUNTIME_BINDING_INVALID,
                "runner_readiness",
            )

    def _complete_ready(
        self,
        command: ProvisionMaterializationCommand,
        fingerprint: str,
        reservation: ReservationRecord,
        token: str,
        owner_id: str,
    ) -> MaterializationReady:
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            row = self._operation_row(
                repository,
                command.context,
                _PROVISION_OPERATION,
                fingerprint,
                owner_id,
            )
            ready = repository.mark_materialization_ready(
                reservation.materialization_id,
                generation=reservation.generation,
                now=self._dependencies.clock.now(),
            )
            result = MaterializationReady(
                handle=MaterializationHandle(
                    materialization_id=ready.materialization_id,
                    environment_id=ready.environment_id,
                    execution_id=ready.execution_id,
                    lease_id=ready.lease_id,
                    generation=ready.generation,
                    fencing_token=SecretStr(token),
                    revision=ready.revision,
                    deadline_at=ready.deadline_at,
                ),
                binding_digest=ready.binding_digest,
                runner_protocol_version=command.binding.runner_manifest.protocol_version,
                lab_only=True,
            )
            self._audit(
                db,
                command,
                target_type="SandboxMaterialization",
                target_id=ready.materialization_id,
                result="SUCCEEDED",
                reason=None,
            )
            self._complete_receipt(repository, row, result, owner_id=owner_id)
            return result

    def _complete_failed(
        self,
        command: ProvisionMaterializationCommand,
        fingerprint: str,
        reservation: ReservationRecord,
        token: str,
        owner_id: str,
        error: SandboxApplicationError | SandboxPolicyViolation,
    ) -> MaterializationFailed:
        guard = MaterializationGuard(
            materialization_id=reservation.materialization_id,
            lease_id=reservation.lease_id,
            generation=reservation.generation,
            fencing_token=SecretStr(token),
            expected_revision=reservation.revision,
        )
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            row = self._operation_row(
                repository,
                command.context,
                _PROVISION_OPERATION,
                fingerprint,
                owner_id,
            )
            current = repository.cleanup_record(reservation.materialization_id)
            if current.state is MaterializationState.PROVISIONING:
                current = repository.begin_provision_cleanup(
                    reservation.materialization_id,
                    denial_code=error.code.value,
                    failure_dimension=error.failure_dimension,
                    now=self._dependencies.clock.now(),
                )
                repository.advance_command(
                    str(row["id"]),
                    owner_id=owner_id,
                    phase="CLEANUP_STARTED",
                    subject_id=reservation.materialization_id,
                    now=self._dependencies.clock.now(),
                )
                self._audit_context(
                    db,
                    command.context,
                    action="sandbox.materialization.cleanup_started",
                    target_id=reservation.materialization_id,
                    result="STARTED",
                    reason=f"{error.code.value}:{error.failure_dimension}",
                )
        if current.state is not MaterializationState.FAILED:
            try:
                current = self._continue_cleanup(
                    guard,
                    (),
                    context=command.context,
                    operation=_PROVISION_OPERATION,
                    fingerprint=fingerprint,
                    owner_id=owner_id,
                )
            except RuntimeMaterializationError:
                quarantined = self._quarantine_lifecycle(
                    context=command.context,
                    materialization_id=reservation.materialization_id,
                    operation=_PROVISION_OPERATION,
                    action="sandbox.materialization.provision",
                    fingerprint=fingerprint,
                    owner_id=owner_id,
                )
                assert quarantined.denial is not None
                return MaterializationFailed(
                    execution=command.binding.execution,
                    binding_digest=command.binding.binding_digest,
                    denial=quarantined.denial,
                )
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            row = self._operation_row(
                repository,
                command.context,
                _PROVISION_OPERATION,
                fingerprint,
                owner_id,
            )
            if current.state is not MaterializationState.FAILED:
                repository.complete_cleanup(
                    reservation.materialization_id,
                    expected_revision=current.revision,
                    terminal_state="FAILED",
                    now=self._dependencies.clock.now(),
                )
                self._audit_context(
                    db,
                    command.context,
                    action="sandbox.cleanup.destroyed",
                    target_id=reservation.materialization_id,
                    result="SUCCEEDED",
                    reason=None,
                )
            result = self._failed(command, error)
            self._audit(
                db,
                command,
                target_type="SandboxMaterialization",
                target_id=reservation.materialization_id,
                result="FAILED",
                reason=f"{error.code.value}:{error.failure_dimension}",
            )
            self._complete_receipt(repository, row, result, owner_id=owner_id)
            return result

    def provision_materialization(
        self,
        command: ProvisionMaterializationCommand,
    ) -> ProvisionResult:
        self._authorize_context(
            command.context,
            operation=_PROVISION_OPERATION,
            environment_id=command.binding.environment.environment_id,
            action="sandbox.materialization.provision",
            target_id=command.binding.environment.environment_id,
        )
        fingerprint = self._fingerprint(command)
        validation_error: SandboxPolicyViolation | None = None
        try:
            validate_binding_for_provision(
                command.binding,
                now=self._dependencies.clock.now(),
            )
        except SandboxPolicyViolation as error:
            validation_error = error
        claimed_result, reservation, token, owner_id, command_id = self._claim(
            command,
            fingerprint,
            validation_error,
        )
        if claimed_result is not None:
            return claimed_result
        if reservation is None or token is None or owner_id is None or command_id is None:
            raise IdempotencyReplayUnavailable("provision reservation is unavailable")
        current = self._cleanup_record(reservation.materialization_id)
        if current.cleanup_terminal_state is MaterializationState.FAILED:
            with self._dependencies.engine.connect() as db:
                status = self._dependencies.repository_factory(db).materialization_status(
                    reservation.materialization_id
                )
            denial = status.denial
            return self._complete_failed(
                command,
                fingerprint,
                reservation,
                token,
                owner_id,
                SandboxApplicationError(
                    denial.code if denial else DenialCode.RESOURCE_EXHAUSTED,
                    (denial.failure_dimension if denial else None) or "runtime_materialization",
                    retryable=True,
                ),
            )
        handle = MaterializationHandle(
            materialization_id=reservation.materialization_id,
            environment_id=reservation.environment_id,
            execution_id=reservation.execution_id,
            lease_id=reservation.lease_id,
            generation=reservation.generation,
            fencing_token=SecretStr(token),
            revision=reservation.revision,
            deadline_at=reservation.deadline_at,
        )
        try:
            readiness = self._dependencies.runtime.provision(
                RuntimeMaterializationRequest(
                    operation_id=command_id,
                    handle=handle,
                    binding=command.binding,
                )
            )
            self._validate_readiness(command, reservation, readiness)
        except SandboxPolicyViolation as error:
            return self._complete_failed(
                command,
                fingerprint,
                reservation,
                token,
                owner_id,
                error,
            )
        except SandboxApplicationError as error:
            return self._complete_failed(
                command,
                fingerprint,
                reservation,
                token,
                owner_id,
                error,
            )
        except RuntimeMaterializationError:
            return self._complete_failed(
                command,
                fingerprint,
                reservation,
                token,
                owner_id,
                SandboxApplicationError(
                    DenialCode.RESOURCE_EXHAUSTED,
                    "runtime_materialization",
                    retryable=True,
                ),
            )
        return self._complete_ready(
            command,
            fingerprint,
            reservation,
            token,
            owner_id,
        )

    def get_materialization_status(
        self,
        query: GetMaterializationStatusQuery,
    ) -> MaterializationStatus:
        context = CommandContext(
            idempotency_key="sandbox.status",
            actor=query.actor,
            correlation_id=f"sandbox:status:{query.materialization_id}",
        )
        self._authorize_context(
            context,
            operation="sandbox.get_materialization_status",
            action="sandbox.materialization.status",
            target_id=query.materialization_id,
        )
        with self._dependencies.engine.begin() as db:
            status = self._dependencies.repository_factory(db).materialization_status(
                query.materialization_id
            )
            self._audit_context(
                db,
                context,
                action="sandbox.materialization.status",
                target_id=query.materialization_id,
                result="SUCCEEDED",
                reason=None,
            )
            return status

    @staticmethod
    def _model_response(model: Any, *, status_code: int) -> IdempotentResponse:
        return IdempotentResponse(status_code=status_code, body=model.model_dump(mode="json"))

    def _complete_preview_denial(
        self,
        command: PublishPreviewCommand,
        *,
        operation: str,
        fingerprint: str,
        owner_id: str,
        error: SandboxApplicationError | SandboxPolicyViolation,
    ) -> SandboxDenied:
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            status = repository.materialization_status(command.guard.materialization_id)
            result = SandboxDenied(denial=self._denial(error), revision=status.revision)
            self._audit_context(
                db,
                command.context,
                action="sandbox.preview.publish",
                target_id=command.guard.materialization_id,
                result="DENIED",
                reason=f"{error.code.value}:{error.failure_dimension}",
            )
            row = self._operation_row(
                repository,
                command.context,
                operation,
                fingerprint,
                owner_id,
            )
            self._complete_response(
                repository,
                row,
                self._model_response(result, status_code=409),
                owner_id=owner_id,
            )
            return result

    def publish_preview(self, command: PublishPreviewCommand) -> PreviewResult:
        operation = "sandbox.preview.publish"
        self._authorize_context(
            command.context,
            operation=operation,
            action=operation,
            target_id=command.guard.materialization_id,
        )
        path = f"{_PROVISION_PATH}/{command.guard.materialization_id}/preview"
        body = {
            **self._guard_body(command.guard),
            "metadata": command.metadata.model_dump(mode="json"),
            "expiresAt": command.expires_at.isoformat(),
        }
        work = self._claim_operation(
            command.context,
            operation=operation,
            action=operation,
            target_id=command.guard.materialization_id,
            path=path,
            body=body,
        )
        if work.replay is not None:
            return _PREVIEW_RESULT.validate_python(work.replay.body)
        try:
            guard = command.guard
            if work.subject_id is None:
                with self._dependencies.engine.begin() as db:
                    now = self._dependencies.clock.now()
                    if command.expires_at <= now:
                        raise SandboxApplicationError(
                            DenialCode.RUNTIME_BINDING_INVALID, "preview_expiry"
                        )
                    intent = self._dependencies.repository_factory(db).begin_preview_intent(
                        command_id=work.command_id,
                        owner_id=work.owner_id,
                        guard=guard,
                        metadata=command.metadata,
                        expires_at=command.expires_at,
                        now=now,
                    )
                    self._audit_context(
                        db,
                        command.context,
                        action="sandbox.preview.intent",
                        target_id=guard.materialization_id,
                        result="STARTED",
                        reason=None,
                    )
                guard = guard.model_copy(update={"expected_revision": intent.revision})
            else:
                if work.subject_id != guard.materialization_id:
                    raise IdempotencyReplayUnavailable("preview recovery is unavailable")
                guard = self._restore_guard(self._cleanup_record(guard.materialization_id))
            preview = self._dependencies.runtime.publish_preview(
                work.command_id,
                guard,
                command.metadata,
                command.expires_at,
            )
            with self._dependencies.engine.begin() as db:
                repository = self._dependencies.repository_factory(db)
                intent = repository.complete_preview_intent(
                    command_id=work.command_id,
                    owner_id=work.owner_id,
                    evidence_id=str(self._dependencies.random.uuid4()),
                    metadata=command.metadata,
                    result_capsule=seal(
                        preview.model_dump_json().encode("utf-8"),
                        self._dependencies.idempotency_sealing_key,
                    ),
                    now=self._dependencies.clock.now(),
                )
                result = PreviewPublished(
                    preview_id=preview.preview_id,
                    access_ref=preview.access_ref,
                    expires_at=preview.expires_at,
                    revision=intent.revision,
                )
                self._audit_context(
                    db,
                    command.context,
                    action=operation,
                    target_id=command.guard.materialization_id,
                    result="SUCCEEDED",
                    reason=None,
                )
                row = self._operation_row(
                    repository,
                    command.context,
                    operation,
                    work.fingerprint,
                    work.owner_id,
                )
                self._complete_response(
                    repository,
                    row,
                    self._model_response(result, status_code=201),
                    owner_id=work.owner_id,
                )
                return result
        except (SandboxApplicationError, SandboxPolicyViolation) as error:
            return self._complete_preview_denial(
                command,
                operation=operation,
                fingerprint=work.fingerprint,
                owner_id=work.owner_id,
                error=error,
            )

    def _complete_lifecycle_denial(
        self,
        *,
        context: CommandContext,
        guard: MaterializationGuard,
        operation: str,
        action: str,
        fingerprint: str,
        owner_id: str,
        error: SandboxApplicationError | SandboxPolicyViolation,
    ) -> LifecycleReceipt:
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            status = repository.materialization_status(guard.materialization_id)
            receipt = LifecycleReceipt(
                operation=operation,
                materialization_id=guard.materialization_id,
                state=status.state,
                revision=status.revision,
                evidence_refs=status.evidence_refs,
                denial=self._denial(error),
            )
            self._audit_context(
                db,
                context,
                action=action,
                target_id=guard.materialization_id,
                result="DENIED",
                reason=f"{error.code.value}:{error.failure_dimension}",
            )
            row = self._operation_row(
                repository,
                context,
                operation,
                fingerprint,
                owner_id,
            )
            self._complete_response(
                repository,
                row,
                self._model_response(receipt, status_code=409),
                owner_id=owner_id,
            )
            return receipt

    def handoff_to_child(self, command: HandoffToChildCommand) -> HandoffResult:
        operation = "sandbox.handoff_to_child"
        action = "sandbox.materialization.handoff"
        self._authorize_context(
            command.context,
            operation=operation,
            action=action,
            target_id=command.guard.materialization_id,
        )
        path = f"{_PROVISION_PATH}/{command.guard.materialization_id}/handoff"
        work = self._claim_operation(
            command.context,
            operation=operation,
            action=action,
            target_id=command.guard.materialization_id,
            path=path,
            body={
                **self._guard_body(command.guard),
                "childExecutionId": command.child_execution_id,
            },
        )
        if work.replay is not None:
            return _LIFECYCLE_RESULT.validate_python(work.replay.body)
        try:
            with self._dependencies.engine.begin() as db:
                self._dependencies.repository_factory(db).lock_current_guard(command.guard)
        except StaleRunnerGeneration as error:
            return self._complete_lifecycle_denial(
                context=command.context,
                guard=command.guard,
                operation=operation,
                action=action,
                fingerprint=work.fingerprint,
                owner_id=work.owner_id,
                error=error,
            )
        return self._complete_lifecycle_denial(
            context=command.context,
            guard=command.guard,
            operation=operation,
            action=action,
            fingerprint=work.fingerprint,
            owner_id=work.owner_id,
            error=SandboxApplicationError(
                DenialCode.POLICY_DISABLED,
                "child_execution",
            ),
        )

    def _cleanup(
        self,
        *,
        context: CommandContext,
        guard: MaterializationGuard,
        evidence_refs: tuple[EvidenceRef, ...],
        operation: str,
        action: str,
        path_suffix: str,
        transition_state: str,
        terminal_state: MaterializationState,
    ) -> LifecycleReceipt:
        self._authorize_context(
            context,
            operation=operation,
            action=action,
            target_id=guard.materialization_id,
        )
        path = f"{_PROVISION_PATH}/{guard.materialization_id}/{path_suffix}"
        work = self._claim_operation(
            context,
            operation=operation,
            action=action,
            target_id=guard.materialization_id,
            path=path,
            body={
                **self._guard_body(guard),
                "evidenceRefs": [item.model_dump(mode="json") for item in evidence_refs],
            },
        )
        if work.replay is not None:
            return _LIFECYCLE_RESULT.validate_python(work.replay.body)
        try:
            if work.subject_id is None:
                with self._dependencies.engine.begin() as db:
                    repository = self._dependencies.repository_factory(db)
                    self._operation_row(
                        repository, context, operation, work.fingerprint, work.owner_id
                    )
                    repository.begin_cleanup(
                        guard,
                        evidence=tuple(
                            (str(self._dependencies.random.uuid4()), item) for item in evidence_refs
                        ),
                        transition_state=transition_state,
                        terminal_state=terminal_state.value,
                        cancellation_reason=None,
                        now=self._dependencies.clock.now(),
                    )
                    repository.advance_command(
                        work.command_id,
                        owner_id=work.owner_id,
                        phase="CLEANUP_STARTED",
                        subject_id=guard.materialization_id,
                        now=self._dependencies.clock.now(),
                    )
                    self._audit_context(
                        db,
                        context,
                        action="sandbox.materialization.cleanup_started",
                        target_id=guard.materialization_id,
                        result="STARTED",
                        reason=terminal_state.value,
                    )
            else:
                if work.subject_id != guard.materialization_id:
                    raise IdempotencyReplayUnavailable("cleanup recovery is unavailable")
                guard = self._restore_guard(self._cleanup_record(guard.materialization_id))
        except StaleRunnerGeneration as error:
            return self._complete_lifecycle_denial(
                context=context,
                guard=guard,
                operation=operation,
                action=action,
                fingerprint=work.fingerprint,
                owner_id=work.owner_id,
                error=error,
            )
        try:
            current = self._continue_cleanup(
                guard,
                evidence_refs,
                context=context,
                operation=operation,
                fingerprint=work.fingerprint,
                owner_id=work.owner_id,
            )
        except RuntimeMaterializationError:
            return self._quarantine_lifecycle(
                context=context,
                materialization_id=guard.materialization_id,
                operation=operation,
                action=action,
                fingerprint=work.fingerprint,
                owner_id=work.owner_id,
            )
        return self._complete_lifecycle_cleanup(
            context=context,
            materialization_id=guard.materialization_id,
            operation=operation,
            action=action,
            fingerprint=work.fingerprint,
            owner_id=work.owner_id,
            expected_revision=current.revision,
            terminal_state=terminal_state,
        )

    def _cleanup_record(self, materialization_id: str) -> CleanupRecord:
        with self._dependencies.engine.connect() as db:
            return self._dependencies.repository_factory(db).cleanup_record(materialization_id)

    def _continue_cleanup(
        self,
        guard: MaterializationGuard,
        evidence_refs: tuple[EvidenceRef, ...],
        *,
        context: CommandContext,
        operation: str,
        fingerprint: str,
        owner_id: str,
    ) -> CleanupRecord:
        def commit_step(current: CleanupRecord, method: str, action: str) -> None:
            with self._dependencies.engine.begin() as db:
                repository = self._dependencies.repository_factory(db)
                self._operation_row(repository, context, operation, fingerprint, owner_id)
                transition = cast(Callable[..., int], getattr(repository, method))
                transition(
                    guard.materialization_id,
                    expected_revision=current.revision,
                    now=self._dependencies.clock.now(),
                )
                self._audit_context(
                    db,
                    context,
                    action=action,
                    target_id=guard.materialization_id,
                    result="SUCCEEDED",
                    reason=None,
                )

        current = self._cleanup_record(guard.materialization_id)
        if current.destroyed and current.state in {
            MaterializationState.RELEASED,
            MaterializationState.FINALIZED,
            MaterializationState.CANCELED,
            MaterializationState.TIMED_OUT,
            MaterializationState.FAILED,
        }:
            return current
        if not current.evidence_persisted:
            observation = self._dependencies.runtime.observe(guard.materialization_id)
            if observation.presence is RuntimePresence.UNKNOWN:
                raise RuntimeMaterializationError("runtime observation is unavailable")
            if observation.presence is RuntimePresence.PRESENT:
                self._dependencies.runtime.persist_evidence(guard, evidence_refs)
            commit_step(current, "mark_evidence_persisted", "sandbox.cleanup.evidence_persisted")
            current = self._cleanup_record(guard.materialization_id)

        observation = self._dependencies.runtime.observe(guard.materialization_id)
        if not current.fenced:
            if observation.presence is RuntimePresence.UNKNOWN:
                raise RuntimeMaterializationError("runtime observation is unavailable")
            if (
                observation.presence is RuntimePresence.PRESENT
                and not observation.side_effects_fenced
            ):
                self._dependencies.runtime.fence(guard)
            commit_step(current, "mark_fenced", "sandbox.cleanup.fenced")
            current = self._cleanup_record(guard.materialization_id)

        observation = self._dependencies.runtime.observe(guard.materialization_id)
        if not current.secret_revoked:
            if observation.presence is RuntimePresence.UNKNOWN:
                raise RuntimeMaterializationError("runtime observation is unavailable")
            if observation.presence is RuntimePresence.PRESENT and not observation.secret_revoked:
                self._dependencies.runtime.revoke_secret(guard)
            commit_step(current, "mark_secret_revoked", "sandbox.cleanup.secret_revoked")
            current = self._cleanup_record(guard.materialization_id)

        if not current.lease_released:
            commit_step(current, "release_capacity", "sandbox.cleanup.capacity_released")
            current = self._cleanup_record(guard.materialization_id)

        observation = self._dependencies.runtime.observe(guard.materialization_id)
        if observation.presence is RuntimePresence.UNKNOWN:
            raise RuntimeMaterializationError("runtime observation is unavailable")
        if observation.presence is RuntimePresence.PRESENT and not observation.destroyed:
            self._dependencies.runtime.destroy(guard)
        return current

    def _complete_lifecycle_cleanup(
        self,
        *,
        context: CommandContext,
        materialization_id: str,
        operation: str,
        action: str,
        fingerprint: str,
        owner_id: str,
        expected_revision: int,
        terminal_state: MaterializationState,
    ) -> LifecycleReceipt:
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            self._operation_row(repository, context, operation, fingerprint, owner_id)
            current = repository.cleanup_record(materialization_id)
            already_terminal = current.state is terminal_state
            revision = current.revision
            if not already_terminal:
                revision = repository.complete_cleanup(
                    materialization_id,
                    expected_revision=expected_revision,
                    terminal_state=terminal_state.value,
                    now=self._dependencies.clock.now(),
                )
            status = repository.materialization_status(materialization_id)
            receipt = LifecycleReceipt(
                operation=operation,
                materialization_id=materialization_id,
                state=terminal_state,
                revision=revision,
                evidence_refs=status.evidence_refs,
            )
            if not already_terminal:
                self._audit_context(
                    db,
                    context,
                    action="sandbox.cleanup.destroyed",
                    target_id=materialization_id,
                    result="SUCCEEDED",
                    reason=None,
                )
            self._audit_context(
                db,
                context,
                action=action,
                target_id=materialization_id,
                result="SUCCEEDED",
                reason=current.cancellation_reason,
            )
            row = self._operation_row(
                repository,
                context,
                operation,
                fingerprint,
                owner_id,
            )
            self._complete_response(
                repository,
                row,
                self._model_response(receipt, status_code=200),
                owner_id=owner_id,
            )
            return receipt

    def _quarantine_lifecycle(
        self,
        *,
        context: CommandContext,
        materialization_id: str,
        operation: str,
        action: str,
        fingerprint: str,
        owner_id: str,
    ) -> LifecycleReceipt:
        error = SandboxApplicationError(
            DenialCode.RESOURCE_EXHAUSTED,
            "runtime_cleanup",
            retryable=True,
        )
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            self._operation_row(repository, context, operation, fingerprint, owner_id)
            current = repository.cleanup_record(materialization_id)
            revision = repository.quarantine_cleanup(
                materialization_id,
                expected_revision=current.revision,
                denial_code=error.code.value,
                failure_dimension=error.failure_dimension,
                now=self._dependencies.clock.now(),
            )
            status = repository.materialization_status(materialization_id)
            receipt = LifecycleReceipt(
                operation=operation,
                materialization_id=materialization_id,
                state=MaterializationState.QUARANTINED,
                revision=revision,
                evidence_refs=status.evidence_refs,
                denial=self._denial(error),
            )
            self._audit_context(
                db,
                context,
                action=action,
                target_id=materialization_id,
                result="FAILED",
                reason=f"{error.code.value}:{error.failure_dimension}",
            )
            repository.advance_command(
                self._operation_row(
                    repository,
                    context,
                    operation,
                    fingerprint,
                    owner_id,
                )["id"],
                owner_id=owner_id,
                phase="CLEANUP_QUARANTINED",
                subject_id=materialization_id,
                now=self._dependencies.clock.now(),
            )
            return receipt

    def _restore_guard(self, current: CleanupRecord) -> MaterializationGuard:
        with self._dependencies.engine.connect() as db:
            recovery = self._dependencies.repository_factory(db).provision_recovery(
                current.materialization_id
            )
        reservation = recovery.reservation
        if (
            reservation.materialization_id != current.materialization_id
            or reservation.environment_id != current.environment_id
            or reservation.execution_id != current.execution_id
            or reservation.lease_id != current.lease_id
            or reservation.generation != current.generation
        ):
            raise IdempotencyReplayUnavailable("provision recovery is unavailable")
        return MaterializationGuard(
            materialization_id=current.materialization_id,
            lease_id=current.lease_id,
            generation=current.generation,
            fencing_token=SecretStr(self._unseal_recovery(reservation, recovery.recovery_capsule)),
            expected_revision=current.revision,
        )

    def checkpoint_and_release(
        self,
        command: CheckpointAndReleaseCommand,
    ) -> ReleaseReceipt:
        return self._cleanup(
            context=command.context,
            guard=command.guard,
            evidence_refs=command.evidence_refs,
            operation="sandbox.checkpoint_and_release",
            action="sandbox.materialization.checkpoint_release",
            path_suffix="checkpoint-release",
            transition_state="RELEASING",
            terminal_state=MaterializationState.RELEASED,
        )

    def finalize_execution(
        self,
        command: FinalizeExecutionCommand,
    ) -> LifecycleReceipt:
        return self._cleanup(
            context=command.context,
            guard=command.guard,
            evidence_refs=command.evidence_refs,
            operation="sandbox.finalize_execution",
            action="sandbox.materialization.finalize",
            path_suffix="finalize",
            transition_state="FINALIZING",
            terminal_state=MaterializationState.FINALIZED,
        )

    def cancel_execution(self, command: CancelExecutionCommand) -> CancellationReceipt:
        operation = "sandbox.cancel_execution"
        action = "sandbox.execution.cancel"
        with self._dependencies.engine.connect() as db:
            existing = self._dependencies.repository_factory(db).idempotency_by_scope(
                command.context.actor, operation, command.context.idempotency_key
            )
            existing_subject = (
                str(existing["subject_id"])
                if existing is not None and existing["subject_id"] is not None
                else None
            )
        authorized_environment_id = self._authorize_context(
            command.context,
            operation=operation,
            action=action,
            target_id=existing_subject or command.execution.execution_id,
            execution_scope=existing_subject is None,
        )
        path = f"{_SANDBOX_PATH}/executions/{command.execution.execution_id}/cancel"
        work = self._claim_operation(
            command.context,
            operation=operation,
            action=action,
            target_id=command.execution.execution_id,
            path=path,
            body={
                "execution": command.execution.model_dump(mode="json"),
                "reason": command.reason.value,
            },
            authorize_bound_subject=True,
        )
        if work.replay is not None:
            return _LIFECYCLE_RESULT.validate_python(work.replay.body)
        terminal_state = (
            MaterializationState.TIMED_OUT
            if command.reason is CancellationReason.TIMED_OUT
            else MaterializationState.CANCELED
        )
        if work.subject_id is None:
            try:
                with self._dependencies.engine.begin() as db:
                    repository = self._dependencies.repository_factory(db)
                    self._operation_row(
                        repository, command.context, operation, work.fingerprint, work.owner_id
                    )
                    current = repository.begin_cancel_cleanup(
                        execution_id=command.execution.execution_id,
                        authorized_environment_id=authorized_environment_id,
                        terminal_state=terminal_state.value,
                        cancellation_reason=command.reason.value,
                        command_id=work.command_id,
                        owner_id=work.owner_id,
                        now=self._dependencies.clock.now(),
                    )
                    self._audit_context(
                        db,
                        command.context,
                        action="sandbox.materialization.cleanup_started",
                        target_id=current.materialization_id,
                        result="STARTED",
                        reason=command.reason.value,
                    )
            except SandboxApplicationError as error:
                with self._dependencies.engine.begin() as db:
                    self._audit_context(
                        db,
                        command.context,
                        action=action,
                        target_id=command.execution.execution_id,
                        result="DENIED",
                        reason=f"{error.code.value}:{error.failure_dimension}",
                    )
                raise
        else:
            current = self._cleanup_record(work.subject_id)
        if current.state in {
            MaterializationState.RELEASED,
            MaterializationState.FINALIZED,
            MaterializationState.CANCELED,
            MaterializationState.TIMED_OUT,
            MaterializationState.FAILED,
        }:
            with self._dependencies.engine.begin() as db:
                repository = self._dependencies.repository_factory(db)
                status = repository.materialization_status(current.materialization_id)
                receipt = LifecycleReceipt(
                    operation=operation,
                    materialization_id=current.materialization_id,
                    state=status.state,
                    revision=status.revision,
                    evidence_refs=status.evidence_refs,
                    denial=status.denial,
                )
                self._audit_context(
                    db,
                    command.context,
                    action=action,
                    target_id=current.materialization_id,
                    result="SUCCEEDED",
                    reason="already_terminal",
                )
                row = self._operation_row(
                    repository,
                    command.context,
                    operation,
                    work.fingerprint,
                    work.owner_id,
                )
                self._complete_response(
                    repository,
                    row,
                    self._model_response(receipt, status_code=200),
                    owner_id=work.owner_id,
                )
                return receipt

        guard = self._restore_guard(current)
        try:
            current = self._continue_cleanup(
                guard,
                (),
                context=command.context,
                operation=operation,
                fingerprint=work.fingerprint,
                owner_id=work.owner_id,
            )
        except RuntimeMaterializationError:
            return self._quarantine_lifecycle(
                context=command.context,
                materialization_id=guard.materialization_id,
                operation=operation,
                action=action,
                fingerprint=work.fingerprint,
                owner_id=work.owner_id,
            )
        return self._complete_lifecycle_cleanup(
            context=command.context,
            materialization_id=guard.materialization_id,
            operation=operation,
            action=action,
            fingerprint=work.fingerprint,
            owner_id=work.owner_id,
            expected_revision=current.revision,
            terminal_state=terminal_state,
        )

    def reconcile_lease(self, command: ReconcileLeaseCommand) -> ReconciliationReceipt:
        operation = "sandbox.reconcile_lease"
        action = "sandbox.lease.reconcile"
        self._authorize_context(
            command.context,
            operation=operation,
            environment_id=command.environment_id,
            action=action,
            target_id=command.environment_id,
        )
        if command.observed_at > self._dependencies.clock.now():
            error = SandboxApplicationError(
                DenialCode.RUNTIME_BINDING_INVALID,
                "observed_at",
            )
            with self._dependencies.engine.begin() as db:
                self._audit_context(
                    db,
                    command.context,
                    action=action,
                    target_id=command.environment_id,
                    result="DENIED",
                    reason=f"{error.code.value}:{error.failure_dimension}",
                )
            raise error
        path = f"{_SANDBOX_PATH}/leases/reconcile"
        work = self._claim_operation(
            command.context,
            operation=operation,
            action=action,
            target_id=command.environment_id,
            path=path,
            body={
                "environmentId": command.environment_id,
                "executionId": command.execution_id,
                "observedAt": command.observed_at.isoformat(),
            },
        )
        if work.replay is not None:
            return _RECONCILIATION_RESULT.validate_python(work.replay.body)
        progress = work.progress
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            self._operation_row(
                repository,
                command.context,
                operation,
                work.fingerprint,
                work.owner_id,
            )
            if not progress:
                candidates = repository.cleanup_candidates(
                    environment_id=command.environment_id,
                    execution_id=command.execution_id,
                    observed_at=command.observed_at,
                )
                progress = {
                    "candidates": [item.materialization_id for item in candidates],
                    "items": {},
                }
                repository.save_command_progress(
                    work.command_id,
                    owner_id=work.owner_id,
                    progress=progress,
                    now=self._dependencies.clock.now(),
                )
            else:
                candidates = tuple(
                    repository.cleanup_record(item) for item in progress["candidates"]
                )

        items: list[LeaseReconciliationItem] = []
        reconciled_count = 0
        for candidate in candidates:
            completed_item = progress["items"].get(candidate.materialization_id)
            if completed_item is not None:
                item = LeaseReconciliationItem.model_validate(completed_item)
                items.append(item)
                reconciled_count += int(item.action == "CLEANUP_COMPLETED")
                continue
            guard = self._restore_guard(candidate)
            if candidate.state is MaterializationState.PROVISIONING:
                with self._dependencies.engine.begin() as db:
                    repository = self._dependencies.repository_factory(db)
                    self._operation_row(
                        repository, command.context, operation, work.fingerprint, work.owner_id
                    )
                    repository.begin_provision_cleanup(
                        candidate.materialization_id,
                        denial_code=DenialCode.RESOURCE_EXHAUSTED.value,
                        failure_dimension="orphaned_provisioning",
                        now=self._dependencies.clock.now(),
                    )
                    self._audit_context(
                        db,
                        command.context,
                        action="sandbox.materialization.cleanup_started",
                        target_id=candidate.materialization_id,
                        result="STARTED",
                        reason="orphaned_provisioning",
                    )
                guard = self._restore_guard(self._cleanup_record(candidate.materialization_id))
            elif candidate.state is MaterializationState.READY:
                with self._dependencies.engine.begin() as db:
                    repository = self._dependencies.repository_factory(db)
                    self._operation_row(
                        repository, command.context, operation, work.fingerprint, work.owner_id
                    )
                    repository.begin_cleanup(
                        guard,
                        evidence=(),
                        transition_state="CANCELING",
                        terminal_state=MaterializationState.TIMED_OUT.value,
                        cancellation_reason=CancellationReason.TIMED_OUT.value,
                        now=self._dependencies.clock.now(),
                    )
                    self._audit_context(
                        db,
                        command.context,
                        action="sandbox.materialization.cleanup_started",
                        target_id=candidate.materialization_id,
                        result="STARTED",
                        reason="TIMED_OUT",
                    )
                guard = self._restore_guard(self._cleanup_record(candidate.materialization_id))
            current = self._cleanup_record(candidate.materialization_id)
            terminal_state = current.cleanup_terminal_state
            if terminal_state is None:
                raise StaleRunnerGeneration
            with self._dependencies.engine.connect() as db:
                status = self._dependencies.repository_factory(db).materialization_status(
                    candidate.materialization_id
                )
            try:
                current = self._continue_cleanup(
                    guard,
                    status.evidence_refs,
                    context=command.context,
                    operation=operation,
                    fingerprint=work.fingerprint,
                    owner_id=work.owner_id,
                )
            except RuntimeMaterializationError:
                with self._dependencies.engine.begin() as db:
                    repository = self._dependencies.repository_factory(db)
                    self._operation_row(
                        repository, command.context, operation, work.fingerprint, work.owner_id
                    )
                    failed = repository.cleanup_record(candidate.materialization_id)
                    revision = repository.quarantine_cleanup(
                        candidate.materialization_id,
                        expected_revision=failed.revision,
                        denial_code=DenialCode.RESOURCE_EXHAUSTED.value,
                        failure_dimension="runtime_cleanup",
                        now=self._dependencies.clock.now(),
                    )
                    self._audit_context(
                        db,
                        command.context,
                        action=action,
                        target_id=candidate.materialization_id,
                        result="FAILED",
                        reason="RESOURCE_EXHAUSTED:runtime_cleanup",
                    )
                    item = LeaseReconciliationItem(
                        materialization_id=candidate.materialization_id,
                        state=MaterializationState.QUARANTINED,
                        revision=revision,
                        action="QUARANTINED",
                    )
                    progress["items"][candidate.materialization_id] = item.model_dump(mode="json")
                    repository.save_command_progress(
                        work.command_id,
                        owner_id=work.owner_id,
                        progress=progress,
                        now=self._dependencies.clock.now(),
                    )
                items.append(item)
                continue
            with self._dependencies.engine.begin() as db:
                repository = self._dependencies.repository_factory(db)
                self._operation_row(
                    repository,
                    command.context,
                    operation,
                    work.fingerprint,
                    work.owner_id,
                )
                revision = current.revision
                if current.state is not terminal_state:
                    revision = repository.complete_cleanup(
                        candidate.materialization_id,
                        expected_revision=current.revision,
                        terminal_state=terminal_state.value,
                        now=self._dependencies.clock.now(),
                    )
                    self._audit_context(
                        db,
                        command.context,
                        action="sandbox.cleanup.destroyed",
                        target_id=candidate.materialization_id,
                        result="SUCCEEDED",
                        reason=None,
                    )
                self._audit_context(
                    db,
                    command.context,
                    action=action,
                    target_id=candidate.materialization_id,
                    result="SUCCEEDED",
                    reason=current.cancellation_reason or terminal_state.value,
                )
                item = LeaseReconciliationItem(
                    materialization_id=candidate.materialization_id,
                    state=terminal_state,
                    revision=revision,
                    action="CLEANUP_COMPLETED",
                )
                progress["items"][candidate.materialization_id] = item.model_dump(mode="json")
                repository.save_command_progress(
                    work.command_id,
                    owner_id=work.owner_id,
                    progress=progress,
                    now=self._dependencies.clock.now(),
                )
            items.append(item)
            reconciled_count += 1

        receipt = ReconciliationReceipt(
            environment_id=command.environment_id,
            observed_at=command.observed_at,
            items=tuple(items),
            reconciled_count=reconciled_count,
        )
        with self._dependencies.engine.begin() as db:
            repository = self._dependencies.repository_factory(db)
            repository.record_reconciliation(
                reconciliation_id=work.command_id,
                environment_id=command.environment_id,
                execution_id=command.execution_id,
                actor=command.context.actor,
                correlation_id=command.context.correlation_id,
                observed_at=command.observed_at,
                scanned_count=len(candidates),
                reconciled_count=reconciled_count,
                now=self._dependencies.clock.now(),
            )
            row = self._operation_row(
                repository,
                command.context,
                operation,
                work.fingerprint,
                work.owner_id,
            )
            self._complete_response(
                repository,
                row,
                self._model_response(receipt, status_code=200),
                owner_id=work.owner_id,
            )
        return receipt
