from control_plane.app.modules.requirement.domain import (
    RequirementDependencyUnavailable,
    required_work_item_set_hash,
)


def validate_current_delivery_set(work_item_ids: tuple[str, ...], stored_hash: str) -> None:
    if required_work_item_set_hash(work_item_ids) != stored_hash:
        raise RequirementDependencyUnavailable("Requirement delivery snapshot is inconsistent")
