import hmac
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from pydantic import ConfigDict
from sqlalchemy import Connection, Engine, text

from control_plane.app.modules.agent_run.domain import (
    DenialCode,
    EvidenceRef,
    ExecutionBindingProjection,
    MaterializationGuard,
)
from control_plane.app.modules.agent_run.domain.models import FrozenModel, NonEmptyStr, PositiveInt
from control_plane.app.modules.agent_run.domain.policy import (
    SandboxPolicyViolation,
    fencing_token_digest,
    validate_binding_for_provision,
)
from control_plane.app.modules.agent_run.ports.runtime import (
    RuntimeMaterializationError,
    RuntimeMaterializationRequest,
    RuntimeObservation,
    RuntimePresence,
    RuntimePreview,
    RuntimeReadiness,
)


class DevRuntimeEvent(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: NonEmptyStr
    materialization_id: NonEmptyStr
    generation: PositiveInt


class DevRuntimeStepFailure(RuntimeMaterializationError):
    """Deterministic, safe failure used to exercise reconciliation."""


class _RuntimeState:
    def __init__(
        self,
        guard: MaterializationGuard,
        binding: ExecutionBindingProjection | None,
    ) -> None:
        self.guard = guard
        self.binding = binding
        self.evidence_persisted = False
        self.fenced = False
        self.secret_revoked = False
        self.destroyed = False


class RestrictedDevSandboxAdapter:
    """LAB_ONLY contract adapter; it never executes repository-controlled code."""

    def __init__(
        self,
        *,
        repository_root: Path,
        repositories: Mapping[str, Path],
        fail_steps: frozenset[str] = frozenset(),
        state_engine: Engine | None = None,
        observation_available: bool = True,
    ) -> None:
        try:
            self._repository_root = repository_root.resolve(strict=True)
        except OSError:
            raise ValueError("repository root is unavailable") from None
        self._repositories = dict(repositories)
        self._fail_steps = fail_steps
        self._state_engine = state_engine
        self._observation_available = observation_available
        self._events: list[DevRuntimeEvent] = []
        self._states: dict[str, _RuntimeState] = {}
        self._preview_results: dict[str, RuntimePreview] = {}
        self._serialization = RLock()

    @property
    def events(self) -> tuple[DevRuntimeEvent, ...]:
        return tuple(self._events)

    def clear_failures(self) -> None:
        self._fail_steps = frozenset()

    def _record(self, action: str, guard: MaterializationGuard) -> None:
        if action in self._fail_steps:
            raise DevRuntimeStepFailure(f"restricted DEV step failed: {action}")
        self._events.append(
            DevRuntimeEvent(
                action=action,
                materialization_id=guard.materialization_id,
                generation=guard.generation,
            )
        )

    @staticmethod
    def _is_link(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", None)
        if path.is_symlink() or bool(is_junction and is_junction()):
            return True
        try:
            return path.is_file() and path.stat(follow_symlinks=False).st_nlink > 1
        except OSError:
            return True

    def _validate_repository(self, binding: ExecutionBindingProjection) -> None:
        repository_id = binding.boundaries.repository_checkout.repository_id
        configured = self._repositories.get(repository_id)
        if configured is None:
            raise SandboxPolicyViolation(
                DenialCode.RUNTIME_BINDING_INVALID,
                "repository_binding",
            )
        if self._is_link(configured):
            raise SandboxPolicyViolation(
                DenialCode.RUNTIME_BOUNDARY_VIOLATION,
                "repository_link",
            )
        try:
            repository = configured.resolve(strict=True)
        except OSError:
            raise SandboxPolicyViolation(
                DenialCode.RUNTIME_BINDING_INVALID,
                "repository_binding",
            ) from None
        if not repository.is_dir() or not repository.is_relative_to(self._repository_root):
            raise SandboxPolicyViolation(
                DenialCode.RUNTIME_BOUNDARY_VIOLATION,
                "repository_root",
            )

        for current_root, directory_names, file_names in os.walk(repository, followlinks=False):
            root = Path(current_root)
            if ".git" in directory_names or ".git" in root.parts:
                raise SandboxPolicyViolation(
                    DenialCode.RUNTIME_BOUNDARY_VIOLATION,
                    "repository_control_data",
                )
            for name in (*directory_names, *file_names):
                candidate = root / name
                if self._is_link(candidate):
                    raise SandboxPolicyViolation(
                        DenialCode.RUNTIME_BOUNDARY_VIOLATION,
                        "repository_link",
                    )

    def provision(self, request: RuntimeMaterializationRequest) -> RuntimeReadiness:
        validate_binding_for_provision(request.binding, now=datetime.now(UTC))
        self._validate_repository(request.binding)
        guard = MaterializationGuard(
            materialization_id=request.handle.materialization_id,
            lease_id=request.handle.lease_id,
            generation=request.handle.generation,
            fencing_token=request.handle.fencing_token,
            expected_revision=request.handle.revision,
        )
        readiness = RuntimeReadiness(
            materialization_id=guard.materialization_id,
            binding_digest=request.binding.binding_digest,
            generation=guard.generation,
            protocol_version=request.binding.runner_manifest.protocol_version,
            deadline_at=request.binding.deadline_at,
            lab_only=True,
            isolation_evidence_refs=(),
        )
        if self._state_engine is None:
            existing = self._states.get(guard.materialization_id)
            if existing is not None:
                if not self._same_fence(existing.guard, guard):
                    raise SandboxPolicyViolation(
                        DenialCode.STALE_RUNNER_GENERATION,
                        "runner_generation",
                    )
                return readiness
            self._record("provision", guard)
            self._states[guard.materialization_id] = _RuntimeState(guard, request.binding)
            return readiness

        with self._state_engine.begin() as db:
            materialization_state = db.execute(
                text("SELECT state FROM agent_run.sandbox_materialization WHERE id=:id FOR UPDATE"),
                {"id": guard.materialization_id},
            ).scalar_one_or_none()
            if materialization_state not in {"PROVISIONING", "READY"}:
                raise SandboxPolicyViolation(
                    DenialCode.STALE_RUNNER_GENERATION, "runner_generation"
                )
            row = (
                db.execute(
                    text(
                        "SELECT operation_id, generation, fencing_token_digest, "
                        "binding_digest, protocol_version, deadline_at "
                        "FROM agent_run.runtime_state WHERE materialization_id=:id FOR UPDATE"
                    ),
                    {"id": guard.materialization_id},
                )
                .mappings()
                .one_or_none()
            )
            expected = (
                request.operation_id,
                guard.generation,
                fencing_token_digest(guard.fencing_token.get_secret_value()),
                request.binding.binding_digest,
                request.binding.runner_manifest.protocol_version,
                request.binding.deadline_at,
            )
            if row is not None:
                actual = (
                    str(row["operation_id"]),
                    row["generation"],
                    row["fencing_token_digest"],
                    row["binding_digest"],
                    row["protocol_version"],
                    row["deadline_at"],
                )
                if actual != expected:
                    raise SandboxPolicyViolation(
                        DenialCode.STALE_RUNNER_GENERATION,
                        "runner_generation",
                    )
                return readiness
            if materialization_state != "PROVISIONING":
                raise SandboxPolicyViolation(
                    DenialCode.STALE_RUNNER_GENERATION, "runner_generation"
                )
            self._record("provision", guard)
            now = datetime.now(UTC)
            db.execute(
                text(
                    "INSERT INTO agent_run.runtime_state "
                    "(materialization_id, operation_id, generation, fencing_token_digest, "
                    "binding_digest, protocol_version, deadline_at, preview_enabled, "
                    "evidence_persisted, side_effects_fenced, "
                    "secret_revoked, destroyed, preview_active, created_at, updated_at) VALUES "
                    "(:materialization_id, :operation_id, :generation, :fence_digest, "
                    ":binding_digest, :protocol_version, :deadline_at, :preview_enabled, "
                    "false, false, false, false, "
                    "false, :now, :now)"
                ),
                {
                    "materialization_id": guard.materialization_id,
                    "operation_id": request.operation_id,
                    "generation": guard.generation,
                    "fence_digest": expected[2],
                    "binding_digest": request.binding.binding_digest,
                    "protocol_version": request.binding.runner_manifest.protocol_version,
                    "deadline_at": request.binding.deadline_at,
                    "preview_enabled": request.binding.boundaries.preview_enabled,
                    "now": now,
                },
            )
        return readiness

    def _state(self, guard: MaterializationGuard) -> _RuntimeState:
        state: _RuntimeState | None = self._states.get(guard.materialization_id)
        if state is None or not self._same_fence(state.guard, guard):
            raise SandboxPolicyViolation(
                DenialCode.STALE_RUNNER_GENERATION,
                "runner_generation",
            )
        return state

    @staticmethod
    def _same_fence(expected: MaterializationGuard, actual: MaterializationGuard) -> bool:
        expected_digest = fencing_token_digest(expected.fencing_token.get_secret_value())
        actual_digest = fencing_token_digest(actual.fencing_token.get_secret_value())
        return (
            expected.materialization_id == actual.materialization_id
            and expected.lease_id == actual.lease_id
            and expected.generation == actual.generation
            and expected_digest == actual_digest
        )

    def publish_preview(
        self,
        operation_id: str,
        guard: MaterializationGuard,
        metadata: EvidenceRef,
        expires_at: datetime,
    ) -> RuntimePreview:
        with self._serialization:
            return self._publish_preview(operation_id, guard, metadata, expires_at)

    def _publish_preview(
        self,
        operation_id: str,
        guard: MaterializationGuard,
        metadata: EvidenceRef,
        expires_at: datetime,
    ) -> RuntimePreview:
        del metadata
        if self._state_engine is not None:
            with self._state_engine.begin() as db:
                row = (
                    db.execute(
                        text(
                            "SELECT generation, fencing_token_digest, preview_enabled, "
                            "side_effects_fenced, destroyed, preview_operation_id, preview_id, "
                            "access_ref, preview_expires_at FROM agent_run.runtime_state "
                            "WHERE materialization_id=:id FOR UPDATE"
                        ),
                        {"id": guard.materialization_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    row is None
                    or row["generation"] != guard.generation
                    or not hmac.compare_digest(
                        row["fencing_token_digest"],
                        fencing_token_digest(guard.fencing_token.get_secret_value()),
                    )
                ):
                    raise SandboxPolicyViolation(
                        DenialCode.STALE_RUNNER_GENERATION,
                        "runner_generation",
                    )
                if row["side_effects_fenced"] or row["destroyed"]:
                    raise SandboxPolicyViolation(
                        DenialCode.RUNTIME_CAPABILITY_DENIED,
                        "preview_fenced",
                    )
                if not row["preview_enabled"]:
                    raise SandboxPolicyViolation(
                        DenialCode.RUNTIME_CAPABILITY_DENIED,
                        "preview",
                    )
                if row["preview_operation_id"] is not None:
                    if str(row["preview_operation_id"]) != operation_id:
                        raise SandboxPolicyViolation(
                            DenialCode.RUNTIME_CAPABILITY_DENIED,
                            "preview_active",
                        )
                    return RuntimePreview(
                        preview_id=row["preview_id"],
                        access_ref=row["access_ref"],
                        expires_at=row["preview_expires_at"],
                    )
                self._record("publish_preview", guard)
                preview = RuntimePreview(
                    preview_id=f"preview:{operation_id}",
                    access_ref=f"preview-access:{operation_id}",
                    expires_at=expires_at,
                )
                db.execute(
                    text(
                        "UPDATE agent_run.runtime_state SET preview_active=true, "
                        "preview_operation_id=:operation_id, preview_id=:preview_id, "
                        "access_ref=:access_ref, preview_expires_at=:expires_at, "
                        "updated_at=:now WHERE materialization_id=:id"
                    ),
                    {
                        "id": guard.materialization_id,
                        "operation_id": operation_id,
                        "preview_id": preview.preview_id,
                        "access_ref": preview.access_ref,
                        "expires_at": preview.expires_at,
                        "now": datetime.now(UTC),
                    },
                )
                return preview
        state = self._state(guard)
        if state.fenced or state.destroyed:
            raise SandboxPolicyViolation(
                DenialCode.RUNTIME_CAPABILITY_DENIED,
                "preview_fenced",
            )
        existing = self._preview_results.get(operation_id)
        if existing is not None:
            return existing
        if state.binding is None:
            raise RuntimeMaterializationError("runtime binding is unavailable")
        if not state.binding.boundaries.preview_enabled:
            raise SandboxPolicyViolation(
                DenialCode.RUNTIME_CAPABILITY_DENIED,
                "preview",
            )
        self._record("publish_preview", guard)
        preview = RuntimePreview(
            preview_id=f"preview:{operation_id}",
            access_ref=f"preview-access:{operation_id}",
            expires_at=expires_at,
        )
        self._preview_results[operation_id] = preview
        return preview

    def persist_evidence(
        self,
        guard: MaterializationGuard,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> None:
        del evidence_refs
        if self._state_engine is not None:
            with self._state_engine.begin() as db:
                row = self._locked_runtime_row(db, guard)
                if row["evidence_persisted"]:
                    return
                self._record("evidence", guard)
                db.execute(
                    text(
                        "UPDATE agent_run.runtime_state SET evidence_persisted=true, "
                        "updated_at=:now WHERE materialization_id=:id"
                    ),
                    {"id": guard.materialization_id, "now": datetime.now(UTC)},
                )
            return
        state = self._state(guard)
        self._record("evidence", guard)
        state.evidence_persisted = True

    def _locked_runtime_row(
        self,
        db: Connection,
        guard: MaterializationGuard,
    ) -> Mapping[str, object]:
        row = (
            db.execute(
                text(
                    "SELECT generation, fencing_token_digest, evidence_persisted, "
                    "side_effects_fenced, secret_revoked, destroyed "
                    "FROM agent_run.runtime_state WHERE materialization_id=:id FOR UPDATE"
                ),
                {"id": guard.materialization_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["generation"] != guard.generation
            or not hmac.compare_digest(
                str(row["fencing_token_digest"]),
                fencing_token_digest(guard.fencing_token.get_secret_value()),
            )
        ):
            raise SandboxPolicyViolation(
                DenialCode.STALE_RUNNER_GENERATION,
                "runner_generation",
            )
        return dict(row)

    def fence(self, guard: MaterializationGuard) -> None:
        with self._serialization:
            self._fence(guard)

    def _fence(self, guard: MaterializationGuard) -> None:
        if self._state_engine is not None:
            with self._state_engine.begin() as db:
                row = self._locked_runtime_row(db, guard)
                if row["side_effects_fenced"]:
                    return
                if not row["evidence_persisted"]:
                    raise DevRuntimeStepFailure("evidence must precede fence")
                self._record("fence", guard)
                db.execute(
                    text(
                        "UPDATE agent_run.runtime_state SET side_effects_fenced=true, "
                        "preview_active=false, updated_at=:now WHERE materialization_id=:id"
                    ),
                    {"id": guard.materialization_id, "now": datetime.now(UTC)},
                )
            return
        state = self._state(guard)
        self._record("fence", guard)
        state.fenced = True

    def revoke_secret(self, guard: MaterializationGuard) -> None:
        if self._state_engine is not None:
            with self._state_engine.begin() as db:
                row = self._locked_runtime_row(db, guard)
                if row["secret_revoked"]:
                    return
                if not row["side_effects_fenced"]:
                    raise DevRuntimeStepFailure("fence must precede secret revocation")
                self._record("revoke_secret", guard)
                db.execute(
                    text(
                        "UPDATE agent_run.runtime_state SET secret_revoked=true, "
                        "updated_at=:now WHERE materialization_id=:id"
                    ),
                    {"id": guard.materialization_id, "now": datetime.now(UTC)},
                )
            return
        state = self._state(guard)
        if not state.fenced:
            raise DevRuntimeStepFailure("fence must precede secret revocation")
        self._record("revoke_secret", guard)
        state.secret_revoked = True

    def destroy(self, guard: MaterializationGuard) -> None:
        if self._state_engine is not None:
            with self._state_engine.begin() as db:
                row = self._locked_runtime_row(db, guard)
                if row["destroyed"]:
                    return
                if not row["secret_revoked"]:
                    raise DevRuntimeStepFailure("secret revocation must precede destroy")
                self._record("destroy", guard)
                db.execute(
                    text(
                        "UPDATE agent_run.runtime_state SET destroyed=true, preview_active=false, "
                        "updated_at=:now WHERE materialization_id=:id"
                    ),
                    {"id": guard.materialization_id, "now": datetime.now(UTC)},
                )
            return
        state = self._state(guard)
        if not state.secret_revoked:
            raise DevRuntimeStepFailure("secret revocation must precede destroy")
        self._record("destroy", guard)
        state.destroyed = True

    def observe(self, materialization_id: str) -> RuntimeObservation:
        if not self._observation_available:
            return RuntimeObservation(
                materialization_id=materialization_id,
                presence=RuntimePresence.UNKNOWN,
            )
        if self._state_engine is not None:
            with self._state_engine.connect() as db:
                row = (
                    db.execute(
                        text(
                            "SELECT generation, evidence_persisted, side_effects_fenced, "
                            "secret_revoked, destroyed, preview_active "
                            "FROM agent_run.runtime_state WHERE materialization_id=:id"
                        ),
                        {"id": materialization_id},
                    )
                    .mappings()
                    .one_or_none()
                )
            if row is None:
                return RuntimeObservation(
                    materialization_id=materialization_id,
                    presence=RuntimePresence.ABSENT,
                )
            return RuntimeObservation(
                materialization_id=materialization_id,
                presence=RuntimePresence.PRESENT,
                generation=row["generation"],
                evidence_persisted=row["evidence_persisted"],
                side_effects_fenced=row["side_effects_fenced"],
                secret_revoked=row["secret_revoked"],
                destroyed=row["destroyed"],
                preview_access_active=row["preview_active"],
            )
        state = self._states.get(materialization_id)
        if state is None:
            return RuntimeObservation(
                materialization_id=materialization_id,
                presence=RuntimePresence.ABSENT,
            )
        return RuntimeObservation(
            materialization_id=materialization_id,
            presence=RuntimePresence.PRESENT,
            generation=state.guard.generation,
            evidence_persisted=state.evidence_persisted,
            side_effects_fenced=state.fenced,
            secret_revoked=state.secret_revoked,
            destroyed=state.destroyed,
            preview_access_active=any(
                result.preview_id.startswith("preview:")
                for result in self._preview_results.values()
            )
            and not state.fenced
            and not state.destroyed,
        )
