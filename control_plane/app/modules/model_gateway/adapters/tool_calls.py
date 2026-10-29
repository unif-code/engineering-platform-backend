"""Observe one fixed function proposal. No tool lookup, execution or follow-up request."""

import json
import math
from typing import Any, NoReturn

from pydantic import ValidationError

from control_plane.app.modules.model_gateway.adapters.chat_identity import (
    ChatIdentityMismatch,
    completion_metadata,
)
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckReason,
    CheckState,
    ProbeOutcome,
    ToolCallObservation,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    TOOL_CALL_MARKER,
    TOOL_CALL_NAME,
    CheckKind,
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("non-standard JSON constant")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _json(value: bytes | str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def _matches(calls: Any) -> bool:
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return False
    call = calls[0]
    identifier = call.get("id")
    function = call.get("function")
    if (
        call.get("type") != "function"
        or not isinstance(identifier, str)
        or not 1 <= len(identifier) <= 128
        or not identifier.strip()
        or not isinstance(function, dict)
        or function.get("name") != TOOL_CALL_NAME
        or not isinstance(function.get("arguments"), str)
    ):
        return False
    try:
        arguments = _json(function["arguments"])
    except (ValueError, TypeError, RecursionError):
        return False
    return isinstance(arguments, dict) and arguments == {"marker": TOOL_CALL_MARKER}


def parse_tool_call_response(raw: bytes, requested_model: str) -> ProbeOutcome:
    try:
        value = _json(raw)
        metadata = completion_metadata(value, requested_model)
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("exactly one completion choice required")
        choice = choices[0]
        finish = choice.get("finish_reason")
        if finish == "length":
            return ProbeOutcome(
                state=CheckState.FAILED, reason=CheckReason.RESPONSE_TRUNCATED, **metadata
            )
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("assistant response required")
        refusal = message.get("refusal")
        if finish == "content_filter" or (isinstance(refusal, str) and refusal):
            return ProbeOutcome(
                state=CheckState.FAILED, reason=CheckReason.RESPONSE_REFUSED, **metadata
            )
        if refusal is not None and not isinstance(refusal, str):
            raise ValueError("invalid refusal field")
        if (
            message.get("content") is not None and not isinstance(message["content"], str)
        ) or message.get("function_call") is not None:
            raise ValueError("invalid or conflicting assistant response")
        calls = message.get("tool_calls")
        observation = ToolCallObservation(
            kind=CheckKind.TOOL_CALL,
            consumed_bytes=len(raw),
            matching_call_observed=_matches(calls),
            tool_call_completion_observed=finish == "tool_calls",
            local_response_closed=False,
        )
        if finish == "stop" and (calls is None or calls == []):
            return ProbeOutcome(
                state=CheckState.FAILED,
                reason=CheckReason.TOOL_CALL_SIGNAL_MISSING,
                observation=observation,
                **metadata,
            )
        success = observation.matching_call_observed and observation.tool_call_completion_observed
        return ProbeOutcome(
            state=CheckState.SUCCEEDED if success else CheckState.FAILED,
            reason=None if success else CheckReason.INVALID_RESPONSE,
            observation=observation,
            **metadata,
        )
    except ChatIdentityMismatch as error:
        return error.outcome
    except (ValueError, TypeError, ValidationError, UnicodeError, RecursionError):
        return ProbeOutcome(state=CheckState.FAILED, reason=CheckReason.INVALID_RESPONSE)
