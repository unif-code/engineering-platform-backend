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
from threading import Thread

import httpx
from pydantic import TypeAdapter, ValidationError

from control_plane.app.modules.model_gateway.adapters.stream import (
    StreamProbe,
    StreamProtocolError,
)
from control_plane.app.modules.model_gateway.domain import ProviderModelId
from control_plane.app.modules.model_gateway.domain.checks import (
    BasicTextObservation,
    CheckBlocked,
    CheckReason,
    CheckState,
    ProbeOutcome,
    ProbeUsage,
    ThinkingObservation,
    provider_request_id,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    MAX_RESPONSE_BYTES,
    PROBE_TIMEOUT_SECONDS,
    CheckKind,
    ConnectionDefinition,
    probe_body,
)
from control_plane.app.modules.model_gateway.ports.checks import (
    ModelSecretPort,
    ProviderSecretMaterial,
)


@dataclass(frozen=True)
class PreparedProbe:
    hostname: str
    address: str
    material: ProviderSecretMaterial


async def public_addresses(hostname: str) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[str, ...]] = loop.create_future()
    system_resolver = socket.getaddrinfo

    def resolve() -> None:
        addresses: tuple[str, ...] | None
        try:
            rows = system_resolver(hostname, 443, type=socket.SOCK_STREAM)
            addresses = tuple(sorted({str(row[4][0]) for row in rows}))
        except OSError:
            addresses = None

        def complete() -> None:
            if future.done():
                return
            if addresses is None:
                future.set_exception(OSError("DNS resolution unavailable"))
            else:
                future.set_result(addresses)

        try:
            loop.call_soon_threadsafe(complete)
        except RuntimeError:
            pass  # The timed-out caller has closed its loop; never initiate HTTP here.

    # The OS resolver cannot be cancelled; it must not hold up timeout or process shutdown.
    Thread(target=resolve, daemon=True).start()
    return await future


def parse_response(raw: bytes, requested_model: str) -> ProbeOutcome:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("object") != "chat.completion":
            raise ValueError
        reported = TypeAdapter(ProviderModelId).validate_python(value.get("model"))
        request_id = provider_request_id(value.get("id"))
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
        secrets: ModelSecretPort,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[tuple[str, ...]]] = public_addresses,
    ) -> None:
        self.secrets, self.transport, self.resolver = secrets, transport, resolver

    def _material(self, connection: ConnectionDefinition) -> ProviderSecretMaterial:
        try:
            material = self.secrets.resolve(connection.secret_ref)
            if re.fullmatch(r"[A-Za-z0-9._/-]{8,8192}", material.value.get_secret_value()) is None:
                raise ValueError
            return material
        except (ValueError, ValidationError):
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

    def send(self, prepared: PreparedProbe, model_id: str, check_kind: CheckKind) -> ProbeOutcome:
        start = time.monotonic()
        stream = None if check_kind is CheckKind.BASIC_TEXT else StreamProbe(check_kind, model_id)
        try:
            outcome = asyncio.run(self._send(prepared, model_id, check_kind, stream))
        except Exception:
            if stream is not None:
                outcome = stream.outcome(
                    CheckState.UNKNOWN,
                    CheckReason.STREAM_CLOSE_FAILED
                    if stream.close_failed or stream.receiving_complete
                    else CheckReason.STREAM_INTERRUPTED,
                )
            else:
                outcome = ProbeOutcome(
                    state=CheckState.UNKNOWN, reason=CheckReason.REQUEST_OUTCOME_UNKNOWN
                )
        if (
            outcome.state is CheckState.SUCCEEDED
            and isinstance(outcome.observation, ThinkingObservation)
            and not outcome.observation.reasoning_observed
        ):
            outcome = outcome.model_copy(
                update={
                    "state": CheckState.FAILED,
                    "reason": CheckReason.THINKING_SIGNAL_MISSING,
                }
            )
        if prepared.material.value.get_secret_value() in outcome.model_dump_json():
            outcome = ProbeOutcome(state=CheckState.FAILED, reason=CheckReason.INVALID_RESPONSE)
        return outcome.model_copy(update={"elapsed_ms": int((time.monotonic() - start) * 1000)})

    async def _body(
        self, response: httpx.Response, model_id: str, stream: StreamProbe | None
    ) -> ProbeOutcome:
        if 300 <= response.status_code < 400:
            return ProbeOutcome(state=CheckState.FAILED, reason=CheckReason.REDIRECT_REJECTED)
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
            return ProbeOutcome(state=CheckState.FAILED, reason=CheckReason.INVALID_RESPONSE)
        if stream is not None:
            if (
                response.headers.get("content-type", "").split(";")[0].strip().lower()
                != "text/event-stream"
            ):
                return stream.outcome(CheckState.FAILED, CheckReason.INVALID_STREAM_RESPONSE)
            try:
                # No chunk_size buffering: stop as soon as the first complete text event arrives.
                async for chunk in response.aiter_raw():
                    for event in stream.frames.feed(chunk):
                        if stream.event(event):
                            return stream.outcome(CheckState.SUCCEEDED)
                return stream.outcome(CheckState.UNKNOWN, CheckReason.STREAM_INTERRUPTED)
            except StreamProtocolError as error:
                return stream.outcome(CheckState.FAILED, error.reason)
        chunks = bytearray()
        async for chunk in response.aiter_raw(chunk_size=4096):
            chunks.extend(chunk[: MAX_RESPONSE_BYTES + 1 - len(chunks)])
            if len(chunks) > MAX_RESPONSE_BYTES:
                return ProbeOutcome(
                    state=CheckState.FAILED,
                    reason=CheckReason.RESPONSE_TOO_LARGE,
                    observation=BasicTextObservation(
                        kind=CheckKind.BASIC_TEXT,
                        consumed_bytes=len(chunks),
                        text_observed=False,
                        normal_completion_observed=False,
                    ),
                )
        result = parse_response(bytes(chunks), model_id)
        success = result.state is CheckState.SUCCEEDED
        return result.model_copy(
            update={
                "observation": BasicTextObservation(
                    kind=CheckKind.BASIC_TEXT,
                    consumed_bytes=len(chunks),
                    text_observed=success,
                    normal_completion_observed=success,
                )
            }
        )

    async def _send(
        self,
        prepared: PreparedProbe,
        model_id: str,
        check_kind: CheckKind,
        stream: StreamProbe | None,
    ) -> ProbeOutcome:
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
        deadline = asyncio.get_running_loop().time() + PROBE_TIMEOUT_SECONDS
        client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS, connect=5),
        )
        response: httpx.Response | None = None
        close_failed = False
        try:
            async with asyncio.timeout_at(deadline):
                request = client.build_request(
                    "POST",
                    target,
                    json=probe_body(model_id, check_kind),
                    headers={
                        "Host": prepared.hostname,
                        "Authorization": f"Bearer {prepared.material.value.get_secret_value()}",
                        "Accept": "application/json" if stream is None else "text/event-stream",
                        "Accept-Encoding": "identity",
                    },
                    extensions={"sni_hostname": prepared.hostname},
                )
                response = await client.send(request, stream=True)
                outcome = await self._body(response, model_id, stream)
                if stream is not None:
                    stream.receiving_complete = True
        finally:
            # Cleanup shares the original deadline, including after a body timeout.
            for resource in (response, client):
                if resource is None:
                    continue
                try:
                    async with asyncio.timeout_at(deadline):
                        await resource.aclose()
                except Exception:
                    close_failed = True
            if stream is not None:
                stream.local_closed = response is not None and not close_failed
                stream.close_failed = close_failed
            if close_failed:
                raise OSError("local response cleanup unconfirmed") from None
        if stream is not None:
            outcome = outcome.model_copy(update={"observation": stream.observation()})
        return outcome
