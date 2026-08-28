import hashlib
from typing import Any
from uuid import UUID

from control_plane.app.modules.audit import AuditEnvelope, record
from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    requirement_dto,
    sdd_artifact_version_dto,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    CreateSddArtifactResult,
    InvalidRequirementInput,
    RequirementError,
    RequirementNotFound,
    RequirementState,
    SddArtifactNotFound,
    SddArtifactVersionDto,
    StaleBaselineSubject,
    StaleRequirementRevision,
)
from control_plane.app.modules.requirement.ports import RequirementRepository
from control_plane.app.shared.api.request_id import current_request_id
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)

_MAX_SDD_BYTES = 200_000
_MEDIA_TYPE = "text/markdown; charset=utf-8"
_STATE = "AVAILABLE"
_TRUST = "TRUSTED_PLAIN_TEXT"


def normalize_sdd_markdown(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise InvalidRequirementInput("SDD Markdown content is required")
    if len(normalized.encode("utf-8")) > _MAX_SDD_BYTES:
        raise InvalidRequirementInput("SDD Markdown content exceeds 200000 bytes")
    return normalized


def _audit_denial(
    *,
    dependencies: RequirementDependencies,
    actor: str,
    requirement_id: str,
    error: Exception,
) -> None:
    record(
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor,
            actor_type="HUMAN",
            action="requirement.sdd_artifact.create_denied",
            target_type="REQUIREMENT",
            target_id=requirement_id,
            result="DENIED",
            reason=f"reasonCode={type(error).__name__.upper()}",
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.denial_audit,
    )


def _normalized_artifact_id(artifact_id: str) -> str:
    try:
        return str(UUID(artifact_id))
    except (AttributeError, TypeError, ValueError):
        raise InvalidRequirementInput("artifact ID is invalid") from None


def _create_sdd_artifact_once(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    artifact_id: str | None,
    content: str,
    expected_revision: int,
    actor: Any,
    dependencies: RequirementDependencies,
) -> CreateSddArtifactResult:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    if requirement["revision"] != expected_revision:
        raise StaleRequirementRevision(requirement_id)
    if RequirementState(requirement["state"]) is not RequirementState.PREPARING:
        raise StaleBaselineSubject("Requirement is not preparing an SDD Artifact")

    normalized_content = normalize_sdd_markdown(content)
    stable_actor = actor_id(actor)
    if artifact_id is None:
        stable_artifact_id = str(dependencies.random.uuid4())
        version = 1
    else:
        stable_artifact_id = _normalized_artifact_id(artifact_id)
        latest = repository.latest_sdd_artifact_version(
            requirement_id,
            stable_artifact_id,
        )
        if latest is None:
            raise SddArtifactNotFound(stable_artifact_id)
        version = latest["version"] + 1

    now = dependencies.clock.now()
    digest = "sha256:" + hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
    artifact = repository.insert_sdd_artifact_version(
        artifact_id=stable_artifact_id,
        version=version,
        requirement_id=requirement_id,
        sha256=digest,
        state=_STATE,
        media_type=_MEDIA_TYPE,
        trust=_TRUST,
        content=normalized_content,
        created_by=stable_actor,
        now=now,
    )
    updated = repository.update_requirement_state(
        requirement_id,
        expected_revision=expected_revision,
        state=RequirementState.PREPARING.value,
        now=now,
    )
    if updated is None:
        raise StaleRequirementRevision(requirement_id)
    audit(
        repository,
        dependencies=dependencies,
        actor=stable_actor,
        action="requirement.sdd_artifact.created",
        target_type="SDD_ARTIFACT_VERSION",
        target_id=f"{stable_artifact_id}@{version}",
        reason=f"sha256={digest}; requirementRevision={updated['revision']}",
    )
    return CreateSddArtifactResult(
        requirement=requirement_dto(updated),
        artifact=sdd_artifact_version_dto(artifact),
    )


def create_sdd_artifact(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    artifact_id: str | None,
    content: str,
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> CreateSddArtifactResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "requirementId": requirement_id,
        "artifactId": artifact_id,
        "content": content,
        "expectedRevision": expected_revision,
    }
    fingerprint = canonical_request_fingerprint(
        operation="requirement_create_sdd_artifact",
        method="COMMAND",
        path="requirement.create-sdd-artifact",
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )

    def command() -> IdempotentResponse:
        try:
            created = _create_sdd_artifact_once(
                repository,
                requirement_id=requirement_id,
                artifact_id=artifact_id,
                content=content,
                expected_revision=expected_revision,
                actor=actor,
                dependencies=dependencies,
            )
        except RequirementError as error:
            _audit_denial(
                dependencies=dependencies,
                actor=stable_actor,
                requirement_id=requirement_id,
                error=error,
            )
            raise
        return IdempotentResponse(status_code=201, body=created.model_dump(mode="json"))

    try:
        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation="requirement_create_sdd_artifact",
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except IdempotencyConflict as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            requirement_id=requirement_id,
            error=error,
        )
        raise
    return CreateSddArtifactResult.model_validate(execution.response.body)


def get_sdd_artifact(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    artifact_id: str,
    artifact_version: int,
) -> SddArtifactVersionDto:
    if repository.requirement_by_id(requirement_id) is None:
        raise RequirementNotFound(requirement_id)
    row = repository.sdd_artifact_version(
        requirement_id,
        _normalized_artifact_id(artifact_id),
        artifact_version,
    )
    if row is None:
        raise SddArtifactNotFound(f"{artifact_id}@{artifact_version}")
    return sdd_artifact_version_dto(row)
