import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from control_plane.app.modules.model_gateway.domain.checks import ProbeOutcome, ThinkingObservation
from control_plane.app.modules.model_gateway.domain.connections import CheckKind
from tests.model_gateway.test_stream_probe import ControlledStream, chunk, event, run_stream

REASONING = "synthetic private reasoning 甲"
ANSWER = "synthetic final answer 乙"


def thinking_chunk(
    reasoning: object, content: str | None = None, finish: str | None = None, **extra: object
) -> dict[str, Any]:
    value = chunk(content, finish, **extra)
    value["choices"][0]["delta"]["reasoning_content"] = reasoning
    return value


def thinking_observation(value: ProbeOutcome) -> ThinkingObservation:
    assert isinstance(value.observation, ThinkingObservation)
    return value.observation


@pytest.mark.parametrize(
    ("events", "state", "reason"),
    [
        ([thinking_chunk(REASONING), chunk(ANSWER, "stop")], "SUCCEEDED", None),
        ([thinking_chunk(REASONING, ANSWER, "stop")], "SUCCEEDED", None),
        ([chunk(ANSWER, "stop")], "FAILED", "THINKING_SIGNAL_MISSING"),
        ([thinking_chunk(None, ANSWER, "stop")], "FAILED", "THINKING_SIGNAL_MISSING"),
        ([thinking_chunk("", ANSWER, "stop")], "FAILED", "THINKING_SIGNAL_MISSING"),
        ([thinking_chunk(" \t\n", ANSWER, "stop")], "FAILED", "THINKING_SIGNAL_MISSING"),
        (
            [chunk("<think>pretend reasoning</think>323", "stop")],
            "FAILED",
            "THINKING_SIGNAL_MISSING",
        ),
        ([thinking_chunk(REASONING, None, "stop")], "FAILED", "INVALID_STREAM_RESPONSE"),
        ([thinking_chunk(REASONING, " \t", "stop")], "FAILED", "INVALID_STREAM_RESPONSE"),
        ([], "FAILED", "INVALID_STREAM_RESPONSE"),
    ],
)
def test_thinking_requires_reasoning_answer_and_complete_stream(
    tmp_path: Path, events: list[dict[str, Any]], state: str, reason: str | None
) -> None:
    wire = b"".join(event(value) for value in events) + event("[DONE]")
    source = ControlledStream([wire[index : index + 1] for index in range(len(wire))])
    result = run_stream(tmp_path, source, CheckKind.THINKING)
    assert result.state == state and result.reason == reason
    assert source.closed
    metadata = thinking_observation(result)
    assert metadata.local_stream_closed and metadata.provider_cancellation == "UNCONFIRMED"
    if state == "SUCCEEDED":
        assert metadata.reasoning_observed and metadata.text_observed
        assert metadata.reasoning_delta_count == metadata.text_delta_count == 1
        assert metadata.reasoning_bytes == len(REASONING.encode())
        assert metadata.text_bytes == len(ANSWER.encode())
    for private in (REASONING, ANSWER, "<think>", "Compute 17 times"):
        assert private not in result.model_dump_json()


@pytest.mark.parametrize(
    ("parts", "state", "reason"),
    [
        ([event(thinking_chunk(REASONING))], "UNKNOWN", "STREAM_INTERRUPTED"),
        ([event(chunk(ANSWER, "length"))], "FAILED", "RESPONSE_TRUNCATED"),
        ([event(thinking_chunk(REASONING, ANSWER, "length"))], "FAILED", "RESPONSE_TRUNCATED"),
        ([event(thinking_chunk(1, ANSWER, "stop"))], "FAILED", "INVALID_STREAM_RESPONSE"),
        ([event(thinking_chunk([], ANSWER, "stop"))], "FAILED", "INVALID_STREAM_RESPONSE"),
        ([event(chunk(ANSWER, model="different-model"))], "FAILED", "MODEL_IDENTITY_MISMATCH"),
        (
            [event(thinking_chunk(REASONING)) + event(chunk(ANSWER, id="different-id"))],
            "FAILED",
            "STREAM_IDENTITY_CHANGED",
        ),
        ([event(chunk(ANSWER, "content_filter"))], "FAILED", "RESPONSE_REFUSED"),
        (
            [
                event(
                    thinking_chunk(
                        REASONING,
                        choices=[
                            {
                                "index": 0,
                                "delta": {"tool_calls": [{"id": "tool"}]},
                                "finish_reason": None,
                            }
                        ],
                    )
                )
            ],
            "FAILED",
            "INVALID_STREAM_RESPONSE",
        ),
    ],
)
def test_incomplete_thinking_preserves_unknown_or_protocol_failure(
    tmp_path: Path, parts: list[bytes], state: str, reason: str
) -> None:
    source = ControlledStream(parts)
    result = run_stream(tmp_path, source, CheckKind.THINKING)
    assert result.state == state and result.reason == reason
    assert source.closed
    assert REASONING not in result.model_dump_json()
    assert ANSWER not in result.model_dump_json()


def test_thinking_missing_signal_cannot_mask_cleanup_failure(tmp_path: Path) -> None:
    class FailedClose(ControlledStream):
        async def aclose(self) -> None:
            raise OSError("synthetic private close failure")

    result = run_stream(
        tmp_path, FailedClose([event(chunk(ANSWER, "stop")) + event("[DONE]")]), CheckKind.THINKING
    )
    assert result.state == "UNKNOWN" and result.reason == "STREAM_CLOSE_FAILED"
    assert not thinking_observation(result).reasoning_observed
    assert "synthetic private close failure" not in result.model_dump_json()


def test_thinking_timeout_keeps_partial_observation_without_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowAnswer(ControlledStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield event(thinking_chunk(REASONING))
            await asyncio.sleep(10)

    monkeypatch.setattr(
        "control_plane.app.modules.model_gateway.adapters.probe.PROBE_TIMEOUT_SECONDS", 0.02
    )
    result = run_stream(tmp_path, SlowAnswer([]), CheckKind.THINKING)
    assert result.state == "UNKNOWN" and result.reason == "STREAM_INTERRUPTED"
    assert thinking_observation(result).reasoning_observed
    assert not thinking_observation(result).text_observed


@pytest.mark.parametrize(
    ("parts", "reason"),
    [
        ([event(thinking_chunk("x" * 16385))], "STREAM_EVENT_TOO_LARGE"),
        ([event(thinking_chunk("x")) * 257], "STREAM_EVENT_LIMIT"),
        ([b": heartbeat\n\n" * 6000], "RESPONSE_TOO_LARGE"),
    ],
)
def test_thinking_keeps_existing_stream_bounds(
    tmp_path: Path, parts: list[bytes], reason: str
) -> None:
    result = run_stream(tmp_path, ControlledStream(parts), CheckKind.THINKING)
    assert result.state == "FAILED" and result.reason == reason
    value = thinking_observation(result)
    assert value.reasoning_delta_count <= 256 and value.reasoning_bytes <= 65536


def test_thinking_counts_fields_separately_and_does_not_grade_the_answer(tmp_path: Path) -> None:
    source = ControlledStream(
        [
            event(thinking_chunk(" \t")),
            event(thinking_chunk("合成甲", " ")),
            event(thinking_chunk("合成乙", "wrong answer", "stop")),
            event(chunk(choices=[], usage={"completion_tokens": 5})),
            event("[DONE]"),
        ]
    )
    result = run_stream(tmp_path, source, CheckKind.THINKING)
    assert result.state == "SUCCEEDED"
    value = thinking_observation(result)
    assert value.reasoning_observed and value.reasoning_delta_count == 3
    assert value.reasoning_bytes == 20
    assert value.text_delta_count == 2 and value.text_bytes == 13
    assert result.usage is not None and result.usage.completion_tokens == 5
    assert result.usage.total_tokens is None


@pytest.mark.parametrize("kind", [CheckKind.STREAM_TEXT, CheckKind.STREAM_STOP])
def test_existing_stream_kinds_cannot_pass_using_only_reasoning(
    tmp_path: Path, kind: CheckKind
) -> None:
    result = run_stream(
        tmp_path,
        ControlledStream(
            [
                event(thinking_chunk(REASONING, None, "stop")),
                event("[DONE]"),
            ]
        ),
        kind,
    )
    assert result.state == "FAILED" and result.reason == "INVALID_STREAM_RESPONSE"
    assert "reasoningObserved" not in result.model_dump_json(by_alias=True)
