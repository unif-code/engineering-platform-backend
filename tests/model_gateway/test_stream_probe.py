import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.domain.checks import ProbeOutcome, StreamObservation
from control_plane.app.modules.model_gateway.domain.connections import (
    CheckKind,
    ConnectionDefinition,
)
from tests.model_gateway.test_probe import CONNECTION, material, public_dns


def chunk(content: str | None = None, finish: str | None = None, **extra: object) -> dict[str, Any]:
    return {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "model": "synthetic-model",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
        **extra,
    }


def event(value: object) -> bytes:
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes]) -> None:
        self.parts, self.consumed, self.closed = parts, 0, False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            self.consumed += 1
            yield part

    async def aclose(self) -> None:
        self.closed = True


def test_streaming_completion_and_actual_local_stop(tmp_path: Path) -> None:
    material(tmp_path)
    data = event(chunk()) + event(chunk("你好")) + event(chunk("", "stop"))
    data += event(
        chunk(choices=[], usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
    )
    data += event("[DONE]")
    stream = ControlledStream([data[index : index + 1] for index in range(len(data))])
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    probe = HttpxModelProbe(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(send), resolver=public_dns
    )
    prepared = probe.prepare(ConnectionDefinition.model_validate(CONNECTION))
    full = probe.send(prepared, "synthetic-model", CheckKind.STREAM_TEXT)
    assert isinstance(full.observation, StreamObservation)
    assert full.state == "SUCCEEDED" and full.observation.completion_marker_observed
    assert stream.closed and len(calls) == 1
    stream = ControlledStream(
        [
            event(chunk()),
            event(chunk("Hello")),
            event(chunk("never consume", "stop")),
            event("[DONE]"),
        ]
    )
    stopped = probe.send(prepared, "synthetic-model", CheckKind.STREAM_STOP)
    assert isinstance(stopped.observation, StreamObservation)
    assert stopped.state == "SUCCEEDED" and stopped.observation.local_stream_closed
    assert stopped.observation.provider_cancellation == "UNCONFIRMED"
    assert stream.closed and stream.consumed == 2 and len(calls) == 2
    assert "Hello" not in stopped.model_dump_json()


def run_stream(
    tmp_path: Path, stream: ControlledStream, kind: CheckKind = CheckKind.STREAM_TEXT
) -> ProbeOutcome:
    material(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=stream
        )

    probe = HttpxModelProbe(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    result = probe.send(
        probe.prepare(ConnectionDefinition.model_validate(CONNECTION)), "synthetic-model", kind
    )
    assert len(calls) == 1
    return result


@pytest.mark.parametrize(
    ("parts", "state", "reason"),
    [
        ([event(chunk("partial"))], "UNKNOWN", "STREAM_INTERRUPTED"),
        ([event(chunk("text", "stop"))], "UNKNOWN", "STREAM_INTERRUPTED"),
        ([event("[DONE]")], "FAILED", "INVALID_STREAM_RESPONSE"),
        (
            [
                event(chunk())
                + event(chunk(choices=[], usage={"total_tokens": 2}))
                + event("[DONE]")
            ],
            "FAILED",
            "INVALID_STREAM_RESPONSE",
        ),
        (
            [
                event(
                    chunk(
                        choices=[
                            {
                                "index": 0,
                                "delta": {"tool_calls": [{"id": "x"}]},
                                "finish_reason": None,
                            }
                        ]
                    )
                )
            ],
            "FAILED",
            "INVALID_STREAM_RESPONSE",
        ),
        (
            [
                event(
                    chunk(
                        choices=[{"index": 1, "delta": {"content": "other"}, "finish_reason": None}]
                    )
                )
            ],
            "FAILED",
            "INVALID_STREAM_RESPONSE",
        ),
        (
            [event(chunk("a")) + event(chunk("b", id="changed"))],
            "FAILED",
            "STREAM_IDENTITY_CHANGED",
        ),
        (
            [event(chunk("a")) + event(chunk("b", model="changed"))],
            "FAILED",
            "MODEL_IDENTITY_MISMATCH",
        ),
        ([b'data: {"bad":"\xff"}\n\n'], "FAILED", "INVALID_STREAM_RESPONSE"),
        ([b"data: {broken}\n\n"], "FAILED", "INVALID_STREAM_RESPONSE"),
        ([b"data: " + b"x" * 16385], "FAILED", "STREAM_EVENT_TOO_LARGE"),
        ([b": heartbeat\n\n" * 6000], "FAILED", "RESPONSE_TOO_LARGE"),
        ([event(chunk()) * 257], "FAILED", "STREAM_EVENT_LIMIT"),
        ([event(chunk("cut", "length"))], "FAILED", "RESPONSE_TRUNCATED"),
        (
            [
                event(
                    chunk(
                        choices=[
                            {
                                "index": 0,
                                "delta": {"refusal": "rejected"},
                                "finish_reason": "content_filter",
                            }
                        ]
                    )
                )
            ],
            "FAILED",
            "RESPONSE_REFUSED",
        ),
    ],
)
def test_incomplete_or_invalid_stream_never_passes_or_retries(
    tmp_path: Path, parts: list[bytes], state: str, reason: str
) -> None:
    stream = ControlledStream(parts)
    value = run_stream(tmp_path, stream)
    assert value.state == state and value.reason == reason
    assert stream.closed
    assert isinstance(value.observation, StreamObservation)
    assert observation(value).provider_cancellation == "UNCONFIRMED"
    assert observation(value).consumed_bytes <= 65537


def test_multiline_crlf_comments_coalesced_events_and_repeated_usage(tmp_path: Path) -> None:
    pretty = json.dumps(chunk("text", "stop"), indent=2)
    multiline = b"\xef\xbb\xbf: heartbeat\r\nevent: message\r\n"
    multiline += b"".join(f"data: {line}\r\n".encode() for line in pretty.splitlines()) + b"\r\n"
    usage = event(
        chunk(choices=[], usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4})
    )
    value = run_stream(tmp_path, ControlledStream([multiline + usage + usage + event("[DONE]")]))
    assert value.state == "SUCCEEDED" and value.usage is not None
    assert value.usage.total_tokens == 4
    assert observation(value).data_event_count == 4
    assert observation(value).text_delta_count == 1


def test_stop_ignores_later_events_even_in_the_same_http_read(tmp_path: Path) -> None:
    parts = [
        event(chunk("first", "stop"))
        + event(chunk("must not parse", id="changed"))
        + b"data: invalid\n\n"
    ]
    value = run_stream(tmp_path, ControlledStream(parts), CheckKind.STREAM_STOP)
    assert value.state == "SUCCEEDED"
    assert observation(value).text_delta_count == 1
    assert observation(value).normal_completion_observed
    assert not observation(value).completion_marker_observed
    assert observation(value).provider_cancellation == "UNCONFIRMED"
    assert "must not parse" not in value.model_dump_json()


def test_close_failure_and_total_timeout_cannot_fabricate_stop_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    class BrokenClose(ControlledStream):
        async def aclose(self) -> None:
            raise OSError("private close error")

    failed = run_stream(tmp_path, BrokenClose([event(chunk("first"))]), CheckKind.STREAM_STOP)
    assert failed.state == "UNKNOWN" and failed.reason == "STREAM_CLOSE_FAILED"
    assert observation(failed).text_observed and not observation(failed).local_stream_closed
    assert "private close error" not in failed.model_dump_json()

    class Continuous(ControlledStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                yield event(chunk("x"))
                await asyncio.sleep(0.01)

    monkeypatch.setattr(
        "control_plane.app.modules.model_gateway.adapters.probe.PROBE_TIMEOUT_SECONDS", 0.04
    )
    stream = Continuous([])
    timeout = run_stream(tmp_path, stream)
    assert timeout.state == "UNKNOWN" and timeout.reason == "STREAM_INTERRUPTED"
    assert observation(timeout).text_delta_count > 0
    assert stream.closed


def observation(value: ProbeOutcome) -> StreamObservation:
    assert isinstance(value.observation, StreamObservation)
    return value.observation


def test_cleanup_after_body_timeout_shares_the_original_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import time

    class StalledBodyAndClose(ControlledStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield event(chunk())
            await asyncio.sleep(10)

        async def aclose(self) -> None:
            await asyncio.sleep(10)

    monkeypatch.setattr(
        "control_plane.app.modules.model_gateway.adapters.probe.PROBE_TIMEOUT_SECONDS", 0.02
    )
    start = time.monotonic()
    value = run_stream(tmp_path, StalledBodyAndClose([]), CheckKind.STREAM_STOP)
    assert time.monotonic() - start < 1
    assert value.state == "UNKNOWN" and value.reason == "STREAM_CLOSE_FAILED"
    assert not observation(value).local_stream_closed
