import asyncio
import json
from pathlib import Path

import httpx
import pytest

from control_plane.app.modules.model_gateway.adapters.connections import (
    FileConnectionDirectory,
    ModelConnectionSettings,
)
from control_plane.app.modules.model_gateway.adapters.probe import HttpxModelProbe
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckBlocked,
    CheckReason,
    CheckState,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    ConnectionDefinition,
    probe_body,
)

CONNECTION = {
    "reference": "model-connection:trial",
    "version": "config-1",
    "materialVersion": "material-1",
    "providerKind": "BAILIAN_COMPATIBLE_MODE",
    "region": "cn-beijing",
    "workspaceId": "synthetic-workspace",
    "allowedModelIds": ["synthetic-model"],
    "secretRef": "secret-ref:trial-key",
}
RESPONSE = {
    "id": "chatcmpl-synthetic",
    "object": "chat.completion",
    "model": "synthetic-model",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}
    ],
}


def material(root: Path, version: str = "material-1") -> None:
    root.mkdir(exist_ok=True)
    (root / "trial-key").write_text(
        json.dumps({"version": version, "value": "synthetic-only-secret"})
    )


async def public_dns(hostname: str) -> tuple[str, ...]:
    assert hostname == "synthetic-workspace.cn-beijing.maas.aliyuncs.com"
    return ("8.8.8.8",)


def response(
    body: object = RESPONSE, *, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(
        status,
        stream=httpx.ByteStream(raw),
        headers={"content-type": "application/json", **(headers or {})},
    )


def test_single_send_uses_pinned_public_ip_original_sni_fixed_probe_and_optional_usage(
    tmp_path: Path,
) -> None:
    material(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "synthetic-workspace.cn-beijing.maas.aliyuncs.com"
        assert request.extensions["sni_hostname"] == request.headers["host"]
        assert request.url.scheme == "https"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert json.loads(request.content) == probe_body("synthetic-model")
        assert request.headers["authorization"] == "Bearer synthetic-only-secret"
        return response()

    probe = HttpxModelProbe(tmp_path, transport=httpx.MockTransport(handler), resolver=public_dns)
    prepared = probe.prepare(ConnectionDefinition.model_validate(CONNECTION))
    result = probe.send(prepared, "synthetic-model")
    assert result.state is CheckState.SUCCEEDED and result.usage is None
    assert len(calls) == 1 and result.elapsed_ms is not None
    assert "synthetic-only-secret" not in result.model_dump_json()
    assert "OK" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("body", "status", "headers", "reason"),
    [
        (RESPONSE | {"model": "replacement-model"}, 200, {}, CheckReason.MODEL_IDENTITY_MISMATCH),
        (
            RESPONSE | {"choices": [{"finish_reason": "length"}]},
            200,
            {},
            CheckReason.RESPONSE_TRUNCATED,
        ),
        (
            RESPONSE
            | {
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": ""}}
                ]
            },
            200,
            {},
            CheckReason.INVALID_RESPONSE,
        ),
        (RESPONSE | {"usage": {"prompt_tokens": True}}, 200, {}, CheckReason.INVALID_RESPONSE),
        (b"{malformed", 200, {}, CheckReason.INVALID_RESPONSE),
        (b"x" * 65537, 200, {}, CheckReason.RESPONSE_TOO_LARGE),
        (RESPONSE, 200, {"content-encoding": "gzip"}, CheckReason.INVALID_RESPONSE),
        (
            b"private error body",
            302,
            {"location": "http://127.0.0.1/private"},
            CheckReason.REDIRECT_REJECTED,
        ),
        (b"private error body", 403, {}, CheckReason.PROVIDER_REJECTED),
        (b"private error body", 429, {}, CheckReason.PROVIDER_RATE_LIMITED),
        (b"private error body", 503, {}, CheckReason.PROVIDER_ERROR),
    ],
)
def test_protocol_failures_are_bounded_sanitized_and_never_retried(
    tmp_path: Path, body: object, status: int, headers: dict[str, str], reason: CheckReason
) -> None:
    material(tmp_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(body, status=status, headers=headers)

    probe = HttpxModelProbe(tmp_path, transport=httpx.MockTransport(handler), resolver=public_dns)
    result = probe.send(
        probe.prepare(ConnectionDefinition.model_validate(CONNECTION)), "synthetic-model"
    )
    assert result.state is CheckState.FAILED and result.reason is reason
    assert len(calls) == 1
    assert "private error body" not in result.model_dump_json()


def test_send_timeout_is_unknown_and_usage_is_preserved_without_inventing_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material(tmp_path)
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(1)
        return response()

    monkeypatch.setattr(
        "control_plane.app.modules.model_gateway.adapters.probe.PROBE_TIMEOUT_SECONDS", 0.01
    )
    probe = HttpxModelProbe(tmp_path, transport=httpx.MockTransport(handler), resolver=public_dns)
    result = probe.send(
        probe.prepare(ConnectionDefinition.model_validate(CONNECTION)), "synthetic-model"
    )
    assert result.state is CheckState.UNKNOWN and len(calls) == 1
    assert result.provider_request_id is None
    from control_plane.app.modules.model_gateway.adapters.probe import parse_response

    value = parse_response(
        json.dumps(RESPONSE | {"usage": {"prompt_tokens": 3}}).encode(), "synthetic-model"
    )
    assert value.usage is not None and value.usage.prompt_tokens == 3
    assert value.usage.completion_tokens is value.usage.total_tokens is None


@pytest.mark.parametrize(
    "addresses", [("127.0.0.1",), ("10.0.0.1",), ("::1",), ("8.8.8.8", "192.168.1.1"), ()]
)
def test_private_or_unproven_targets_cannot_reach_send(
    tmp_path: Path, addresses: tuple[str, ...]
) -> None:
    material(tmp_path)

    async def dns(hostname: str) -> tuple[str, ...]:
        return addresses

    with pytest.raises(CheckBlocked) as error:
        HttpxModelProbe(tmp_path, resolver=dns).prepare(
            ConnectionDefinition.model_validate(CONNECTION)
        )
    assert error.value.reason is CheckReason.TARGET_NOT_ALLOWED


def test_material_version_and_manifest_admission_fail_closed(tmp_path: Path) -> None:
    connection = ConnectionDefinition.model_validate(CONNECTION)
    with pytest.raises(CheckBlocked) as error:
        HttpxModelProbe(tmp_path, resolver=public_dns).prepare(connection)
    assert error.value.reason is CheckReason.MATERIAL_UNAVAILABLE
    material(tmp_path, "material-2")
    with pytest.raises(CheckBlocked) as error:
        HttpxModelProbe(tmp_path, resolver=public_dns).prepare(connection)
    assert error.value.reason is CheckReason.MATERIAL_VERSION_CHANGED
    manifest = tmp_path / "connections.json"
    manifest.write_text(json.dumps({"environment": "TEST", "connections": [CONNECTION]}))
    directory = FileConnectionDirectory(
        ModelConnectionSettings(environment="TEST", connections_path=manifest)
    )
    assert directory.resolve(connection.reference) == ("TEST", connection)
    manifest.write_text(json.dumps({"environment": "WRONG", "connections": [CONNECTION]}))
    with pytest.raises(CheckBlocked):
        directory.resolve(connection.reference)


def test_provider_cannot_echo_material_in_otherwise_valid_metadata(tmp_path: Path) -> None:
    material(tmp_path)
    probe = HttpxModelProbe(
        tmp_path,
        resolver=public_dns,
        transport=httpx.MockTransport(
            lambda request: response(RESPONSE | {"id": "chatcmpl-synthetic-only-secret"})
        ),
    )
    result = probe.send(
        probe.prepare(ConnectionDefinition.model_validate(CONNECTION)), "synthetic-model"
    )
    assert result.state is CheckState.FAILED and result.reason is CheckReason.INVALID_RESPONSE
    assert "synthetic-only-secret" not in result.model_dump_json()
