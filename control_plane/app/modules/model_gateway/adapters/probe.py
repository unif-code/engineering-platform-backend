"""One bounded HTTPS text probe. No Provider SDK, retries, redirects or raw evidence."""

import asyncio
import ipaddress
import json
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter, ValidationError

from control_plane.app.modules.model_gateway.domain import ProviderModelId
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckBlocked,
    CheckReason,
    CheckState,
    ProbeOutcome,
    ProbeUsage,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    MAX_RESPONSE_BYTES,
    PROBE_TIMEOUT_SECONDS,
    ConnectionDefinition,
    VersionLabel,
    probe_body,
)
from control_plane.app.shared.security.file_references import (
    FileSecretReferenceReader,
    SecretReferenceUnavailable,
)


class FileMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: VersionLabel
    value: SecretStr


@dataclass(frozen=True)
class PreparedProbe:
    hostname: str
    address: str
    material: FileMaterial


async def public_addresses(hostname: str) -> tuple[str, ...]:
    rows = await asyncio.get_running_loop().getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    return tuple(sorted({str(row[4][0]) for row in rows}))


def _safe_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None
        or value.lower().startswith("sk-")
    ):
        raise ValueError("invalid provider identifier")
    return value


def parse_response(raw: bytes, requested_model: str) -> ProbeOutcome:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("object") != "chat.completion":
            raise ValueError
        reported = TypeAdapter(ProviderModelId).validate_python(value.get("model"))
        request_id = _safe_identifier(value.get("id"))
        usage = None if value.get("usage") is None else ProbeUsage.model_validate(value["usage"])
        if reported != requested_model:
            return ProbeOutcome(
                state=CheckState.FAILED,
                reason=CheckReason.MODEL_IDENTITY_MISMATCH,
                provider_request_id=request_id,
                reported_model_id=reported,
                usage=usage,
            )
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            return ProbeOutcome(
                state=CheckState.FAILED,
                reason=CheckReason.RESPONSE_TRUNCATED,
                provider_request_id=request_id,
                reported_model_id=reported,
                usage=usage,
            )
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            return ProbeOutcome(
                state=CheckState.FAILED,
                reason=CheckReason.RESPONSE_REFUSED,
                provider_request_id=request_id,
                reported_model_id=reported,
                usage=usage,
            )
        content = message.get("content")
        if (
            choice.get("finish_reason") != "stop"
            or message.get("tool_calls")
            or message.get("function_call")
            or not isinstance(content, str)
            or not content.strip()
        ):
            raise ValueError
        return ProbeOutcome(
            state=CheckState.SUCCEEDED,
            provider_request_id=request_id,
            reported_model_id=reported,
            usage=usage,
        )
    except (ValueError, TypeError, ValidationError, UnicodeError, RecursionError):
        return ProbeOutcome(state=CheckState.FAILED, reason=CheckReason.INVALID_RESPONSE)


class HttpxModelProbe:
    def __init__(
        self,
        secret_root: Path | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[tuple[str, ...]]] = public_addresses,
    ) -> None:
        self.secret_root, self.transport, self.resolver = secret_root, transport, resolver

    def _material(self, connection: ConnectionDefinition) -> FileMaterial:
        try:
            if self.secret_root is None:
                raise ValueError
            raw = FileSecretReferenceReader(self.secret_root).resolve(connection.secret_ref)
            material = FileMaterial.model_validate_json(raw)
            if re.fullmatch(r"[A-Za-z0-9._/-]{8,8192}", material.value.get_secret_value()) is None:
                raise ValueError
            return material
        except (SecretReferenceUnavailable, ValueError, ValidationError):
            raise CheckBlocked(CheckReason.MATERIAL_UNAVAILABLE) from None

    def material_version(self, connection: ConnectionDefinition) -> str:
        return self._material(connection).version

    def prepare(self, connection: ConnectionDefinition) -> PreparedProbe:
        material = self._material(connection)
        if material.version != connection.material_version:
            raise CheckBlocked(CheckReason.MATERIAL_VERSION_CHANGED)

        async def resolve() -> tuple[str, ...]:
            async with asyncio.timeout(5):
                return await self.resolver(connection.hostname)

        try:
            addresses = asyncio.run(resolve())
        except (OSError, TimeoutError):
            raise CheckBlocked(CheckReason.TARGET_UNAVAILABLE) from None
        try:
            if not addresses:
                raise ValueError
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                    raise ValueError
        except ValueError:
            raise CheckBlocked(CheckReason.TARGET_NOT_ALLOWED) from None
        return PreparedProbe(connection.hostname, addresses[0], material)

    def send(self, prepared: PreparedProbe, model_id: str) -> ProbeOutcome:
        start = time.monotonic()
        try:
            outcome = asyncio.run(self._send(prepared, model_id))
        except (TimeoutError, httpx.HTTPError, OSError):
            outcome = ProbeOutcome(
                state=CheckState.UNKNOWN, reason=CheckReason.REQUEST_OUTCOME_UNKNOWN
            )
        if prepared.material.value.get_secret_value() in outcome.model_dump_json():
            outcome = ProbeOutcome(state=CheckState.FAILED, reason=CheckReason.INVALID_RESPONSE)
        return outcome.model_copy(update={"elapsed_ms": int((time.monotonic() - start) * 1000)})

    async def _send(self, prepared: PreparedProbe, model_id: str) -> ProbeOutcome:
        # Pin the validated public IP; retain the approved hostname for Host and TLS verification.
        target = httpx.URL(
            f"https://{prepared.hostname}/compatible-mode/v1/chat/completions"
        ).copy_with(host=prepared.address)
        transport = self.transport or httpx.AsyncHTTPTransport(
            verify=ssl.create_default_context(),
            retries=0,
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS, connect=5),
            ) as client:
                async with client.stream(
                    "POST",
                    target,
                    json=probe_body(model_id),
                    headers={
                        "Host": prepared.hostname,
                        "Authorization": f"Bearer {prepared.material.value.get_secret_value()}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    extensions={"sni_hostname": prepared.hostname},
                ) as response:
                    if 300 <= response.status_code < 400:
                        return ProbeOutcome(
                            state=CheckState.FAILED, reason=CheckReason.REDIRECT_REJECTED
                        )
                    if response.status_code != 200:
                        reason = (
                            CheckReason.PROVIDER_RATE_LIMITED
                            if response.status_code == 429
                            else CheckReason.PROVIDER_REJECTED
                            if 400 <= response.status_code < 500
                            else CheckReason.PROVIDER_ERROR
                        )
                        return ProbeOutcome(state=CheckState.FAILED, reason=reason)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return ProbeOutcome(
                            state=CheckState.FAILED, reason=CheckReason.INVALID_RESPONSE
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=4096):
                        if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                            return ProbeOutcome(
                                state=CheckState.FAILED, reason=CheckReason.RESPONSE_TOO_LARGE
                            )
                        chunks.extend(chunk)
                    return parse_response(bytes(chunks), model_id)
