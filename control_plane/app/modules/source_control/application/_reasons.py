from control_plane.app.modules.source_control.domain import (
    SourceControlDependencyUnavailable,
    SourceControlEffectDto,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason


def stored_reason(value: object) -> SourceControlReason | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceControlDependencyUnavailable("Stored Source Control reason is invalid")
    try:
        return SourceControlReason(value)
    except ValueError:
        raise SourceControlDependencyUnavailable(
            "Stored Source Control reason is invalid"
        ) from None


def effect_reason(effect: SourceControlEffectDto) -> SourceControlReason | None:
    return stored_reason(effect.last_error_code)


__all__: list[str] = []
