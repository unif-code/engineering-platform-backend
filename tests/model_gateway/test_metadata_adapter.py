"""Synthetic HTTP only; values below are not current Provider model facts."""

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from control_plane.app.modules.model_gateway.adapters.metadata import HttpxModelMetadata
from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
from control_plane.app.modules.model_gateway.domain.metadata import (
    MetadataBlocked,
    MetadataReason,
    MetadataSourceDefinition,
    MetadataState,
)
from tests.model_gateway.test_probe import material, public_dns, response

SOURCE: dict[str, Any] = {
    "purpose": "MODEL_METADATA_OBSERVATION",
    "sourceId": "synthetic-metadata",
    "version": "source-1",
    "connectionRef": "model-connection:trial",
    "providerKind": "BAILIAN_COMPATIBLE_MODE",
    "region": "cn-beijing",
    "workspaceId": "synthetic-workspace",
    "serviceSite": "asia-pacific-china",
    "allowedModelIds": ["synthetic-model"],
    "inferenceProvider": "aliyun-bailian",
    "secretRef": "secret-ref:trial-key",
    "materialVersion": "material-1",
}
MODEL: dict[str, Any] = {
    "model": "synthetic-model",
    "provider": "synthetic-author",
    "inference_provider": "aliyun-bailian",
    "model_info": {"context_window": 8192, "max_input_tokens": None, "max_output_tokens": 512},
}
RESPONSE: dict[str, Any] = {
    "success": True,
    "output": {"total": 1, "page_no": 1, "page_size": 2, "models": [MODEL]},
}


def test_one_official_get_reports_values_without_promoting_declared_capability(
    tmp_path: Path,
) -> None:
    material(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET" and request.content == b""
        assert request.url.host == "8.8.8.8"
        assert request.url.path == "/api/v1/models"
        assert request.headers["host"] == "synthetic-workspace.cn-beijing.maas.aliyuncs.com"
        assert request.extensions["sni_hostname"] == request.headers["host"]
        assert request.headers["authorization"] == "Bearer synthetic-only-secret"
        assert dict(request.url.params) == {
            "model": "synthetic-model",
            "inference_providers": "aliyun-bailian",
            "service_site": "asia-pacific-china",
            "page_no": "1",
            "page_size": "2",
        }
        return response(RESPONSE)

    adapter = HttpxModelMetadata(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    outcome = adapter.observe(
        adapter.prepare(MetadataSourceDefinition.model_validate(SOURCE)), "synthetic-model"
    )
    print(
        "METADATA37_ADAPTER",
        json.dumps(
            {
                "calls": len(calls),
                "state": outcome.state,
                "method": calls[0].method,
                "path": calls[0].url.path,
            }
        ),
    )
    assert len(calls) == 1
    assert outcome.state is MetadataState.REPORTED
    assert outcome.report is not None
    assert outcome.report.context_window.value == 8192
    assert outcome.report.max_input_tokens.state == "NULL"
    assert outcome.report.max_output_tokens.value == 512
    assert outcome.report.specification_version is None and outcome.report.expires_at is None
    assert "synthetic-only-secret" not in outcome.model_dump_json()


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "multiple",
        "duplicate",
        "more-pages",
        "count-mismatch",
        "empty-with-count",
        "wrong-page",
        "wrong-size",
        "boolean-count",
        "float-count",
        "negative-count",
        "wrong-model",
        "wrong-inference",
        "missing-author",
        "missing-model",
        "null",
        "missing",
        "zero",
        "negative",
        "boolean",
        "float",
        "string",
        "overflow",
        "failure",
        "error-code",
        "bad-json",
        "duplicate-member",
        "secret-echo",
        "deep-json",
    ],
)
def test_strict_response_uses_valid_control_and_exactly_one_get_per_observation(
    tmp_path: Path, case: str
) -> None:
    material(tmp_path)
    body = copy.deepcopy(RESPONSE)
    output = body["output"]
    model = output["models"][0]
    expected = MetadataState.INDETERMINATE
    if case == "empty":
        output.update(total=0, models=[])
        expected = MetadataState.NOT_FOUND
    if case in {"multiple", "duplicate"}:
        output.update(total=2, models=[model, copy.deepcopy(model)])
        if case == "multiple":
            output["models"][1]["model"] = "another-model"
    if case == "more-pages":
        output["total"] = 9
    if case == "count-mismatch":
        output["total"] = 0
    if case == "empty-with-count":
        output["models"] = []
    for name, field, value in [
        ("wrong-page", "page_no", 2),
        ("wrong-size", "page_size", 20),
        ("boolean-count", "total", True),
        ("float-count", "total", 1.0),
        ("negative-count", "total", -1),
    ]:
        if case == name:
            output[field] = value
    if case == "wrong-model":
        model["model"] = "different-model"
    if case == "wrong-inference":
        model["inference_provider"] = "different-provider"
    if case == "missing-author":
        model.pop("provider")
    if case == "missing-model":
        model.pop("model")
    if case == "null":
        model["model_info"]["context_window"] = None
        expected = MetadataState.REPORTED
    if case == "missing":
        model["model_info"].pop("context_window")
        expected = MetadataState.REPORTED
    for name, token_value in [
        ("zero", 0),
        ("negative", -1),
        ("boolean", True),
        ("float", 1.5),
        ("string", "8192"),
        ("overflow", 2147483648),
    ]:
        if case == name:
            model["model_info"]["context_window"] = token_value
    if case == "failure":
        body["success"] = False
        expected = MetadataState.FAILED
    if case == "error-code":
        body["code"] = "SyntheticError"
        expected = MetadataState.FAILED
    raw = json.dumps(body).encode()
    if case == "bad-json":
        raw = b'{"output":'
    if case == "deep-json":
        raw = b"[" * 2000 + b"0" + b"]" * 2000
    if case == "duplicate-member":
        raw = raw.replace(b'"total": 1', b'"total": 0, "total": 1')
    if case == "secret-echo":
        model["provider"] = "synthetic-only-secret"
        raw = json.dumps(body).encode()
        expected = MetadataState.FAILED
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(RESPONSE if len(calls) == 1 else raw)

    adapter = HttpxModelMetadata(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    prepared = adapter.prepare(MetadataSourceDefinition.model_validate(SOURCE))
    assert adapter.observe(prepared, "synthetic-model").state is MetadataState.REPORTED
    outcome = adapter.observe(prepared, "synthetic-model")
    assert len(calls) == 2 and all(x.method == "GET" for x in calls)
    assert outcome.state is expected
    if case in {"null", "missing"}:
        assert outcome.report is not None
        assert outcome.report.context_window.state == case.upper()
        assert outcome.report.context_window.value is None
    elif expected is not MetadataState.REPORTED:
        assert outcome.report is None
    if case != "secret-echo":
        assert outcome.response_sha256 == hashlib.sha256(raw).hexdigest()
        assert outcome.response_bytes == len(raw)
    assert "synthetic-only-secret" not in outcome.model_dump_json()


@pytest.mark.parametrize("status", [301, 401, 403, 429, 503])
def test_source_http_failure_is_an_observation_result_without_retry(
    tmp_path: Path, status: int
) -> None:
    material(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response({}, status=status, headers={"location": "https://other.invalid"})

    adapter = HttpxModelMetadata(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=public_dns
    )
    result = adapter.observe(
        adapter.prepare(MetadataSourceDefinition.model_validate(SOURCE)), "synthetic-model"
    )
    assert result.state is MetadataState.FAILED and result.report is None
    assert len(calls) == 1 and result.response_sha256 is None


def test_dns_and_http_share_one_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import control_plane.app.modules.model_gateway.adapters.metadata as module

    material(tmp_path)
    monkeypatch.setattr(module, "METADATA_TIMEOUT_SECONDS", 0.15)
    calls = 0

    async def dns(_hostname: str) -> tuple[str, ...]:
        await asyncio.sleep(0.09)
        return ("8.8.8.8",)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.09)
        return response(RESPONSE)

    adapter = HttpxModelMetadata(
        FileModelSecretPort(tmp_path), transport=httpx.MockTransport(handler), resolver=dns
    )
    result = adapter.observe(
        adapter.prepare(MetadataSourceDefinition.model_validate(SOURCE)), "synthetic-model"
    )
    assert calls == 1 and result.state is MetadataState.UNKNOWN and result.report is None


def test_cleanup_cannot_extend_deadline_or_publish_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import control_plane.app.modules.model_gateway.adapters.metadata as module

    material(tmp_path)
    monkeypatch.setattr(module, "METADATA_TIMEOUT_SECONDS", 0.05)

    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield json.dumps(RESPONSE).encode()

        async def aclose(self) -> None:
            await asyncio.sleep(0.2)

    adapter = HttpxModelMetadata(
        FileModelSecretPort(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=SlowClose()
            )
        ),
        resolver=public_dns,
    )
    result = adapter.observe(
        adapter.prepare(MetadataSourceDefinition.model_validate(SOURCE)), "synthetic-model"
    )
    assert result.state is MetadataState.UNKNOWN and result.report is None


def test_large_response_and_private_dns_never_produce_partial_metadata(tmp_path: Path) -> None:
    material(tmp_path)
    adapter = HttpxModelMetadata(
        FileModelSecretPort(tmp_path),
        transport=httpx.MockTransport(lambda _: response(b" " * 65537)),
        resolver=public_dns,
    )
    result = adapter.observe(
        adapter.prepare(MetadataSourceDefinition.model_validate(SOURCE)), "synthetic-model"
    )
    assert result.reason is MetadataReason.RESPONSE_TOO_LARGE and result.response_sha256 is None

    async def private_dns(_hostname: str) -> tuple[str, ...]:
        return ("127.0.0.1",)

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("private target must not receive a request")

    adapter = HttpxModelMetadata(
        FileModelSecretPort(tmp_path),
        transport=httpx.MockTransport(forbidden),
        resolver=private_dns,
    )
    result = adapter.observe(
        adapter.prepare(MetadataSourceDefinition.model_validate(SOURCE)), "synthetic-model"
    )
    assert result.reason is MetadataReason.TARGET_NOT_ALLOWED


def test_material_version_is_checked_before_any_dns_or_request(tmp_path: Path) -> None:
    material(tmp_path, "rotated")
    adapter = HttpxModelMetadata(FileModelSecretPort(tmp_path))
    with pytest.raises(MetadataBlocked, match="MATERIAL_VERSION_CHANGED"):
        adapter.prepare(MetadataSourceDefinition.model_validate(SOURCE))


@pytest.mark.parametrize(
    ("region", "workspace", "host"),
    [
        ("cn-beijing", "synthetic-workspace", "synthetic-workspace.cn-beijing.maas.aliyuncs.com"),
        ("ap-southeast-1", None, "dashscope-intl.aliyuncs.com"),
        ("cn-hongkong", None, "cn-hongkong.dashscope.aliyuncs.com"),
        (
            "ap-northeast-1",
            "synthetic-workspace",
            "synthetic-workspace.ap-northeast-1.maas.aliyuncs.com",
        ),
        (
            "eu-central-1",
            "synthetic-workspace",
            "synthetic-workspace.eu-central-1.maas.aliyuncs.com",
        ),
        ("us-east-1", "synthetic-workspace", "synthetic-workspace.us-east-1.maas.aliyuncs.com"),
    ],
)
def test_metadata_endpoint_mapping_is_independent_of_inference(
    region: str, workspace: str | None, host: str
) -> None:
    value = MetadataSourceDefinition.model_validate(
        SOURCE | {"region": region, "workspaceId": workspace}
    )
    assert value.hostname == host
    with pytest.raises(ValidationError):
        MetadataSourceDefinition.model_validate(
            SOURCE | {"region": region, "workspaceId": "unexpected" if workspace is None else None}
        )
