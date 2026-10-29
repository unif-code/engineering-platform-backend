import asyncio
import json
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.domain.checks import ProbeOutcome, ToolCallObservation
from control_plane.app.modules.model_gateway.domain.connections import (
    CheckKind,
    ConnectionDefinition,
)
from tests.model_gateway.test_probe import CONNECTION, live_dispatch, material, public_dns, response
from tests.model_gateway.test_stream_probe import ControlledStream
from tests.model_gateway.tool_call_support import tool_response


def run_tool(
    tmp_path: Path,
    value: object,
    *,
    stream: httpx.AsyncByteStream | None = None,
    status: int = 200,
) -> ProbeOutcome:
    material(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert json.loads(request.content) == {
            "model": "synthetic-model",
            "messages": [{"role": "user", "content": "Call platform_probe with marker set to ok."}],
            "stream": False,
            "enable_thinking": False,
            "parallel_tool_calls": False,
            "max_completion_tokens": 64,
            "tool_choice": {"type": "function", "function": {"name": "platform_probe"}},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "platform_probe",
                        "description": "Return the fixed probe marker.",
                        "parameters": {
                            "type": "object",
                            "properties": {"marker": {"type": "string", "enum": ["ok"]}},
                            "required": ["marker"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
        }
        assert request.headers["Host"] == "synthetic-workspace.cn-beijing.maas.aliyuncs.com"
        assert request.extensions["sni_hostname"] == request.headers["Host"]
        return (
            response(value, status=status)
            if stream is None
            else httpx.Response(status, headers={"content-type": "application/json"}, stream=stream)
        )

    probe = HttpxModelProbe(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    result = probe.send(
        probe.prepare(ConnectionDefinition.model_validate(CONNECTION)),
        "synthetic-model",
        CheckKind.TOOL_CALL,
        dispatch_guard=live_dispatch,
    )
    assert len(requests) == 1
    assert "synthetic-only-secret" not in result.model_dump_json()
    return result


@pytest.mark.parametrize("content", [None, "", "ignored natural language"])
def test_fixed_non_streaming_proposal_is_observed_without_tool_execution(
    tmp_path: Path, content: str | None
) -> None:
    value = tool_response()
    value["choices"][0]["message"]["content"] = content
    result = run_tool(tmp_path, value)
    assert result.state == "SUCCEEDED" and result.usage is None
    assert isinstance(result.observation, ToolCallObservation)
    assert result.observation.matching_call_observed
    assert result.observation.tool_call_completion_observed
    assert result.observation.local_response_closed
    assert result.observation.consumed_bytes == len(json.dumps(value).encode())
    assert set(result.observation.model_dump()) == {
        "kind",
        "consumed_bytes",
        "matching_call_observed",
        "tool_call_completion_observed",
        "local_response_closed",
    }
    assert all(
        text not in result.model_dump_json()
        for text in [
            "platform_probe",
            "marker",
            "arguments",
            "call_synthetic",
            "ignored natural language",
        ]
    )


def test_missing_content_and_equivalent_json_whitespace_remain_valid(tmp_path: Path) -> None:
    value = tool_response()
    message = value["choices"][0]["message"]
    message.pop("content")
    message["function_call"] = None
    message["tool_calls"][0]["function"]["arguments"] = ' { "marker" : "\\u006fk" } '
    assert run_tool(tmp_path, value).state == "SUCCEEDED"


@pytest.mark.parametrize("calls", ["missing", None, []])
def test_only_complete_stop_without_call_is_signal_missing(tmp_path: Path, calls: object) -> None:
    value = tool_response()
    value["choices"][0]["finish_reason"] = "stop"
    if calls == "missing":
        value["choices"][0]["message"].pop("tool_calls")
    else:
        value["choices"][0]["message"]["tool_calls"] = calls
    value["choices"][0]["message"]["content"] = '{"name":"platform_probe","marker":"ok"}'
    result = run_tool(tmp_path, value)
    assert result.state == "FAILED" and result.reason == "TOOL_CALL_SIGNAL_MISSING"
    assert isinstance(result.observation, ToolCallObservation)
    assert not result.observation.matching_call_observed
    assert not result.observation.tool_call_completion_observed
    assert result.observation.local_response_closed


@pytest.mark.parametrize("length", [1, 128])
def test_call_id_shape_bounds_do_not_require_a_provider_specific_prefix(
    tmp_path: Path, length: int
) -> None:
    value = tool_response()
    value["choices"][0]["message"]["tool_calls"][0]["id"] = "x" * length
    assert run_tool(tmp_path, value).state == "SUCCEEDED"


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {},
        [],
        1,
        True,
        "null",
        "[]",
        '"ok"',
        "{}",
        '{"marker":true}',
        '{"marker":1}',
        '{"marker":"OK"}',
        '{"marker":"ok","extra":1}',
        '{"marker":"wrong","marker":"ok"}',
        '{"marker":"ok","marker":"ok"}',
        '{"marker":NaN}',
        '{"marker":Infinity}',
        '{"marker":-Infinity}',
        '{"marker":1e999}',
        '{"marker":"ok"} trailing',
    ],
)
def test_arguments_are_strict_json_and_exactly_the_fixed_object(
    tmp_path: Path, arguments: object
) -> None:
    value = tool_response()
    value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    result = run_tool(tmp_path, value)
    assert result.state == "FAILED" and result.reason == "INVALID_RESPONSE"


@pytest.mark.parametrize(
    "case",
    [
        "choices-empty",
        "choices-multiple",
        "calls-empty",
        "calls-object",
        "calls-multiple",
        "call-type",
        "name",
        "function-type",
        "id-empty",
        "id-blank",
        "id-long",
        "id-type",
        "finish-stop",
        "finish-null",
        "legacy-function",
        "content-type",
        "refusal-type",
        "role",
    ],
)
def test_ambiguous_or_conflicting_proposals_never_pass(tmp_path: Path, case: str) -> None:
    value = tool_response()
    choice = value["choices"][0]
    message = choice["message"]
    call = message["tool_calls"][0]
    if case == "choices-empty":
        value["choices"] = []
    if case == "choices-multiple":
        value["choices"].append(deepcopy(choice))
    if case == "calls-empty":
        message["tool_calls"] = []
    if case == "calls-object":
        message["tool_calls"] = call
    if case == "calls-multiple":
        message["tool_calls"].append(deepcopy(call))
    if case == "call-type":
        call["type"] = "other"
    if case == "name":
        call["function"]["name"] = "other_function"
    if case == "function-type":
        call["function"] = []
    if case == "id-empty":
        call["id"] = ""
    if case == "id-blank":
        call["id"] = " \t\n"
    if case == "id-long":
        call["id"] = "x" * 129
    if case == "id-type":
        call["id"] = 4
    if case == "finish-stop":
        choice["finish_reason"] = "stop"
    if case == "finish-null":
        choice["finish_reason"] = None
    if case == "legacy-function":
        message["function_call"] = {}
    if case == "content-type":
        message["content"] = 1
    if case == "refusal-type":
        message["refusal"] = False
    if case == "role":
        message["role"] = "tool"
    result = run_tool(tmp_path, value)
    assert result.state == "FAILED" and result.reason == "INVALID_RESPONSE"


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda value: '{"object":"other",' + value[1:],
        lambda value: value.replace('"tool_calls": [', '"tool_calls": [], "tool_calls": [', 1),
        lambda value: value[:-1] + ',"extra":NaN}',
        lambda value: value[:-1] + ',"extra":1e999}',
    ],
)
def test_outer_json_is_also_strict(tmp_path: Path, rewrite: Callable[[str], str]) -> None:
    result = run_tool(tmp_path, rewrite(json.dumps(tool_response())).encode())
    assert result.state == "FAILED" and result.reason == "INVALID_RESPONSE"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("identity", "MODEL_IDENTITY_MISMATCH"),
        ("length", "RESPONSE_TRUNCATED"),
        ("refusal", "RESPONSE_REFUSED"),
        ("content_filter", "RESPONSE_REFUSED"),
    ],
)
def test_stronger_reasons_are_not_hidden_by_missing_signal(
    tmp_path: Path, change: str, reason: str
) -> None:
    value = tool_response()
    value["choices"][0]["finish_reason"] = "stop"
    value["choices"][0]["message"].pop("tool_calls")
    if change == "identity":
        value["model"] = "other-model"
    if change in {"length", "content_filter"}:
        value["choices"][0]["finish_reason"] = change
    if change == "refusal":
        value["choices"][0]["message"]["refusal"] = "synthetic refusal"
    result = run_tool(tmp_path, value)
    assert result.state == "FAILED" and result.reason == reason


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "PROVIDER_REJECTED"),
        (403, "PROVIDER_REJECTED"),
        (429, "PROVIDER_RATE_LIMITED"),
        (500, "PROVIDER_ERROR"),
        (302, "REDIRECT_REJECTED"),
    ],
)
def test_http_failure_does_not_become_tool_absence(
    tmp_path: Path, status: int, reason: str
) -> None:
    result = run_tool(tmp_path, {}, status=status)
    assert result.state == "FAILED" and result.reason == reason


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_matching_observation_survives_failed_close_without_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class BrokenClose(ControlledStream):
        async def aclose(self) -> None:
            if failure == "error":
                raise OSError("synthetic private cleanup error")
            await asyncio.sleep(1)

    monkeypatch.setattr(
        "control_plane.app.modules.model_gateway.adapters.probe.PROBE_TIMEOUT_SECONDS", 0.02
    )

    result = run_tool(tmp_path, None, stream=BrokenClose([json.dumps(tool_response()).encode()]))
    assert result.state == "UNKNOWN" and result.reason == "RESPONSE_CLOSE_FAILED"
    assert isinstance(result.observation, ToolCallObservation)
    assert (
        result.observation.matching_call_observed
        and result.observation.tool_call_completion_observed
    )
    assert not result.observation.local_response_closed


def test_timeout_and_oversize_never_create_matching_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Slow(ControlledStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"object":"chat.completion"'
            await asyncio.sleep(1)

    monkeypatch.setattr(
        "control_plane.app.modules.model_gateway.adapters.probe.PROBE_TIMEOUT_SECONDS", 0.02
    )
    result = run_tool(tmp_path, None, stream=Slow([]))
    assert result.state == "UNKNOWN" and result.observation is None
    result = run_tool(tmp_path, b"x" * 65537)
    assert result.state == "FAILED" and result.reason == "RESPONSE_TOO_LARGE"
