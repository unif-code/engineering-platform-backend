from control_plane.app.modules.source_control.application._integration_reconcile_completion import (
    complete_create_reconciliation,
    complete_reconciliation_block,
    deliver_create_completion,
    deliver_terminal_effect,
    renew_reconciliation_lease,
    return_reconciliation_unknown,
)
from control_plane.app.modules.source_control.application._integration_reconcile_context import (
    LocalReconciliationBlocked,
    read_create_reconciliation_context,
)
from control_plane.app.modules.source_control.application._integration_reconcile_provider import (
    ReconciliationProviderBlocked,
    ReconciliationProviderUnknown,
    discover_create_candidate,
    prove_create_merge_request,
    retry_create_merge_request,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectState,
    MergeRequestCreationOrigin,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason


def reconcile_create_effect(
    effect: SourceControlEffectDto,
    *,
    dependencies: SourceControlDependencies,
) -> SourceControlEffectDto:
    local = read_create_reconciliation_context(effect, dependencies=dependencies)
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
        candidate = discover_create_candidate(local, dependencies=dependencies)
        if candidate is None:
            renewed = renew_reconciliation_lease(effect, dependencies=dependencies)
            if renewed is None:
                return return_reconciliation_unknown(effect, dependencies=dependencies)
            local = local.__class__(
                effect=renewed,
                payload=local.payload,
                workspace_id=local.workspace_id,
                branch_binding_id=local.branch_binding_id,
                source_branch=local.source_branch,
                profile=local.profile,
            )
            iid = retry_create_merge_request(local, dependencies=dependencies)
            origin = MergeRequestCreationOrigin.PLATFORM_CREATED
        else:
            iid = candidate.iid
            origin = MergeRequestCreationOrigin.EXTERNAL_ADOPTED
        proof = prove_create_merge_request(
            local,
            iid=iid,
            creation_origin=origin,
            dependencies=dependencies,
        )
    except ReconciliationProviderUnknown:
        return return_reconciliation_unknown(local.effect, dependencies=dependencies)
    except ReconciliationProviderBlocked as blocked:
        block_completion = complete_reconciliation_block(
            local.effect,
            reason=blocked.reason,
            dependencies=dependencies,
        )
        return deliver_terminal_effect(
            block_completion,
            binding_id=None,
            dependencies=dependencies,
        )
    if proof.snapshot.state == "opened":
        state = EffectState.SUCCEEDED
        reason = None
    elif proof.snapshot.state == "closed":
        state = EffectState.BLOCKED
        reason = SourceControlReason.MR_CLOSED
    elif proof.snapshot.state == "merged":
        state = EffectState.BLOCKED
        reason = SourceControlReason.EXTERNAL_MERGE_DRIFT
    else:
        return return_reconciliation_unknown(local.effect, dependencies=dependencies)
    create_completion = complete_create_reconciliation(
        local,
        proof,
        final_state=state,
        reason=reason,
        dependencies=dependencies,
    )
    return deliver_create_completion(create_completion, dependencies=dependencies)


__all__: list[str] = []
