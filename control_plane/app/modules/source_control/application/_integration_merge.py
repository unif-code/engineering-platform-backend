from control_plane.app.modules.source_control.application._integration_callbacks import (
    _release_pre_effect_transient,
)
from control_plane.app.modules.source_control.application._integration_common import (
    MERGE_TOPIC as _MERGE_TOPIC,
)
from control_plane.app.modules.source_control.application._integration_common import (
    EffectCollision as _EffectCollision,
)
from control_plane.app.modules.source_control.application._integration_common import (
    ProcessIntegrationRequestResult,
)
from control_plane.app.modules.source_control.application._integration_common import (
    claim_exact_delivery_request as _claim_exact_delivery_request,
)
from control_plane.app.modules.source_control.application._integration_merge_completion import (
    _commit_external_merge_drift,
    _complete_merge_effect_block,
    _complete_merge_preflight_block,
    _complete_merge_success,
    _mark_merge_unknown,
    _replay_merge_effect,
    _replay_merge_preflight_callback,
)
from control_plane.app.modules.source_control.application._integration_merge_context import (
    _read_merge_admission,
    _read_stored_merge_facts,
)
from control_plane.app.modules.source_control.application._integration_merge_provider import (
    _merge_exact_head,
    _MergeExecutionBlocked,
    _MergeExecutionUnknown,
    _MergePreflightBlocked,
    _MergePreflightTransient,
    _provider_block_reason,
    _read_merge_provider_proof,
)
from control_plane.app.modules.source_control.application._integration_merge_state import (
    _acquire_merge_effect,
    _classify_current_merge_effect,
    _validated_merge_effect,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectState,
    MergeIntegrationMergeRequestEffectPayload,
    RequirementCallbackUnavailable,
    SourceControlDependencyUnavailable,
)

_MERGE_PREFLIGHT_OUTCOME_REASONS = frozenset(
    {
        "BRANCH_BINDING_MISSING",
        "EXTERNAL_MERGE_DRIFT",
        "HEAD_SHA_CHANGED",
        "MERGE_ACTOR_INELIGIBLE",
        "MERGE_CONFLICT",
        "MR_CHECKS_BLOCKED",
        "MR_CLOSED",
        "MR_CONFLICT",
        "OWNER_MISMATCH",
        "PROJECT_PROFILE_UNSUPPORTED",
        "REPOSITORY_NOT_AUTHORIZED",
        "TARGET_BRANCH_NOT_FOUND",
        "TARGET_BRANCH_NOT_PROTECTED",
    }
)


def _process_integration_merge_request(
    *,
    message_id: str,
    dependencies: SourceControlDependencies,
) -> ProcessIntegrationRequestResult:
    repository_factory = dependencies.delivery_repository_factory
    if (
        repository_factory is None
        or dependencies.requirement_delivery is None
        or dependencies.requirement is None
        or dependencies.eligibility is None
        or dependencies.gitlab_merge_requests is None
        or dependencies.policy is None
    ):
        raise SourceControlDependencyUnavailable("Integration merge dependency unavailable")
    claimed, inbox = _claim_exact_delivery_request(
        message_id,
        expected_topic=_MERGE_TOPIC,
        dependencies=dependencies,
    )
    persisted_preflight_reason = inbox["last_error_code"]
    if persisted_preflight_reason in _MERGE_PREFLIGHT_OUTCOME_REASONS:
        preflight_facts = _read_stored_merge_facts(
            inbox,
            include_effect=False,
            dependencies=dependencies,
        )
        if claimed is None:
            if inbox["state"] != "PROCESSED":
                raise RequirementCallbackUnavailable("Delivery request is unavailable")
            return ProcessIntegrationRequestResult(
                effect=None,
                binding=preflight_facts.binding,
                observation=preflight_facts.observation,
                blocked_reason=persisted_preflight_reason,
            )
        return _replay_merge_preflight_callback(
            inbox,
            preflight_facts,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=persisted_preflight_reason,
            dependencies=dependencies,
        )
    try:
        stored_facts = _read_stored_merge_facts(inbox, dependencies=dependencies)
    except _EffectCollision:
        if claimed is None:
            raise RequirementCallbackUnavailable("Delivery request is unavailable") from None
        admission = _read_merge_admission(inbox, dependencies=dependencies)
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MR_CONFLICT",
            dependencies=dependencies,
        )
    if stored_facts.effect is not None and stored_facts.effect.state is not EffectState.PLANNED:
        if claimed is None and inbox["state"] != "PROCESSED":
            raise RequirementCallbackUnavailable("Delivery request is unavailable")
        return _replay_merge_effect(
            inbox,
            stored_facts,
            claimed_attempts=None if claimed is None else claimed["attempts"],
            dependencies=dependencies,
        )
    if claimed is None:
        if inbox["state"] == "PROCESSED":
            return ProcessIntegrationRequestResult(
                effect=None,
                binding=stored_facts.binding,
                observation=stored_facts.observation,
                blocked_reason=inbox["last_error_code"],
            )
        raise RequirementCallbackUnavailable("Delivery request is unavailable")

    admission = _read_merge_admission(inbox, dependencies=dependencies)
    if admission.blocked_reason is not None:
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=admission.blocked_reason,
            dependencies=dependencies,
        )
    try:
        proof = _read_merge_provider_proof(admission, dependencies=dependencies)
    except _MergePreflightBlocked as blocked:
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=blocked.reason_code,
            dependencies=dependencies,
        )
    except _MergePreflightTransient:
        _release_pre_effect_transient(
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    try:
        existing_effect = _classify_current_merge_effect(
            inbox,
            admission,
            proof.current_head_sha,
            stored_facts.effect,
            dependencies=dependencies,
        )
    except _EffectCollision:
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MR_CONFLICT",
            dependencies=dependencies,
        )
    if existing_effect is not None:
        existing_payload = _validated_merge_effect(
            existing_effect,
            admission=admission,
            request_fingerprint=inbox["payload_hash"],
        )
        assert existing_payload is not None
        if existing_effect.state is not EffectState.PLANNED:
            raise RequirementCallbackUnavailable("Integration merge effect is not writable")
        if existing_payload.requested_head_sha != proof.current_head_sha:
            return _complete_merge_effect_block(
                admission,
                existing_effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                reason_code="HEAD_SHA_CHANGED",
                readback=(proof.merge_request if proof.merge_request.state == "merged" else None),
                dependencies=dependencies,
            )
    provider_reason = _provider_block_reason(proof)
    if (
        provider_reason == "EXTERNAL_MERGE_DRIFT"
        and proof.source is None
        and existing_effect is not None
    ):
        provider_reason = "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION"
    if provider_reason is not None:
        if existing_effect is not None:
            return _complete_merge_effect_block(
                admission,
                existing_effect,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                reason_code=provider_reason,
                readback=(
                    proof.merge_request
                    if provider_reason
                    in {
                        "EXTERNAL_MERGE_DRIFT",
                        "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION",
                    }
                    else None
                ),
                dependencies=dependencies,
            )
        if provider_reason == "EXTERNAL_MERGE_DRIFT":
            return _commit_external_merge_drift(
                admission,
                proof.merge_request,
                message_id=message_id,
                inbox_attempts=claimed["attempts"],
                dependencies=dependencies,
            )
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=provider_reason,
            dependencies=dependencies,
        )
    try:
        effect = _acquire_merge_effect(
            admission,
            requested_head_sha=proof.current_head_sha,
            request_fingerprint=inbox["payload_hash"],
            dependencies=dependencies,
        )
    except _EffectCollision:
        return _complete_merge_preflight_block(
            admission,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code="MR_CONFLICT",
            dependencies=dependencies,
        )
    payload = effect.payload
    if not isinstance(payload, MergeIntegrationMergeRequestEffectPayload):
        raise _EffectCollision
    try:
        completed = _merge_exact_head(
            admission,
            requested_head_sha=payload.requested_head_sha,
            preflight=proof,
            dependencies=dependencies,
        )
    except _MergeExecutionBlocked as blocked:
        return _complete_merge_effect_block(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            reason_code=blocked.reason_code,
            readback=blocked.readback,
            dependencies=dependencies,
        )
    except _MergeExecutionUnknown:
        return _mark_merge_unknown(
            admission,
            effect,
            message_id=message_id,
            inbox_attempts=claimed["attempts"],
            dependencies=dependencies,
        )
    return _complete_merge_success(
        admission,
        effect,
        completed.merge_request,
        message_id=message_id,
        inbox_attempts=claimed["attempts"],
        dependencies=dependencies,
    )


__all__: list[str] = []
