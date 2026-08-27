from datetime import timedelta

from control_plane.app.modules.source_control.application._integration_common import (
    effect_dto as _effect_dto,
)
from control_plane.app.modules.source_control.application._integration_reconcile_callbacks import (
    replay_pending_integration_callbacks,
)
from control_plane.app.modules.source_control.application._integration_reconcile_create import (
    reconcile_create_effect,
)
from control_plane.app.modules.source_control.application._integration_reconcile_merge import (
    reconcile_merge_effect,
)
from control_plane.app.modules.source_control.application.dependencies import (
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.domain import (
    EffectOperation,
    ReconcileDueIntegrationEffectsResult,
    SourceControlDependencyUnavailable,
)


def reconcile_due_integration_effects(
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> ReconcileDueIntegrationEffectsResult:
    if limit < 1:
        raise ValueError("Integration reconciliation limit must be positive")
    repository_factory = dependencies.delivery_repository_factory
    if repository_factory is None:
        raise SourceControlDependencyUnavailable("Integration repository unavailable")
    now = dependencies.clock.now()
    with dependencies.engine.begin() as db:
        rows = repository_factory(db).claim_effects(
            limit=limit,
            now=now,
            lease_until=now + timedelta(minutes=2),
        )
    effects = []
    for row in rows:
        effect = _effect_dto(row)
        if effect.operation is EffectOperation.CREATE_INTEGRATION_MR:
            effect = reconcile_create_effect(effect, dependencies=dependencies)
        elif effect.operation is EffectOperation.MERGE_INTEGRATION_MR:
            effect = reconcile_merge_effect(effect, dependencies=dependencies)
        effects.append(effect)
    effects.extend(
        replay_pending_integration_callbacks(
            limit=limit,
            excluded_effect_ids=frozenset(effect.id for effect in effects),
            dependencies=dependencies,
        )
    )
    return ReconcileDueIntegrationEffectsResult(effects=tuple(effects))
