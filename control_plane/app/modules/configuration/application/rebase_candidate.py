from copy import deepcopy
from typing import Any

from control_plane.app.modules.configuration.domain import DraftBaseComparison, InvalidPolicyValue
from control_plane.app.modules.configuration.ports.policy_owner import PolicyOwnerPort


def rebase_candidate(
    owner: PolicyOwnerPort,
    observation: DraftBaseComparison,
    resolutions: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    conflicts = {item.key for item in observation.items if item.change == "CONFLICT"}
    if not isinstance(resolutions, dict) or set(resolutions) != conflicts:
        raise InvalidPolicyValue("Resolve exactly the current conflicts")
    candidate: dict[str, Any] = {}
    selections: dict[str, dict[str, Any]] = {}
    automatic = {
        "UNCHANGED": "BASE",
        "CURRENT_ONLY": "CURRENT",
        "DRAFT_ONLY": "DRAFT",
        "SAME_CHANGE": "CURRENT",
    }
    for item in observation.items:
        if item.change == "CONFLICT":
            resolution = resolutions[item.key]
            if not isinstance(resolution, dict):
                raise InvalidPolicyValue("Invalid conflict resolution")
            source = resolution.get("choice")
            if source not in {"CURRENT", "DRAFT", "CUSTOM"} or set(resolution) != (
                {"choice", "value"} if source == "CUSTOM" else {"choice"}
            ):
                raise InvalidPolicyValue("Invalid conflict resolution")
        else:
            source = automatic[item.change]
        candidate[item.key] = deepcopy(
            resolutions[item.key]["value"]
            if source == "CUSTOM"
            else item.base_value
            if source == "BASE"
            else item.current_value
            if source == "CURRENT"
            else item.draft_value
        )
        selections[item.key] = {"change": item.change, "source": source}
    normalized = owner.normalize_candidate(
        observation.namespace, schema_revision=observation.schema_revision, values=candidate
    )
    for key in conflicts:
        choice = selections[key]["source"]
        selections[key]["resolution"] = {
            "choice": choice,
            **({"value": deepcopy(normalized[key])} if choice == "CUSTOM" else {}),
        }
    return normalized, selections
