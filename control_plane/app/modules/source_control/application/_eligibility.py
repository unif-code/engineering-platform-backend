from control_plane.app.modules.source_control.ports import (
    ActorEligibilityContext,
    RequirementBindingContext,
)


def actor_eligibility_context(
    binding: RequirementBindingContext,
    *,
    actor_id: str,
    required_capabilities: tuple[str, ...] | None = None,
) -> ActorEligibilityContext:
    return ActorEligibilityContext(
        actor_id=actor_id,
        workspace_id=binding.workspace_id,
        required_capabilities=(
            binding.required_capabilities
            if required_capabilities is None
            else required_capabilities
        ),
    )


__all__: list[str] = []
