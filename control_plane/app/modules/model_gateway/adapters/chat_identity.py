"""Shared identity and returned usage checks for non-streaming Chat Completions."""

from typing import Any

from pydantic import TypeAdapter

from control_plane.app.modules.model_gateway.domain import ProviderModelId
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckReason,
    CheckState,
    ProbeOutcome,
    ProbeUsage,
    provider_request_id,
)


class ChatIdentityMismatch(ValueError):
    def __init__(self, metadata: dict[str, Any]) -> None:
        super().__init__("chat completion model identity mismatch")
        self.outcome = ProbeOutcome(
            state=CheckState.FAILED, reason=CheckReason.MODEL_IDENTITY_MISMATCH, **metadata
        )


def completion_metadata(value: Any, requested_model: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("object") != "chat.completion":
        raise ValueError("invalid chat completion envelope")
    reported = TypeAdapter(ProviderModelId).validate_python(value.get("model"))
    request_id = provider_request_id(value.get("id"))
    usage = None if value.get("usage") is None else ProbeUsage.model_validate(value["usage"])
    metadata = {"reported_model_id": reported, "provider_request_id": request_id, "usage": usage}
    if reported != requested_model:
        raise ChatIdentityMismatch(metadata)
    return metadata
