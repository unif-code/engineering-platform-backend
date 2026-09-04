from datetime import timedelta
from typing import Any

from control_plane.app.modules.audit import AuditEnvelope
from control_plane.app.modules.source_control.application._batch_claim import (
    InboxClaimLost,
    InboxProcessingFailed,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    ArtifactReference,
    EvidenceCurrentnessState,
    EvidenceMessageConflict,
    EvidenceStale,
    EvidenceUnavailable,
    ExternalValidationReference,
    ExternalValidationRequestEnvelope,
    IntegrationBaselineEvidence,
    IntegrationBaselineEvidenceCurrentness,
    IntegrationBaselineEvidenceItem,
    IntegrationBaselineEvidenceItemCurrentness,
    IntegrationBaselineRequestEnvelope,
    canonical_evidence_item_hash,
    canonical_external_validation_hash,
    canonical_integration_baseline_hash,
)
from control_plane.app.modules.source_control.ports import (
    SourceControlEvidenceRepository,
)


def _append_audit(
    repository: SourceControlEvidenceRepository,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    dependencies: SourceControlDependencies,
    reason: str,
    result: str = "SUCCESS",
) -> None:
    dependencies.audit.append_in_transaction(
        repository.db,
        AuditEnvelope(
            id=str(dependencies.random.uuid4()),
            occurred_at=dependencies.clock.now(),
            actor=actor,
            actor_type="SYSTEM" if actor.startswith("SYSTEM:") else "HUMAN",
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            reason=reason,
            correlation_id=f"source-control:evidence:{target_id}",
        ),
    )


def _artifact_references(raw: object) -> tuple[ArtifactReference, ...]:
    if not isinstance(raw, list):
        raise EvidenceUnavailable("Artifact references are unavailable")
    return tuple(ArtifactReference.model_validate(item) for item in raw)


def _validation_from_row(row: Any) -> ExternalValidationReference:
    return ExternalValidationReference(
        id=str(row["id"]),
        work_item_id=str(row["work_item_id"]),
        integration_merge_request_binding_id=str(row["integration_merge_request_binding_id"]),
        target_commit_sha=row["target_commit_sha"],
        integration_merge_commit_sha=row["integration_merge_commit_sha"],
        reference=row["reference"],
        notes=row["notes"],
        artifact_references=_artifact_references(row["artifact_references"]),
        submitted_by=row["submitted_by"],
        submitted_at=row["submitted_at"],
    )


def _validate_integration_context(
    row: Any,
    *,
    requirement_id: str,
    work_item_id: str,
    repository_id: str,
    binding_id: str,
    target_commit_sha: str,
    integration_merge_commit_sha: str,
) -> None:
    if row is None:
        raise EvidenceStale("Integration MR binding is unavailable")
    if (
        str(row["requirement_id"]) != requirement_id
        or str(row["work_item_id"]) != work_item_id
        or str(row["repository_id"]) != repository_id
        or str(row["binding_id"]) != binding_id
        or row["kind"] != "INTEGRATION"
    ):
        raise EvidenceStale("Integration MR binding does not match the WorkItem")
    if row["observation_state"] != "MERGED":
        raise EvidenceStale("Integration MR is not proven MERGED")
    if row["binding_head_sha"] != target_commit_sha or row["head_sha"] != target_commit_sha:
        raise EvidenceStale("external validation target commit is stale")
    if row["merge_commit_sha"] != integration_merge_commit_sha:
        raise EvidenceStale("external validation merge commit is stale")


def _validation_from_receipt(
    repository: SourceControlEvidenceRepository,
    envelope: ExternalValidationRequestEnvelope,
    *,
    reference_hash: str,
    request_fingerprint: str,
) -> ExternalValidationReference | None:
    receipt = repository.external_validation_receipt(envelope.message_id)
    if receipt is None:
        return None
    if (
        receipt["request_fingerprint"] != request_fingerprint
        or str(receipt["requirement_id"]) != envelope.requirement_id
        or str(receipt["work_item_id"]) != envelope.work_item_id
    ):
        raise EvidenceMessageConflict(envelope.message_id)
    if receipt["outcome"] == "REJECTED":
        if (
            receipt["canonical_external_validation_id"] is not None
            or receipt["rejection_reason_code"] != "EVIDENCE_STALE"
        ):
            raise EvidenceMessageConflict(envelope.message_id)
        raise EvidenceStale("external validation request was previously rejected")
    if receipt["outcome"] != "ACCEPTED":
        raise EvidenceMessageConflict(envelope.message_id)
    canonical = repository.external_validation_by_id(
        str(receipt["canonical_external_validation_id"])
    )
    if (
        canonical is None
        or canonical["reference_hash"] != reference_hash
        or str(canonical["requirement_id"]) != envelope.requirement_id
        or str(canonical["work_item_id"]) != envelope.work_item_id
    ):
        raise EvidenceMessageConflict(envelope.message_id)
    return _validation_from_row(canonical)


def _persist_external_validation_receipt(
    repository: SourceControlEvidenceRepository,
    envelope: ExternalValidationRequestEnvelope,
    *,
    canonical: Any,
    reference_hash: str,
    request_fingerprint: str,
    dependencies: SourceControlDependencies,
) -> ExternalValidationReference:
    if (
        canonical["reference_hash"] != reference_hash
        or str(canonical["requirement_id"]) != envelope.requirement_id
        or str(canonical["work_item_id"]) != envelope.work_item_id
    ):
        raise EvidenceMessageConflict(envelope.message_id)
    inserted = repository.insert_external_validation_receipt(
        message_id=envelope.message_id,
        request_fingerprint=request_fingerprint,
        outcome="ACCEPTED",
        canonical_external_validation_id=str(canonical["id"]),
        requirement_id=envelope.requirement_id,
        work_item_id=envelope.work_item_id,
        rejection_reason_code=None,
        received_at=dependencies.clock.now(),
    )
    if inserted is None:
        replayed = _validation_from_receipt(
            repository,
            envelope,
            reference_hash=reference_hash,
            request_fingerprint=request_fingerprint,
        )
        if replayed is None:
            raise EvidenceMessageConflict(envelope.message_id)
        return replayed
    if str(canonical["id"]) != envelope.message_id:
        _append_audit(
            repository,
            actor=envelope.submitted_by,
            action="source_control.evidence.external_validation_replayed",
            target_type="external_validation_receipt",
            target_id=envelope.message_id,
            dependencies=dependencies,
            reason=(
                f"canonicalExternalValidationId={canonical['id']}; "
                f"workItemId={envelope.work_item_id}"
            ),
        )
    return _validation_from_row(canonical)


def record_external_validation_rejection(
    repository: SourceControlEvidenceRepository,
    envelope: ExternalValidationRequestEnvelope,
    *,
    dependencies: SourceControlDependencies,
) -> None:
    request_fingerprint = envelope.request_fingerprint
    existing = repository.external_validation_receipt(envelope.message_id)
    if existing is not None:
        if (
            existing["request_fingerprint"] != request_fingerprint
            or str(existing["requirement_id"]) != envelope.requirement_id
            or str(existing["work_item_id"]) != envelope.work_item_id
            or existing["outcome"] != "REJECTED"
            or existing["canonical_external_validation_id"] is not None
            or existing["rejection_reason_code"] != "EVIDENCE_STALE"
        ):
            raise EvidenceMessageConflict(envelope.message_id)
        return
    inserted = repository.insert_external_validation_receipt(
        message_id=envelope.message_id,
        request_fingerprint=request_fingerprint,
        outcome="REJECTED",
        canonical_external_validation_id=None,
        requirement_id=envelope.requirement_id,
        work_item_id=envelope.work_item_id,
        rejection_reason_code="EVIDENCE_STALE",
        received_at=dependencies.clock.now(),
    )
    if inserted is None:
        existing = repository.external_validation_receipt(envelope.message_id)
        if (
            existing is None
            or existing["request_fingerprint"] != request_fingerprint
            or str(existing["requirement_id"]) != envelope.requirement_id
            or str(existing["work_item_id"]) != envelope.work_item_id
            or existing["outcome"] != "REJECTED"
            or existing["canonical_external_validation_id"] is not None
            or existing["rejection_reason_code"] != "EVIDENCE_STALE"
        ):
            raise EvidenceMessageConflict(envelope.message_id)
        return
    _append_audit(
        repository,
        actor=envelope.submitted_by,
        action="source_control.evidence.external_validation_rejected",
        target_type="external_validation_request",
        target_id=envelope.message_id,
        dependencies=dependencies,
        reason=(
            f"reasonCode=EVIDENCE_STALE; workItemId={envelope.work_item_id}; "
            f"bindingId={envelope.integration_merge_request_binding_id}"
        ),
        result="DENIED",
    )


def accept_external_validation(
    repository: SourceControlEvidenceRepository,
    envelope: ExternalValidationRequestEnvelope,
    *,
    dependencies: SourceControlDependencies,
) -> ExternalValidationReference:
    stable_artifacts = tuple(
        sorted(
            envelope.artifact_references,
            key=lambda item: (item.artifact_id, item.artifact_version),
        )
    )
    validation = ExternalValidationReference(
        id=envelope.message_id,
        work_item_id=envelope.work_item_id,
        integration_merge_request_binding_id=envelope.integration_merge_request_binding_id,
        target_commit_sha=envelope.target_commit_sha,
        integration_merge_commit_sha=envelope.integration_merge_commit_sha,
        reference=envelope.reference,
        notes=envelope.notes,
        artifact_references=stable_artifacts,
        submitted_by=envelope.submitted_by,
        submitted_at=envelope.submitted_at,
    )
    reference_hash = canonical_external_validation_hash(validation)
    request_fingerprint = envelope.request_fingerprint
    replayed = _validation_from_receipt(
        repository,
        envelope,
        reference_hash=reference_hash,
        request_fingerprint=request_fingerprint,
    )
    if replayed is not None:
        return replayed
    existing = repository.external_validation_by_id(envelope.message_id)
    if existing is not None:
        if (
            existing["reference_hash"] != reference_hash
            or existing["request_fingerprint"] != request_fingerprint
        ):
            raise EvidenceMessageConflict(envelope.message_id)
        return _persist_external_validation_receipt(
            repository,
            envelope,
            canonical=existing,
            reference_hash=reference_hash,
            request_fingerprint=request_fingerprint,
            dependencies=dependencies,
        )
    context = repository.integration_evidence_context(envelope.work_item_id)
    _validate_integration_context(
        context,
        requirement_id=envelope.requirement_id,
        work_item_id=envelope.work_item_id,
        repository_id=envelope.repository_id,
        binding_id=envelope.integration_merge_request_binding_id,
        target_commit_sha=envelope.target_commit_sha,
        integration_merge_commit_sha=envelope.integration_merge_commit_sha,
    )
    inserted = repository.insert_external_validation(
        id=envelope.message_id,
        work_item_id=envelope.work_item_id,
        requirement_id=envelope.requirement_id,
        workspace_id=str(context["workspace_id"]),
        integration_merge_request_binding_id=envelope.integration_merge_request_binding_id,
        target_commit_sha=envelope.target_commit_sha,
        integration_merge_commit_sha=envelope.integration_merge_commit_sha,
        reference=validation.reference,
        notes=validation.notes,
        artifact_references=[item.model_dump(mode="json") for item in stable_artifacts],
        reference_hash=reference_hash,
        request_fingerprint=request_fingerprint,
        submitted_by=envelope.submitted_by,
        submitted_at=envelope.submitted_at,
    )
    if inserted is None:
        existing = repository.external_validation_by_id(envelope.message_id)
        if existing is not None:
            if (
                existing["reference_hash"] != reference_hash
                or existing["request_fingerprint"] != request_fingerprint
            ):
                raise EvidenceMessageConflict(envelope.message_id)
            return _persist_external_validation_receipt(
                repository,
                envelope,
                canonical=existing,
                reference_hash=reference_hash,
                request_fingerprint=request_fingerprint,
                dependencies=dependencies,
            )
        existing = repository.external_validation_by_hash(
            work_item_id=envelope.work_item_id,
            reference_hash=reference_hash,
        )
        if existing is None or existing["reference_hash"] != reference_hash:
            raise EvidenceMessageConflict(envelope.message_id)
        return _persist_external_validation_receipt(
            repository,
            envelope,
            canonical=existing,
            reference_hash=reference_hash,
            request_fingerprint=request_fingerprint,
            dependencies=dependencies,
        )
    _append_audit(
        repository,
        actor=envelope.submitted_by,
        action="source_control.evidence.external_validation_accepted",
        target_type="external_validation_reference",
        target_id=envelope.message_id,
        dependencies=dependencies,
        reason=(
            f"workItemId={envelope.work_item_id}; "
            f"targetCommitSha={envelope.target_commit_sha}; "
            f"artifactCount={len(stable_artifacts)}"
        ),
    )
    return _persist_external_validation_receipt(
        repository,
        envelope,
        canonical=inserted,
        reference_hash=reference_hash,
        request_fingerprint=request_fingerprint,
        dependencies=dependencies,
    )


def _request_matches(row: Any, envelope: IntegrationBaselineRequestEnvelope) -> bool:
    return (
        row["payload_hash"] == envelope.payload_hash
        and str(row["delivery_snapshot_id"]) == envelope.delivery_snapshot_id
        and row["delivery_snapshot_hash"] == envelope.delivery_snapshot_hash
        and str(row["requirement_id"]) == envelope.requirement_id
        and row["requirement_version"] == envelope.requirement_version
        and row["required_work_item_set_version"] == envelope.required_work_item_set_version
        and row["required_work_item_set_hash"] == envelope.required_work_item_set_hash
        and tuple(str(value) for value in row["work_item_ids"]) == envelope.work_item_ids
    )


def accept_integration_baseline_request(
    repository: SourceControlEvidenceRepository,
    envelope: IntegrationBaselineRequestEnvelope,
    *,
    dependencies: SourceControlDependencies,
) -> bool:
    existing = repository.evidence_request(envelope.message_id, for_update=True)
    if existing is not None:
        if not _request_matches(existing, envelope):
            raise EvidenceMessageConflict(envelope.message_id)
        return True
    inserted = repository.insert_evidence_request(
        message_id=envelope.message_id,
        payload_hash=envelope.payload_hash,
        delivery_snapshot_id=envelope.delivery_snapshot_id,
        delivery_snapshot_hash=envelope.delivery_snapshot_hash,
        requirement_id=envelope.requirement_id,
        requirement_version=envelope.requirement_version,
        required_work_item_set_version=envelope.required_work_item_set_version,
        required_work_item_set_hash=envelope.required_work_item_set_hash,
        work_item_ids=list(envelope.work_item_ids),
        now=dependencies.clock.now(),
    )
    if inserted is None:
        existing = repository.evidence_request(envelope.message_id, for_update=True)
        if existing is None or not _request_matches(existing, envelope):
            raise EvidenceMessageConflict(envelope.message_id)
        return True
    _append_audit(
        repository,
        actor=envelope.requested_by,
        action="source_control.evidence.request_accepted",
        target_type="evidence_request",
        target_id=envelope.message_id,
        dependencies=dependencies,
        reason=(
            f"deliverySnapshotId={envelope.delivery_snapshot_id}; "
            f"workItemCount={len(envelope.work_item_ids)}"
        ),
    )
    return True


def _item_from_context(
    repository: SourceControlEvidenceRepository,
    *,
    request: Any,
    work_item_id: str,
) -> IntegrationBaselineEvidenceItem:
    context = repository.integration_evidence_context(work_item_id)
    if context is None:
        raise EvidenceUnavailable(f"WorkItem evidence is unavailable: {work_item_id}")
    if str(context["requirement_id"]) != str(request["requirement_id"]):
        raise EvidenceStale("Evidence WorkItem belongs to another Requirement")
    if (
        context["observation_state"] != "MERGED"
        or context["merge_commit_sha"] is None
        or context["head_sha"] != context["binding_head_sha"]
    ):
        raise EvidenceStale("Integration MR proof is stale")
    validation_row = repository.latest_external_validation(
        work_item_id=work_item_id,
        binding_id=str(context["binding_id"]),
        target_commit_sha=context["head_sha"],
        integration_merge_commit_sha=context["merge_commit_sha"],
    )
    if validation_row is None:
        raise EvidenceUnavailable(f"external validation is unavailable: {work_item_id}")
    validation = _validation_from_row(validation_row)
    artifacts = tuple(
        sorted(
            validation.artifact_references,
            key=lambda item: (item.artifact_id, item.artifact_version),
        )
    )
    return IntegrationBaselineEvidenceItem(
        work_item_id=work_item_id,
        repository_id=str(context["repository_id"]),
        task_branch=context["task_branch"],
        task_commit_sha=context["head_sha"],
        integration_merge_request_binding_id=str(context["binding_id"]),
        integration_merge_request_iid=context["merge_request_iid"],
        integration_merge_commit_sha=context["merge_commit_sha"],
        executor_type="HUMAN",
        executor_id=validation.submitted_by,
        artifact_references=artifacts,
        external_validation=validation,
    )


def _evidence_from_rows(
    repository: SourceControlEvidenceRepository,
    header: Any,
    item_rows: list[Any],
) -> IntegrationBaselineEvidence:
    items: list[IntegrationBaselineEvidenceItem] = []
    for row in item_rows:
        validation = ExternalValidationReference(
            id=str(row["external_validation_reference_id"]),
            work_item_id=str(row["work_item_id"]),
            integration_merge_request_binding_id=str(row["integration_merge_request_binding_id"]),
            target_commit_sha=row["task_commit_sha"],
            integration_merge_commit_sha=row["integration_merge_commit_sha"],
            reference=row["reference"],
            notes=row["notes"],
            artifact_references=_artifact_references(row["artifact_references"]),
            submitted_by=row["submitted_by"],
            submitted_at=row["submitted_at"],
        )
        items.append(
            IntegrationBaselineEvidenceItem(
                work_item_id=str(row["work_item_id"]),
                repository_id=str(row["repository_id"]),
                task_branch=row["task_branch"],
                task_commit_sha=row["task_commit_sha"],
                integration_merge_request_binding_id=str(
                    row["integration_merge_request_binding_id"]
                ),
                integration_merge_request_iid=row["integration_merge_request_iid"],
                integration_merge_commit_sha=row["integration_merge_commit_sha"],
                executor_type=row["executor_type"],
                executor_id=row["executor_id"],
                artifact_references=validation.artifact_references,
                external_validation=validation,
            )
        )
    currentness_items: list[IntegrationBaselineEvidenceItemCurrentness] = []
    for item in items:
        context = repository.integration_evidence_context(item.work_item_id)
        stale_reasons: list[str] = []
        unavailable_reasons: list[str] = []
        latest_validation: Any | None = None
        if context is None:
            unavailable_reasons.append("BINDING_UNAVAILABLE")
        else:
            if str(context["binding_id"]) != item.integration_merge_request_binding_id:
                stale_reasons.append("BINDING_CHANGED")
            if str(context["repository_id"]) != item.repository_id:
                stale_reasons.append("REPOSITORY_CHANGED")
            if context["binding_head_sha"] != item.task_commit_sha:
                stale_reasons.append("BINDING_HEAD_CHANGED")
            if context["head_sha"] is None:
                unavailable_reasons.append("OBSERVATION_UNAVAILABLE")
            else:
                if context["observation_state"] != "MERGED":
                    stale_reasons.append("OBSERVATION_NOT_MERGED")
                if context["head_sha"] != item.task_commit_sha:
                    stale_reasons.append("OBSERVATION_HEAD_CHANGED")
                if context["merge_commit_sha"] != item.integration_merge_commit_sha:
                    stale_reasons.append("OBSERVATION_MERGE_COMMIT_CHANGED")
                if context["merge_commit_sha"] is not None:
                    latest_validation = repository.latest_external_validation(
                        work_item_id=item.work_item_id,
                        binding_id=str(context["binding_id"]),
                        target_commit_sha=context["head_sha"],
                        integration_merge_commit_sha=context["merge_commit_sha"],
                    )
                    if latest_validation is None:
                        unavailable_reasons.append("VALIDATION_UNAVAILABLE")
                    elif str(latest_validation["id"]) != item.external_validation.id:
                        stale_reasons.append("VALIDATION_CHANGED")
        state = (
            EvidenceCurrentnessState.STALE
            if stale_reasons
            else EvidenceCurrentnessState.UNAVAILABLE
            if unavailable_reasons
            else EvidenceCurrentnessState.CURRENT
        )
        currentness_items.append(
            IntegrationBaselineEvidenceItemCurrentness(
                work_item_id=item.work_item_id,
                binding_id=item.integration_merge_request_binding_id,
                current_binding_id=(None if context is None else str(context["binding_id"])),
                evidence_head_sha=item.task_commit_sha,
                binding_head_sha=None if context is None else context["binding_head_sha"],
                latest_observation_head_sha=None if context is None else context["head_sha"],
                evidence_merge_commit_sha=item.integration_merge_commit_sha,
                latest_observation_merge_commit_sha=(
                    None if context is None else context["merge_commit_sha"]
                ),
                external_validation_reference_id=item.external_validation.id,
                latest_external_validation_reference_id=(
                    None if latest_validation is None else str(latest_validation["id"])
                ),
                state=state,
                reasons=tuple(stale_reasons + unavailable_reasons),
            )
        )
    overall_state = (
        EvidenceCurrentnessState.STALE
        if any(item.state is EvidenceCurrentnessState.STALE for item in currentness_items)
        else EvidenceCurrentnessState.UNAVAILABLE
        if any(item.state is EvidenceCurrentnessState.UNAVAILABLE for item in currentness_items)
        else EvidenceCurrentnessState.CURRENT
    )
    return IntegrationBaselineEvidence(
        id=str(header["id"]),
        delivery_snapshot_id=str(header["delivery_snapshot_id"]),
        delivery_snapshot_hash=header["delivery_snapshot_hash"],
        requirement_id=str(header["requirement_id"]),
        requirement_version=header["requirement_version"],
        required_work_item_set_version=header["required_work_item_set_version"],
        required_work_item_set_hash=header["required_work_item_set_hash"],
        evidence_hash=header["evidence_hash"],
        items=tuple(items),
        currentness=IntegrationBaselineEvidenceCurrentness(
            state=overall_state,
            items=tuple(currentness_items),
        ),
        generated_by=header["generated_by"],
        generated_at=header["generated_at"],
    )


def get_integration_baseline_evidence(
    repository: SourceControlEvidenceRepository,
    *,
    evidence_id: str,
) -> IntegrationBaselineEvidence:
    header = repository.integration_baseline_evidence_by_id(evidence_id)
    if header is None:
        raise EvidenceUnavailable(evidence_id)
    return _evidence_from_rows(
        repository,
        header,
        repository.integration_baseline_evidence_items(evidence_id),
    )


def get_integration_baseline_evidence_by_snapshot(
    repository: SourceControlEvidenceRepository,
    *,
    delivery_snapshot_id: str,
    delivery_snapshot_hash: str,
) -> IntegrationBaselineEvidence:
    header = repository.integration_baseline_evidence_by_snapshot(
        delivery_snapshot_id, delivery_snapshot_hash
    )
    if header is None:
        raise EvidenceUnavailable(delivery_snapshot_id)
    return _evidence_from_rows(
        repository,
        header,
        repository.integration_baseline_evidence_items(str(header["id"])),
    )


def process_integration_baseline_request(
    repository: SourceControlEvidenceRepository,
    *,
    message_id: str,
    generated_by: str,
    dependencies: SourceControlDependencies,
) -> IntegrationBaselineEvidence:
    return _process_integration_baseline_request(
        repository,
        message_id=message_id,
        generated_by=generated_by,
        dependencies=dependencies,
        claim_required=False,
    )


def process_integration_baseline_candidate(
    repository: SourceControlEvidenceRepository,
    *,
    message_id: str,
    generated_by: str,
    dependencies: SourceControlDependencies,
) -> IntegrationBaselineEvidence:
    return _process_integration_baseline_request(
        repository,
        message_id=message_id,
        generated_by=generated_by,
        dependencies=dependencies,
        claim_required=True,
    )


def _process_integration_baseline_request(
    repository: SourceControlEvidenceRepository,
    *,
    message_id: str,
    generated_by: str,
    dependencies: SourceControlDependencies,
    claim_required: bool,
) -> IntegrationBaselineEvidence:
    request = repository.evidence_request(message_id, for_update=True)
    if request is None:
        raise EvidenceUnavailable(message_id)
    if claim_required and request["state"] == "PROCESSED":
        raise InboxClaimLost(message_id)
    existing = repository.integration_baseline_evidence_by_snapshot(
        str(request["delivery_snapshot_id"]),
        request["delivery_snapshot_hash"],
    )
    if request["state"] == "PROCESSED":
        if existing is None:
            raise EvidenceUnavailable("processed Evidence request has no Evidence")
        return _evidence_from_rows(
            repository,
            existing,
            repository.integration_baseline_evidence_items(str(existing["id"])),
        )
    now = dependencies.clock.now()
    claimed = repository.claim_evidence_request(
        message_id,
        now=now,
        lease_until=now + timedelta(minutes=2),
    )
    if claimed is None:
        if claim_required:
            raise InboxClaimLost(message_id)
        raise EvidenceUnavailable("Evidence request lease is unavailable")
    try:
        return _generate_claimed_integration_baseline(
            repository,
            message_id=message_id,
            claimed=claimed,
            generated_by=generated_by,
            dependencies=dependencies,
        )
    except Exception as error:
        if claim_required:
            raise InboxProcessingFailed(error, expected_attempts=claimed["attempts"]) from error
        raise


def _generate_claimed_integration_baseline(
    repository: SourceControlEvidenceRepository,
    *,
    message_id: str,
    claimed: Any,
    generated_by: str,
    dependencies: SourceControlDependencies,
) -> IntegrationBaselineEvidence:
    work_item_ids = tuple(str(value) for value in claimed["work_item_ids"])
    if work_item_ids != tuple(sorted(work_item_ids)) or len(set(work_item_ids)) != len(
        work_item_ids
    ):
        raise EvidenceStale("Evidence request WorkItem set is not canonical")
    items = tuple(
        _item_from_context(repository, request=claimed, work_item_id=work_item_id)
        for work_item_id in work_item_ids
    )
    evidence_id = str(dependencies.random.uuid4())
    evidence_hash = canonical_integration_baseline_hash(
        integration_baseline_id=evidence_id,
        requirement_id=str(claimed["requirement_id"]),
        requirement_version=claimed["requirement_version"],
        required_work_item_set_version=claimed["required_work_item_set_version"],
        required_work_item_set_hash=claimed["required_work_item_set_hash"],
        delivery_snapshot_id=str(claimed["delivery_snapshot_id"]),
        delivery_snapshot_hash=claimed["delivery_snapshot_hash"],
        items=items,
    )
    now = dependencies.clock.now()
    inserted = repository.insert_integration_baseline_evidence(
        id=evidence_id,
        delivery_snapshot_id=str(claimed["delivery_snapshot_id"]),
        delivery_snapshot_hash=claimed["delivery_snapshot_hash"],
        requirement_id=str(claimed["requirement_id"]),
        requirement_version=claimed["requirement_version"],
        required_work_item_set_version=claimed["required_work_item_set_version"],
        required_work_item_set_hash=claimed["required_work_item_set_hash"],
        evidence_hash=evidence_hash,
        generated_by=generated_by.strip(),
        generated_at=now,
    )
    if inserted is None:
        existing = repository.integration_baseline_evidence_by_snapshot(
            str(claimed["delivery_snapshot_id"]),
            claimed["delivery_snapshot_hash"],
        )
        if existing is None:
            raise EvidenceMessageConflict(message_id)
        completed = repository.complete_evidence_request(
            message_id,
            expected_attempts=claimed["attempts"],
            now=now,
        )
        if completed is None:
            raise EvidenceUnavailable("Evidence request lease was lost")
        return _evidence_from_rows(
            repository,
            existing,
            repository.integration_baseline_evidence_items(str(existing["id"])),
        )
    for item in items:
        repository.insert_integration_baseline_evidence_item(
            evidence_id=evidence_id,
            requirement_id=str(claimed["requirement_id"]),
            work_item_id=item.work_item_id,
            repository_id=item.repository_id,
            task_branch=item.task_branch,
            task_commit_sha=item.task_commit_sha,
            integration_merge_request_binding_id=(item.integration_merge_request_binding_id),
            integration_merge_request_iid=item.integration_merge_request_iid,
            integration_merge_commit_sha=item.integration_merge_commit_sha,
            executor_type=item.executor_type,
            executor_id=item.executor_id,
            artifact_references=[
                artifact.model_dump(mode="json") for artifact in item.artifact_references
            ],
            external_validation_reference_id=item.external_validation.id,
            item_hash=canonical_evidence_item_hash(item),
        )
    completed = repository.complete_evidence_request(
        message_id,
        expected_attempts=claimed["attempts"],
        now=now,
    )
    if completed is None:
        raise EvidenceUnavailable("Evidence request lease was lost")
    _append_audit(
        repository,
        actor=generated_by,
        action="source_control.evidence.generated",
        target_type="integration_baseline_evidence",
        target_id=evidence_id,
        dependencies=dependencies,
        reason=(
            f"deliverySnapshotId={claimed['delivery_snapshot_id']}; workItemCount={len(items)}"
        ),
    )
    return get_integration_baseline_evidence(repository, evidence_id=evidence_id)
