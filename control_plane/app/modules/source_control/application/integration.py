from typing import Literal

from control_plane.app.modules.source_control.application._integration_callbacks import (
    _complete_effect_block,
    _complete_preflight_block,
    _mark_unknown,
    _record_effect_callback,
    _record_ready,
    _release_pre_effect_transient,
    _replay_processed_request,
    _resolve_atomic_fact_commit,
)
from control_plane.app.modules.source_control.application._integration_common import (
    CREATE_OPERATION as _CREATE_OPERATION,
)
from control_plane.app.modules.source_control.application._integration_common import (
    CREATE_TOPIC as _CREATE_TOPIC,
)
from control_plane.app.modules.source_control.application._integration_common import (
    PREFLIGHT_OUTCOME_REASONS as _PREFLIGHT_OUTCOME_REASONS,
)
from control_plane.app.modules.source_control.application._integration_common import (
    EffectCollision as _EffectCollision,
)
from control_plane.app.modules.source_control.application._integration_common import (
    OriginatingCallbackSubject as _OriginatingCallbackSubject,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProcessIntegrationRequestResult as ProcessIntegrationRequestResult,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProviderPreflightBlocked as _ProviderPreflightBlocked,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProviderPreflightTransient as _ProviderPreflightTransient,
)
from control_plane.app.modules.source_control.application._integration_common import (
    claim_exact_delivery_request as _claim_exact_delivery_request,
)
from control_plane.app.modules.source_control.application._integration_common import (
    effect_dto as _effect_dto,
)
from control_plane.app.modules.source_control.application._integration_merge import (
    _process_integration_merge_request,
)
from control_plane.app.modules.source_control.application._integration_provider import (
    _prove_created_or_adopted_merge_request,
    _ProviderBlocked,
    _ProviderUnknown,
    _read_provider_preflight,
)
from control_plane.app.modules.source_control.application._integration_state import (
    _acquire_in_flight_effect,
    _commit_final_facts,
    _read_admission,
    _validated_effect_payload,
)
from control_plane.app.modules.source_control.application._reasons import stored_reason
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    CreateIntegrationMergeRequestEffectPayload,
    EffectState,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason


def _process_integration_mr_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
    claim_required: bool,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    requirement_delivery = dependencies.requirement_delivery
    requirement_binding = dependencies.requirement
    eligibility = dependencies.eligibility
    gitlab = dependencies.gitlab_merge_requests
    if (
        repository_factory is None
        or requirement_delivery is None
        or requirement_binding is None
        or eligibility is None
        or gitlab is None
        or dependencies.policy is None
    ):
        raise SourceControlDependencyUnavailable("Integration MR dependency unavailable")

    claimed, inbox = _claim_exact_delivery_request(
        message_id,
        expected_topic=_CREATE_TOPIC,
        dependencies=dependencies,
        claim_required=claim_required,
    )

    callback_subject = _OriginatingCallbackSubject(
        work_item_id=str(inbox["work_item_id"]),
        work_item_revision=inbox["work_item_revision"],
    )
    with dependencies.engine.connect() as db:
        local_repository = repository_factory(db)
        branch_row = local_repository.branch_binding_by_work_item(callback_subject.work_item_id)
        existing_effect_row = local_repository.effect_by_operation_subject(
            _CREATE_OPERATION.value,
            f"work-item:{callback_subject.work_item_id}",
        )
    try:
        existing_effect = None if existing_effect_row is None else _effect_dto(existing_effect_row)
    except (TypeError, ValueError):
        existing_effect = None
        existing_payload = None
        local_effect_conflict = True
    else:
        existing_payload = (
            None
            if existing_effect is None or branch_row is None
            else _validated_effect_payload(
                existing_effect,
                subject_key=f"work-item:{callback_subject.work_item_id}",
                requirement_id=str(inbox["requirement_id"]),
                repository_id=str(inbox["repository_id"]),
                request_fingerprint=inbox["payload_hash"],
                branch_binding_id=str(branch_row["id"]),
            )
        )
        local_effect_conflict = existing_effect is not None and existing_payload is None

    persisted_preflight_reason = stored_reason(inbox["last_error_code"])
    if persisted_preflight_reason in _PREFLIGHT_OUTCOME_REASONS:
        if claimed is None:
            if inbox["state"] != "PROCESSED":
                raise RequirementCallbackUnavailable("Delivery request is unavailable")
            return ProcessIntegrationRequestResult(
                effect=None,
                binding=None,
                observation=None,
                blocked_reason=persisted_preflight_reason.value,
            )
        return _complete_preflight_block(
            callback_subject,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=persisted_preflight_reason,
            dependencies=dependencies,
        )

    if local_effect_conflict:
        if claimed is None:
            raise SourceControlDependencyUnavailable("Integration MR effect is invalid")
        return _complete_preflight_block(
            callback_subject,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=SourceControlReason.MR_CONFLICT,
            dependencies=dependencies,
        )

    if claimed is None:
        if inbox["state"] == "PROCESSED" and existing_effect is not None:
            return _replay_processed_request(
                callback_subject,
                dependencies=dependencies,
            )
        raise RequirementCallbackUnavailable("Delivery request is unavailable")

    if existing_effect is not None and existing_effect.state is not EffectState.PLANNED:
        with dependencies.engine.begin() as db:
            completed = repository_factory(db).complete_delivery_request(
                message_id,
                expected_attempts=claimed["attempts"],
                now=dependencies.clock.now(),
            )
            if completed is None:
                raise RequirementCallbackUnavailable("Integration MR inbox lease was lost")
        return _replay_processed_request(
            callback_subject,
            dependencies=dependencies,
        )

    admission = _read_admission(inbox, branch_row, dependencies=dependencies)
    if isinstance(admission, SourceControlReason):
        return _complete_preflight_block(
            callback_subject,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=admission,
            dependencies=dependencies,
        )
    context = admission.context
    try:
        provider_preflight = _read_provider_preflight(admission, gitlab=gitlab)
    except _ProviderPreflightBlocked as blocked:
        return _complete_preflight_block(
            callback_subject,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=blocked.reason_code,
            dependencies=dependencies,
        )
    except _ProviderPreflightTransient:
        _release_pre_effect_transient(
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    source = provider_preflight.source

    if (
        existing_payload is not None
        and existing_effect is not None
        and (existing_payload.head_sha != source.commit_sha)
    ):
        return _complete_effect_block(
            context,
            existing_effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=SourceControlReason.HEAD_SHA_CHANGED,
            dependencies=dependencies,
        )

    payload = (
        existing_payload
        if existing_payload is not None
        else CreateIntegrationMergeRequestEffectPayload(
            branchBindingId=admission.branch_binding_id,
            headSha=source.commit_sha,
        )
    )
    try:
        acquired = _acquire_in_flight_effect(
            admission,
            request_fingerprint=inbox["payload_hash"],
            payload=payload,
            dependencies=dependencies,
        )
    except _EffectCollision:
        return _complete_preflight_block(
            callback_subject,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=SourceControlReason.MR_CONFLICT,
            dependencies=dependencies,
        )
    effect = acquired.effect

    try:
        proof = _prove_created_or_adopted_merge_request(
            admission,
            acquired,
            gitlab=gitlab,
        )
    except _ProviderUnknown:
        return _mark_unknown(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    except _ProviderBlocked as blocked:
        return _complete_effect_block(
            context,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=blocked.reason_code,
            dependencies=dependencies,
        )
    readback = proof.snapshot
    creation_origin = proof.creation_origin

    final_state: Literal[EffectState.SUCCEEDED, EffectState.BLOCKED] = (
        EffectState.SUCCEEDED if readback.state == "opened" else EffectState.BLOCKED
    )
    error_code = {
        "opened": None,
        "closed": SourceControlReason.MR_CLOSED,
        "merged": SourceControlReason.EXTERNAL_MERGE_DRIFT,
    }[readback.state]

    try:
        committed = _commit_final_facts(
            admission,
            effect,
            readback,
            creation_origin=creation_origin,
            final_state=final_state,
            error_code=error_code,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    except RequirementCallbackUnavailable:
        raise
    except Exception:
        return _resolve_atomic_fact_commit(
            callback_subject,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    if readback.state == "opened":
        effect = _record_ready(
            callback_subject,
            committed.effect,
            committed.binding,
            dependencies=dependencies,
        )
    else:
        effect = _record_effect_callback(
            callback_subject,
            committed.effect,
            kind=("blocked" if readback.state == "closed" else "external_drift"),
            binding_id=committed.binding.id,
            reason_code=error_code,
            dependencies=dependencies,
        )
    return ProcessIntegrationRequestResult(
        effect=effect,
        binding=committed.binding,
        observation=committed.observation,
        blocked_reason=None if error_code is None else error_code.value,
    )


def process_integration_mr_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    return _process_integration_mr_request(
        message_id=message_id,
        dependencies=dependencies,
        claim_required=False,
    )


def process_integration_mr_candidate(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    return _process_integration_mr_request(
        message_id=message_id,
        dependencies=dependencies,
        claim_required=True,
    )


def process_integration_merge_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    return _process_integration_merge_request(
        message_id=message_id,
        dependencies=dependencies,
        claim_required=False,
    )


def process_integration_merge_candidate(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    return _process_integration_merge_request(
        message_id=message_id,
        dependencies=dependencies,
        claim_required=True,
    )
