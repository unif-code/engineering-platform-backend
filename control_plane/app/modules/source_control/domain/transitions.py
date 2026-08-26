from control_plane.app.modules.source_control.domain.models import EffectState


class SourceControlError(ValueError):
    """A deterministic Source Control denial."""


class InvalidEffectTransition(SourceControlError):
    pass


_EFFECT_TRANSITIONS = {
    EffectState.PLANNED: {EffectState.IN_FLIGHT, EffectState.BLOCKED},
    EffectState.IN_FLIGHT: {
        EffectState.SUCCEEDED,
        EffectState.BLOCKED,
        EffectState.UNKNOWN,
    },
    EffectState.UNKNOWN: {EffectState.RECONCILIATION},
    EffectState.RECONCILIATION: {
        EffectState.SUCCEEDED,
        EffectState.BLOCKED,
        EffectState.UNKNOWN,
        EffectState.IN_FLIGHT,
    },
    EffectState.SUCCEEDED: set(),
    EffectState.BLOCKED: set(),
}


def transition_effect(current: EffectState, target: EffectState) -> EffectState:
    if target not in _EFFECT_TRANSITIONS[current]:
        raise InvalidEffectTransition(f"{current.value}->{target.value}")
    return target
