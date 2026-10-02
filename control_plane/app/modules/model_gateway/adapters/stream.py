"""Bounded SSE parsing for fixed probes; event text never becomes evidence."""

import json
from collections.abc import Iterator

from pydantic import TypeAdapter, ValidationError

from control_plane.app.modules.model_gateway.domain import ProviderModelId
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckReason,
    CheckState,
    ProbeOutcome,
    ProbeUsage,
    StreamObservation,
    ThinkingObservation,
    provider_request_id,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    MAX_RESPONSE_BYTES,
    MAX_STREAM_EVENT_BYTES,
    MAX_STREAM_EVENTS,
    CheckKind,
)


class StreamProtocolError(Exception):
    def __init__(self, reason: CheckReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class SseFrames:
    def __init__(self) -> None:
        self.consumed_bytes = 0
        self.event_bytes = 0
        self.line = bytearray()
        self.data: list[bytes] = []
        self.after_cr = False
        self.first_line = True

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        for value in chunk:
            self.consumed_bytes += 1
            if self.consumed_bytes > MAX_RESPONSE_BYTES:
                raise StreamProtocolError(CheckReason.RESPONSE_TOO_LARGE)
            self.event_bytes += 1
            if self.event_bytes > MAX_STREAM_EVENT_BYTES:
                raise StreamProtocolError(CheckReason.STREAM_EVENT_TOO_LARGE)
            if value == 10 and self.after_cr:
                self.after_cr = False
                continue
            self.after_cr = value == 13
            if value not in (10, 13):
                self.line.append(value)
                continue
            line = bytes(self.line)
            self.line.clear()
            if self.first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                self.first_line = False
            # Decode only complete lines: a UTF-8 character may span arbitrary HTTP reads.
            try:
                line.decode("utf-8")
            except UnicodeError:
                raise StreamProtocolError(CheckReason.INVALID_STREAM_RESPONSE) from None
            if not line:
                data, self.data = self.data, []
                self.event_bytes = 0
                if data:
                    yield b"\n".join(data)
            elif not line.startswith(b":"):
                name, separator, value_bytes = line.partition(b":")
                if name == b"data":
                    self.data.append(value_bytes.removeprefix(b" ") if separator else b"")


class StreamProbe:
    def __init__(self, kind: CheckKind, model_id: str) -> None:
        self.kind, self.model_id = kind, model_id
        self.frames = SseFrames()
        self.events = 0
        self.text_deltas = 0
        self.text_bytes = 0
        self.answer_observed = False
        self.reasoning_observed = False
        self.reasoning_deltas = 0
        self.reasoning_bytes = 0
        self.normal_completion = False
        self.done = False
        self.local_closed = False
        self.receiving_complete = False
        self.close_failed = False
        self.request_id: str | None = None
        self.reported_model: str | None = None
        self.usage: ProbeUsage | None = None

    def observation(self) -> StreamObservation | ThinkingObservation:
        values = {
            "kind": self.kind,
            "consumed_bytes": self.frames.consumed_bytes,
            "data_event_count": self.events,
            "text_delta_count": self.text_deltas,
            "text_bytes": self.text_bytes,
            "text_observed": self.answer_observed
            if self.kind is CheckKind.THINKING
            else self.text_deltas > 0,
            "normal_completion_observed": self.normal_completion,
            "completion_marker_observed": self.done,
            "local_stream_closed": self.local_closed,
        }
        if self.kind is CheckKind.THINKING:
            return ThinkingObservation.model_validate(
                values
                | {
                    "reasoning_observed": self.reasoning_observed,
                    "reasoning_delta_count": self.reasoning_deltas,
                    "reasoning_bytes": self.reasoning_bytes,
                }
            )
        return StreamObservation.model_validate(values)

    def outcome(self, state: CheckState, reason: CheckReason | None = None) -> ProbeOutcome:
        return ProbeOutcome(
            state=state,
            reason=reason,
            provider_request_id=self.request_id,
            reported_model_id=self.reported_model,
            usage=self.usage,
            observation=self.observation(),
        )

    def event(self, data: bytes) -> bool:
        self.events += 1
        if self.events > MAX_STREAM_EVENTS:
            raise StreamProtocolError(CheckReason.STREAM_EVENT_LIMIT)
        if data.strip() == b"[DONE]":
            self.done = True
            if not self.normal_completion or self.text_deltas == 0:
                raise StreamProtocolError(CheckReason.INVALID_STREAM_RESPONSE)
            if self.kind is CheckKind.THINKING and not self.answer_observed:
                raise StreamProtocolError(CheckReason.INVALID_STREAM_RESPONSE)
            return True
        try:
            value = json.loads(data)
            if not isinstance(value, dict) or value.get("object") != "chat.completion.chunk":
                raise ValueError
            reported = TypeAdapter(ProviderModelId).validate_python(value.get("model"))
            request_id = provider_request_id(value.get("id"))
            if reported != self.model_id:
                raise StreamProtocolError(CheckReason.MODEL_IDENTITY_MISMATCH)
            if self.request_id is not None and request_id != self.request_id:
                raise StreamProtocolError(CheckReason.STREAM_IDENTITY_CHANGED)
            self.request_id, self.reported_model = request_id, reported
            if value.get("usage") is not None:
                # Each usage object is a cumulative observation, never an additive delta.
                self.usage = ProbeUsage.model_validate(value["usage"])
            choices = value.get("choices")
            if not isinstance(choices, list):
                raise ValueError
            if not choices:
                if value.get("usage") is None:
                    raise ValueError
                return False
            if len(choices) != 1 or not isinstance(choices[0], dict):
                raise ValueError
            choice = choices[0]
            if type(choice.get("index")) is not int or choice["index"] != 0:
                raise ValueError
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("role") not in (None, "assistant"):
                raise ValueError
            if delta.get("tool_calls") or delta.get("function_call") or delta.get("audio"):
                raise ValueError
            if delta.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise StreamProtocolError(CheckReason.RESPONSE_REFUSED)
            if choice.get("finish_reason") == "length":
                raise StreamProtocolError(CheckReason.RESPONSE_TRUNCATED)
            if choice.get("finish_reason") not in (None, "stop"):
                raise ValueError
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError
            if self.normal_completion:
                raise ValueError
            if self.kind is CheckKind.THINKING:
                reasoning = delta.get("reasoning_content")
                if reasoning is not None and not isinstance(reasoning, str):
                    raise ValueError
                if reasoning:
                    encoded_reasoning = reasoning.encode("utf-8")
                    self.reasoning_deltas += 1
                    self.reasoning_bytes += len(encoded_reasoning)
                    self.reasoning_observed |= bool(reasoning.strip())
            if content:
                encoded_content = content.encode("utf-8")
                self.text_deltas += 1
                self.text_bytes += len(encoded_content)
                self.answer_observed |= bool(content.strip())
            if choice.get("finish_reason") == "stop":
                self.normal_completion = True
            return self.kind is CheckKind.STREAM_STOP and self.text_deltas > 0
        except (ValueError, TypeError, ValidationError, UnicodeError, RecursionError):
            raise StreamProtocolError(CheckReason.INVALID_STREAM_RESPONSE) from None
