from dataclasses import replace

from control_plane.app.modules.source_control.application._integration_merge_provider import (
    _merge_exact_head,
    _MergeExecutionBlocked,
    _MergeExecutionUnknown,
    _MergePreflightBlocked,
    _MergePreflightTransient,
    _provider_block_reason,
    _read_merge_provider_proof,
)
from control_plane.app.modules.source_control.application._integration_reconcile_completion import (
    complete_merge_reconciliation,
    complete_reconciliation_block,
    deliver_merge_completion,
    deliver_terminal_effect,
    renew_reconciliation_lease,
    return_reconciliation_unknown,
)
from control_plane.app.modules.source_control.application._integration_reconcile_context import (
    LocalReconciliationBlocked,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import EffectState, SourceControlEffectDto
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason

from ._integration_reconcile_merge_context import (
    MergeReconciliationContext,
    read_merge_reconciliation_context,
)


def _block_without_fact(
    context: MergeReconciliationContext,
    reason: SourceControlReason,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    completion = complete_reconciliation_block(
        context.effect,
        reason=reason,
        dependencies=dependencies,
    )
    return deliver_terminal_effect(
        completion,
        binding_id=context.binding.id,
        dependencies=dependencies,
    )


def _complete_fact(
    context: MergeReconciliationContext,
    *,
    snapshot: object,
    state: EffectState,
    reason: SourceControlReason | None,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    completion = complete_merge_reconciliation(
        context,
        snapshot,
        final_state=state,
        reason=reason,
        dependencies=dependencies,
    )
    return deliver_merge_completion(
        completion,
        binding_id=context.binding.id,
        dependencies=dependencies,
    )


def reconcile_merge_effect(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    local = read_merge_reconciliation_context(effect, dependencies=dependencies)
    if isinstance(local, LocalReconciliationBlocked):
        completion = complete_reconciliation_block(
            effect,
            reason=local.reason,
            dependencies=dependencies,
        )
        return deliver_terminal_effect(
            completion,
            binding_id=None,
            dependencies=dependencies,
        )
    try:
        proof = _read_merge_provider_proof(local, dependencies=dependencies)
    except _MergePreflightTransient:
        return return_reconciliation_unknown(effect, dependencies=dependencies)
    except _MergePreflightBlocked as blocked:
        return _block_without_fact(
            local,
            blocked.reason_code,
            dependencies=dependencies,
        )
    requested_head = local.payload.requested_head_sha
    if proof.merge_request.head_sha != requested_head:
        return _block_without_fact(
            local,
            SourceControlReason.HEAD_SHA_CHANGED,
            dependencies=dependencies,
        )
    if proof.merge_request.state == "merged":
        if proof.source is None:
            return _complete_fact(
                local,
                snapshot=proof.merge_request,
                state=EffectState.BLOCKED,
                reason=SourceControlReason.SOURCE_BRANCH_MISSING_AFTER_INTEGRATION,
                dependencies=dependencies,
            )
        if proof.source.commit_sha != requested_head:
            return _complete_fact(
                local,
                snapshot=proof.merge_request,
                state=EffectState.BLOCKED,
                reason=SourceControlReason.HEAD_SHA_CHANGED,
                dependencies=dependencies,
            )
        return _complete_fact(
            local,
            snapshot=proof.merge_request,
            state=EffectState.SUCCEEDED,
            reason=None,
            dependencies=dependencies,
        )
    if proof.merge_request.state == "closed":
        return _complete_fact(
            local,
            snapshot=proof.merge_request,
            state=EffectState.BLOCKED,
            reason=SourceControlReason.MR_CLOSED,
            dependencies=dependencies,
        )
    stable_reason = _provider_block_reason(proof)
    if stable_reason is not None:
        return _complete_fact(
            local,
            snapshot=proof.merge_request,
            state=EffectState.BLOCKED,
            reason=stable_reason,
            dependencies=dependencies,
        )
    renewed = renew_reconciliation_lease(effect, dependencies=dependencies)
    if renewed is None:
        return return_reconciliation_unknown(effect, dependencies=dependencies)
    local = replace(local, effect=renewed)
    try:
        completed = _merge_exact_head(
            local,
            requested_head_sha=requested_head,
            preflight=proof,
            dependencies=dependencies,
        )
    except _MergeExecutionUnknown:
        return return_reconciliation_unknown(local.effect, dependencies=dependencies)
    except _MergeExecutionBlocked as blocked:
        reason = blocked.reason_code
        if blocked.readback is not None:
            return _complete_fact(
                local,
                snapshot=blocked.readback,
                state=EffectState.BLOCKED,
                reason=reason,
                dependencies=dependencies,
            )
        return _block_without_fact(local, reason, dependencies=dependencies)
    return _complete_fact(
        local,
        snapshot=completed.merge_request,
        state=EffectState.SUCCEEDED,
        reason=None,
        dependencies=dependencies,
    )


__all__: list[str] = []
