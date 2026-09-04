import re
from typing import Any

from control_plane.app.modules.audit import AuditEnvelope, record
from control_plane.app.modules.requirement.application.common import (
    actor_id,
    audit,
    requirement_dto,
)
from control_plane.app.modules.requirement.application.delivery_set import (
    validate_current_delivery_set,
)
from control_plane.app.modules.requirement.application.dependencies import (
    RequirementDependencies,
)
from control_plane.app.modules.requirement.domain import (
    ArtifactEvidenceReference,
    DeliverySnapshotConflict,
    EvidenceUnavailableOrStale,
    ExternalValidationSubmission,
    FormalDeliveryState,
    IntegrationDeliveryState,
    InvalidRequirementInput,
    RequestIntegrationBaselineResult,
    RequirementDeliverySnapshot,
    RequirementError,
    RequirementNotFound,
    RequirementState,
    StaleRequirementRevision,
    SubmitExternalValidationResult,
    WorkItemNotFound,
    WorkItemState,
)
from control_plane.app.modules.requirement.ports import (
    ArtifactState,
    RequirementRepository,
)
from control_plane.app.shared.api.request_id import current_request_id
from control_plane.app.shared.idempotency import (
    IdempotencyConflict,
    IdempotentResponse,
    canonical_request_fingerprint,
    execute_idempotent,
)
from control_plane.app.shared.security import sanitize_external_reference

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SUBMIT_OPERATION = "requirement_submit_external_validation"
_SUBMIT_PATH = "requirement.submit-external-validation"
_SUBMIT_TOPIC = "requirement.external-validation.submitted"
_BASELINE_OPERATION = "requirement_request_integration_baseline"
_BASELINE_PATH = "requirement.request-integration-baseline"
_BASELINE_TOPIC = "requirement.integration-baseline.requested"


def _audit_denial(
    *,
    dependencies: RequirementDependencies,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    error: Exception,
) -> None:
    record(
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor,
            actor_type="HUMAN",
            action=f"{action}_denied",
            target_type=target_type,
            target_id=target_id,
            result="DENIED",
            reason=f"reasonCode={type(error).__name__.upper()}",
            correlation_id=current_request_id() or str(dependencies.random.uuid4()),
        ),
        dependencies.denial_audit,
    )


def _normalize_commit(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not _COMMIT_SHA.fullmatch(normalized):
        raise InvalidRequirementInput(f"{field} must be an exact commit SHA")
    return normalized


def _normalize_text(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidRequirementInput(f"{field} is required")
    return normalized


def _locked_subject(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str | None = None,
) -> tuple[Any, Any | None]:
    requirement = repository.requirement_by_id(requirement_id, for_update=True)
    if requirement is None:
        raise RequirementNotFound(requirement_id)
    if work_item_id is None:
        return requirement, None
    work_item = repository.work_item_by_id(work_item_id, for_update=True)
    if work_item is None or str(work_item["requirement_id"]) != requirement_id:
        raise WorkItemNotFound(work_item_id)
    return requirement, work_item


def _validated_artifacts(
    *,
    requirement_id: str,
    references: tuple[ArtifactEvidenceReference, ...],
    dependencies: RequirementDependencies,
) -> tuple[ArtifactEvidenceReference, ...]:
    artifacts = dependencies.artifacts
    if artifacts is None:
        raise EvidenceUnavailableOrStale("Artifact reader is unavailable")
    normalized: list[ArtifactEvidenceReference] = []
    for reference in references:
        try:
            snapshot = artifacts.get_snapshot(
                requirement_id,
                reference.artifact_id,
                reference.artifact_version,
            )
        except Exception as error:
            raise EvidenceUnavailableOrStale("Artifact reference is unavailable") from error
        if (
            snapshot.id != reference.artifact_id
            or snapshot.version != reference.artifact_version
            or snapshot.sha256 != reference.artifact_hash
            or snapshot.state is not ArtifactState.AVAILABLE
        ):
            raise EvidenceUnavailableOrStale("Artifact reference is stale or unavailable")
        normalized.append(reference)
    return tuple(sorted(normalized, key=lambda item: (item.artifact_id, item.artifact_version)))


def _normalized_artifact_references(
    references: tuple[ArtifactEvidenceReference, ...],
) -> tuple[ArtifactEvidenceReference, ...]:
    if not references:
        raise InvalidRequirementInput("at least one exact Artifact reference is required")
    identities = tuple((item.artifact_id, item.artifact_version) for item in references)
    if len(set(identities)) != len(identities):
        raise InvalidRequirementInput("duplicate Artifact references are not allowed")
    return tuple(sorted(references, key=lambda item: (item.artifact_id, item.artifact_version)))


def submit_external_validation(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    work_item_id: str,
    target_commit_sha: str,
    integration_merge_commit_sha: str,
    reference: str,
    notes: str,
    artifact_references: tuple[ArtifactEvidenceReference, ...],
    expected_revision: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> SubmitExternalValidationResult:
    stable_actor = actor_id(actor)
    stable_target_commit = _normalize_commit(target_commit_sha, field="target commit")
    stable_merge_commit = _normalize_commit(
        integration_merge_commit_sha,
        field="integration merge commit",
    )
    try:
        stable_reference = sanitize_external_reference(reference)
    except ValueError as error:
        raise InvalidRequirementInput(str(error)) from error
    stable_notes = _normalize_text(notes, field="validation notes")
    stable_artifacts = _normalized_artifact_references(artifact_references)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "artifactReferences": [item.model_dump(mode="json") for item in stable_artifacts],
        "expectedRevision": expected_revision,
        "integrationMergeCommitSha": stable_merge_commit,
        "notes": stable_notes,
        "reference": stable_reference,
        "requirementId": requirement_id,
        "targetCommitSha": stable_target_commit,
        "workItemId": work_item_id,
    }
    fingerprint = canonical_request_fingerprint(
        operation=_SUBMIT_OPERATION,
        method="COMMAND",
        path=_SUBMIT_PATH,
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    try:
        requirement, work_item = _locked_subject(
            repository,
            requirement_id=requirement_id,
            work_item_id=work_item_id,
        )
        existing = repository.idempotency_by_scope(
            stable_actor,
            _SUBMIT_OPERATION,
            idempotency_key,
        )
        if existing is None and requirement["revision"] != expected_revision:
            raise StaleRequirementRevision(requirement_id)
        if work_item is None:
            raise WorkItemNotFound(work_item_id)

        def command() -> IdempotentResponse:
            validated_artifacts = _validated_artifacts(
                requirement_id=requirement_id,
                references=stable_artifacts,
                dependencies=dependencies,
            )
            if RequirementState(requirement["state"]) not in {
                RequirementState.VERIFYING,
                RequirementState.AWAITING_ACCEPTANCE,
                RequirementState.AWAITING_MERGE,
            }:
                raise EvidenceUnavailableOrStale(
                    "Requirement must be VERIFYING or in delivery review"
                )
            if (
                WorkItemState(work_item["state"]) is not WorkItemState.VERIFYING
                or IntegrationDeliveryState(work_item["integration_delivery_state"])
                is not IntegrationDeliveryState.INTEGRATED
                or work_item["integration_merge_request_binding_id"] is None
            ):
                raise EvidenceUnavailableOrStale("WorkItem must be INTEGRATED")
            now = dependencies.clock.now()
            repository.invalidate_current_delivery_evidence(
                requirement_id,
                reason="EXTERNAL_VALIDATION_CHANGED",
                now=now,
            )
            updated = repository.advance_evidence_input(
                requirement_id,
                expected_revision=expected_revision,
                state=RequirementState.VERIFYING.value,
                now=now,
            )
            if updated is None:
                raise StaleRequirementRevision(requirement_id)
            message_id = str(dependencies.random.uuid4())
            submission = ExternalValidationSubmission(
                message_id=message_id,
                requirement_id=requirement_id,
                requirement_version=updated["requirement_version"],
                work_item_id=work_item_id,
                work_item_revision=work_item["revision"],
                repository_id=work_item["repository_id"],
                integration_merge_request_binding_id=str(
                    work_item["integration_merge_request_binding_id"]
                ),
                target_commit_sha=stable_target_commit,
                integration_merge_commit_sha=stable_merge_commit,
                reference=stable_reference,
                notes=stable_notes,
                artifact_references=validated_artifacts,
                submitted_by=stable_actor,
                submitted_at=now,
            )
            repository.insert_outbox(
                id=message_id,
                topic=_SUBMIT_TOPIC,
                aggregate_type="REQUIREMENT",
                aggregate_id=requirement_id,
                aggregate_version=updated["revision"],
                payload={
                    "artifactReferences": [
                        item.model_dump(mode="json") for item in validated_artifacts
                    ],
                    "integrationMergeCommitSha": stable_merge_commit,
                    "integrationMergeRequestBindingId": str(
                        work_item["integration_merge_request_binding_id"]
                    ),
                    "notes": stable_notes,
                    "reference": stable_reference,
                    "repositoryId": work_item["repository_id"],
                    "requirementId": requirement_id,
                    "requirementVersion": updated["requirement_version"],
                    "submittedBy": stable_actor,
                    "targetCommitSha": stable_target_commit,
                    "workItemId": work_item_id,
                    "workItemRevision": work_item["revision"],
                },
                now=now,
            )
            audit(
                repository,
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.external_validation.submitted",
                target_type="WORK_ITEM",
                target_id=work_item_id,
                reason=(
                    f"targetCommitSha={stable_target_commit}; "
                    f"requirementVersion={updated['requirement_version']}; "
                    f"artifactCount={len(validated_artifacts)}"
                ),
            )
            result = SubmitExternalValidationResult(
                requirement=requirement_dto(updated),
                submission=submission,
                outbox_topic=_SUBMIT_TOPIC,
            )
            return IdempotentResponse(status_code=200, body=result.model_dump(mode="json"))

        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_SUBMIT_OPERATION,
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except (RequirementError, IdempotencyConflict) as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.external_validation.submit",
            target_type="WORK_ITEM",
            target_id=work_item_id,
            error=error,
        )
        raise
    return SubmitExternalValidationResult.model_validate(execution.response.body)


def _snapshot_from_row(row: Any) -> RequirementDeliverySnapshot:
    return RequirementDeliverySnapshot(
        id=str(row["id"]),
        requirement_id=str(row["requirement_id"]),
        requirement_version=row["requirement_version"],
        required_work_item_set_version=row["required_work_item_set_version"],
        required_work_item_set_hash=row["required_work_item_set_hash"],
        work_item_ids=tuple(str(value) for value in row["work_item_ids"]),
        snapshot_hash=row["snapshot_hash"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def request_integration_baseline(
    repository: RequirementRepository,
    *,
    requirement_id: str,
    expected_revision: int,
    expected_requirement_version: int,
    actor: Any,
    idempotency_key: str,
    dependencies: RequirementDependencies,
) -> RequestIntegrationBaselineResult:
    stable_actor = actor_id(actor)
    material = dependencies.secret_manager.load()
    body: dict[str, object] = {
        "expectedRequirementVersion": expected_requirement_version,
        "expectedRevision": expected_revision,
        "requirementId": requirement_id,
    }
    fingerprint = canonical_request_fingerprint(
        operation=_BASELINE_OPERATION,
        method="COMMAND",
        path=_BASELINE_PATH,
        body=body,
        idempotency_sealing_key=material.idempotency_sealing_key,
    )
    try:
        requirement, _ = _locked_subject(repository, requirement_id=requirement_id)
        existing = repository.idempotency_by_scope(
            stable_actor,
            _BASELINE_OPERATION,
            idempotency_key,
        )
        if existing is None and requirement["revision"] != expected_revision:
            raise StaleRequirementRevision(requirement_id)

        def command() -> IdempotentResponse:
            if RequirementState(requirement["state"]) is not RequirementState.VERIFYING:
                raise DeliverySnapshotConflict("Requirement must be VERIFYING")
            if requirement["requirement_version"] != expected_requirement_version:
                raise DeliverySnapshotConflict("Requirement Version is stale")
            work_items = repository.work_items(requirement_id)
            validate_current_delivery_set(
                tuple(str(item["id"]) for item in work_items),
                requirement["required_work_item_set_hash"],
            )
            if not work_items or any(
                IntegrationDeliveryState(item["integration_delivery_state"])
                is not IntegrationDeliveryState.INTEGRATED
                or (
                    WorkItemState(item["state"]) is not WorkItemState.VERIFYING
                    and not (
                        WorkItemState(item["state"]) is WorkItemState.COMPLETED
                        and FormalDeliveryState(item["formal_delivery_state"])
                        is FormalDeliveryState.MERGED
                    )
                )
                for item in work_items
            ):
                raise DeliverySnapshotConflict("all required WorkItems must be INTEGRATED")
            now = dependencies.clock.now()
            snapshot = RequirementDeliverySnapshot.create(
                snapshot_id=str(dependencies.random.uuid4()),
                requirement_id=requirement_id,
                requirement_version=expected_requirement_version,
                required_work_item_set_version=requirement["required_work_item_set_version"],
                required_work_item_set_hash=requirement["required_work_item_set_hash"],
                work_item_ids=tuple(str(item["id"]) for item in work_items),
                created_by=stable_actor,
            )
            row = repository.insert_delivery_snapshot(
                id=snapshot.id,
                requirement_id=snapshot.requirement_id,
                requirement_version=snapshot.requirement_version,
                required_work_item_set_version=snapshot.required_work_item_set_version,
                required_work_item_set_hash=snapshot.required_work_item_set_hash,
                work_item_ids=snapshot.work_item_ids,
                snapshot_hash=snapshot.snapshot_hash,
                created_by=stable_actor,
                now=now,
            )
            if row is None:
                raise DeliverySnapshotConflict(
                    "The same Requirement delivery snapshot already exists"
                )
            updated = repository.touch_requirement(
                requirement_id,
                expected_revision=expected_revision,
                now=now,
            )
            if updated is None:
                raise StaleRequirementRevision(requirement_id)
            persisted = _snapshot_from_row(row)
            repository.insert_outbox(
                id=str(dependencies.random.uuid4()),
                topic=_BASELINE_TOPIC,
                aggregate_type="REQUIREMENT",
                aggregate_id=requirement_id,
                aggregate_version=updated["revision"],
                payload={
                    "deliverySnapshotHash": persisted.snapshot_hash,
                    "deliverySnapshotId": persisted.id,
                    "requestedBy": stable_actor,
                    "requiredWorkItemSetHash": persisted.required_work_item_set_hash,
                    "requiredWorkItemSetVersion": persisted.required_work_item_set_version,
                    "requirementId": requirement_id,
                    "requirementVersion": persisted.requirement_version,
                    "workItemIds": list(persisted.work_item_ids),
                },
                now=now,
            )
            audit(
                repository,
                dependencies=dependencies,
                actor=stable_actor,
                action="requirement.integration_baseline.requested",
                target_type="REQUIREMENT_DELIVERY_SNAPSHOT",
                target_id=persisted.id,
                reason=(
                    f"requirementVersion={persisted.requirement_version}; "
                    f"setVersion={persisted.required_work_item_set_version}; "
                    f"workItemCount={len(persisted.work_item_ids)}"
                ),
            )
            result = RequestIntegrationBaselineResult(
                requirement=requirement_dto(updated),
                snapshot=persisted,
                outbox_topic=_BASELINE_TOPIC,
            )
            return IdempotentResponse(status_code=202, body=result.model_dump(mode="json"))

        execution = execute_idempotent(
            repository,
            actor=stable_actor,
            operation=_BASELINE_OPERATION,
            key=idempotency_key,
            fingerprint=fingerprint,
            command=command,
            now=dependencies.clock.now,
            new_id=dependencies.random.uuid4,
            idempotency_sealing_key=material.idempotency_sealing_key,
        )
    except (RequirementError, IdempotencyConflict) as error:
        _audit_denial(
            dependencies=dependencies,
            actor=stable_actor,
            action="requirement.integration_baseline.request",
            target_type="REQUIREMENT",
            target_id=requirement_id,
            error=error,
        )
        raise
    return RequestIntegrationBaselineResult.model_validate(execution.response.body)
