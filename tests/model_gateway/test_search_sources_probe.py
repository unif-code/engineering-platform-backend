import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.domain.checks import ProbeOutcome
from control_plane.app.modules.model_gateway.domain.connections import (
    CheckKind,
    ConnectionDefinition,
)
from tests.model_gateway.test_probe import CONNECTION, material, public_dns, response
from tests.model_gateway.test_stream_probe import ControlledStream


def search_response(sources: list[dict[str, object]] | None = None) -> dict[str, Any]:
    return {
        "id": "response-synthetic",
        "object": "response",
        "model": "synthetic-model",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "id": "search-1",
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "query": "synthetic private query",
                    "sources": sources
                    if sources is not None
                    else [
                        {
                            "type": "url",
                            "url": "https://www.alibabacloud.com/help?tracking=private#section",
                        }
                    ],
                },
            },
            {
                "id": "message-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "synthetic private answer", "annotations": []}
                ],
            },
        ],
    }


def run_search(tmp_path: Path, value: object) -> ProbeOutcome:
    material(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(value)

    probe = HttpxModelProbe(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    prepared = probe.prepare(
        ConnectionDefinition.model_validate(
            CONNECTION | {"responsesSearchModelIds": ["synthetic-model"]}
        )
    )
    result = probe.send(prepared, "synthetic-model", CheckKind.SEARCH_SOURCES)
    assert len(calls) == 1
    for private in (
        "synthetic private query",
        "synthetic private answer",
        "synthetic-only-secret",
        "tracking=private",
    ):
        assert private not in result.model_dump_json()
    return result


def test_search_uses_stateless_single_tool_request(tmp_path: Path) -> None:
    material(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.scheme == "https" and request.url.host == "8.8.8.8"
        assert request.url.path == "/compatible-mode/v1/responses"
        assert (
            request.headers["host"]
            == request.extensions["sni_hostname"]
            == "synthetic-workspace.cn-beijing.maas.aliyuncs.com"
        )
        assert request.headers["x-dashscope-session-cache"] == "disable"
        assert json.loads(request.content) == {
            "model": "synthetic-model",
            "input": (
                "Search the web for the official Alibaba Cloud Model Studio documentation "
                "and reply with its official URL in one short sentence."
            ),
            "store": False,
            "stream": False,
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "reasoning": {"effort": "none"},
            "max_output_tokens": 64,
        }
        return response(search_response())

    probe = HttpxModelProbe(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    prepared = probe.prepare(
        ConnectionDefinition.model_validate(
            CONNECTION | {"responsesSearchModelIds": ["synthetic-model"]}
        )
    )
    result = probe.send(prepared, "synthetic-model", CheckKind.SEARCH_SOURCES)
    assert result.state == "SUCCEEDED" and len(calls) == 1


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("valid", None),
        ("no_call", "SEARCH_SOURCE_SIGNAL_MISSING"),
        ("empty_sources", "SEARCH_SOURCE_SIGNAL_MISSING"),
        ("answer_url_only", "SEARCH_SOURCE_SIGNAL_MISSING"),
        ("incomplete", "RESPONSE_TRUNCATED"),
        ("error", "PROVIDER_ERROR"),
        ("other_model", "MODEL_IDENTITY_MISMATCH"),
        ("function", "UNEXPECTED_TOOL_OUTPUT"),
        ("no_answer", "INVALID_SEARCH_RESPONSE"),
        ("pending_call", "INVALID_SEARCH_RESPONSE"),
    ],
)
def test_search_requires_completed_call_and_structured_sources(
    tmp_path: Path, variant: str, expected: str | None
) -> None:
    value = search_response()
    if variant in {"no_call", "answer_url_only"}:
        value["output"] = value["output"][1:]
        value["tools"] = [{"type": "web_search"}]
        value["output"][0]["content"][0]["text"] = "https://www.alibabacloud.com/help"
    elif variant == "empty_sources":
        value["output"][0]["action"]["sources"] = []
    elif variant == "incomplete":
        value["status"] = "incomplete"
    elif variant == "error":
        value["error"] = {"message": "synthetic private answer"}
    elif variant == "other_model":
        value["model"] = "different-model"
    elif variant == "function":
        value["output"][0]["type"] = "function_call"
    elif variant == "no_answer":
        value["output"] = value["output"][:1]
    elif variant == "pending_call":
        value["output"][0]["status"] = "in_progress"
    result = run_search(tmp_path, value)
    assert result.reason == expected
    assert result.state == ("SUCCEEDED" if expected is None else "FAILED")


def test_search_sources_are_bounded_sanitized_and_never_fetched(tmp_path: Path) -> None:
    value = search_response(
        [
            {"type": "url", "url": "https://www.alibabacloud.com/help?a=1#x"},
            {"type": "url", "url": "https://www.alibabacloud.com/help?a=2#y"},
        ]
    )
    result = run_search(tmp_path, value)
    assert result.state == "SUCCEEDED"
    observation = result.model_dump(mode="json")["observation"]
    assert observation["completed_search_call_count"] == 1
    assert observation["sources"] == [
        {"call_id": "search-1", "sanitized_url": "https://www.alibabacloud.com/help"}
    ]
    assert observation["queries"][0]["count"] == 1
    assert observation["local_response_closed"] is True
    assert observation["provider_search_call_count"] is None


@pytest.mark.parametrize(
    "url",
    [
        "http://www.alibabacloud.com",
        "javascript:alert(1)",
        "https://user:pass@example.com/",
        "https://127.0.0.1/",
        "https://10.1.2.3/",
        "https://[::1]/",
        "https://localhost/",
        "https://service.local/",
        "https://2130706433/",
        "https://127.1/",
        "https://0x7f.0.0.1/",
        "https://example.com/path\nnext",
        "https://example.com/api_key=secret",
        "https://example.com/%73%6B-sensitive",
        "https://example.com:8080/",
    ],
)
def test_search_rejects_unsafe_source_before_persistence(tmp_path: Path, url: str) -> None:
    result = run_search(tmp_path, search_response([{"type": "url", "url": url}]))
    assert result.state == "FAILED" and result.reason == "UNSAFE_SEARCH_SOURCE"
    assert url not in result.model_dump_json()


@pytest.mark.parametrize(
    ("action", "count", "invalid"),
    [
        ({}, None, False),
        ({"query": " A  B "}, 1, False),
        ({"queries": ["A B", "C"]}, 2, False),
        ({"query": "A B", "queries": [" A  B "]}, 1, False),
        ({"query": "A", "queries": ["B"]}, None, True),
        ({"query": 1}, None, True),
        ({"queries": "A"}, None, True),
    ],
)
def test_query_metadata_is_explicitly_normalized_without_raw_query(
    tmp_path: Path, action: dict[str, object], count: int | None, invalid: bool
) -> None:
    value = search_response()
    value["output"][0]["action"].pop("query")
    value["output"][0]["action"].update(action)
    result = run_search(tmp_path, value)
    if invalid:
        assert result.state == "FAILED" and result.reason == "INVALID_SEARCH_RESPONSE"
    else:
        assert result.state == "SUCCEEDED"
        observation = result.model_dump(mode="json")["observation"]
        assert observation["queries"][0]["count"] == count
        assert (observation["queries"][0]["digest"] is None) == (count is None)
        assert "query" not in observation["queries"][0]


@pytest.mark.parametrize("variant", ["calls", "sources", "queries", "items", "duplicate_id"])
def test_search_evidence_limits_are_rejections_not_truncated_success(
    tmp_path: Path, variant: str
) -> None:
    value = search_response()
    if variant == "calls":
        value["output"] = [
            value["output"][0] | {"id": f"search-{index}"} for index in range(9)
        ] + value["output"][1:]
    elif variant == "sources":
        value["output"][0]["action"]["sources"] *= 17
    elif variant == "queries":
        value["output"][0]["action"]["queries"] = ["A"] * 9
    elif variant == "items":
        value["output"] *= 17
    else:
        value["output"].append(value["output"][0])
    result = run_search(tmp_path, value)
    assert result.state == "FAILED"
    assert result.reason == (
        "INVALID_SEARCH_RESPONSE" if variant == "duplicate_id" else "SEARCH_EVIDENCE_LIMIT"
    )


def test_usage_and_tool_counts_are_reported_metadata_not_http_attempt_counts(
    tmp_path: Path,
) -> None:
    value = search_response()
    value["usage"] = {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
        "x_tools": {"web_search": {"count": 2}},
    }
    result = run_search(tmp_path, value)
    assert result.state == "SUCCEEDED" and result.usage is not None
    assert result.usage.prompt_tokens == 3 and result.usage.completion_tokens == 4
    metadata = result.model_dump(mode="json")["observation"]
    assert (
        metadata["provider_search_call_count"] == 2 and metadata["completed_search_call_count"] == 1
    )
    value["x_tools"] = {"web_search": {"count": 3}}
    assert run_search(tmp_path, value).reason == "INVALID_SEARCH_RESPONSE"


def test_search_timeout_close_failure_and_oversize_never_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    material(tmp_path)
    calls: list[httpx.Request] = []

    class BrokenClose(ControlledStream):
        async def aclose(self) -> None:
            raise OSError("synthetic private answer")

    stream = BrokenClose([json.dumps(search_response()).encode()])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=stream)

    probe = HttpxModelProbe(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    prepared = probe.prepare(ConnectionDefinition.model_validate(CONNECTION))
    result = probe.send(prepared, "synthetic-model", CheckKind.SEARCH_SOURCES)
    assert result.state == "UNKNOWN" and result.reason == "RESPONSE_CLOSE_FAILED"
    assert len(calls) == 1 and result.observation is None
    monkeypatch.setattr(
        "control_plane.app.modules.model_gateway.adapters.probe.PROBE_TIMEOUT_SECONDS", 0.02
    )

    async def timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(1)
        return response(search_response())

    timed = HttpxModelProbe(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(timeout), resolver=public_dns
    )
    assert timed.send(prepared, "synthetic-model", CheckKind.SEARCH_SOURCES).state == "UNKNOWN"
    assert len(calls) == 2
    assert run_search(tmp_path, b"x" * 65537).reason == "RESPONSE_TOO_LARGE"


def test_public_source_preserves_legal_percent_encoded_path_spaces(tmp_path: Path) -> None:
    result = run_search(
        tmp_path,
        search_response(
            [
                {
                    "type": "url",
                    "url": "https://docs.example.com/Reference%20Guide?tracking=private#part",
                },
            ]
        ),
    )
    assert result.state == "SUCCEEDED"
    assert result.model_dump(mode="json")["observation"]["sources"] == [
        {"call_id": "search-1", "sanitized_url": "https://docs.example.com/Reference%20Guide"},
    ]
