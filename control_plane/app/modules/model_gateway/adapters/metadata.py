"""One approved official GET, with one deadline from DNS through response cleanup."""

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from control_plane.app.modules.model_gateway.adapters.probe import (
    model_https_client,
    public_addresses,
    verified_public_address,
)
from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked, provider_request_id
from control_plane.app.modules.model_gateway.domain.metadata import (
    METADATA_MAX_BYTES,
    METADATA_PAGE_NO,
    METADATA_PAGE_SIZE,
    METADATA_TIMEOUT_SECONDS,
    MetadataBlocked,
    MetadataOutcome,
    MetadataReason,
    MetadataReport,
    MetadataSourceDefinition,
    MetadataState,
    ReportedLimit,
)
from control_plane.app.modules.model_gateway.ports.checks import (
    ModelSecretPort,
    ProviderSecretMaterial,
)


@dataclass(frozen=True)
class PreparedMetadata:
    source: MetadataSourceDefinition
    material: ProviderSecretMaterial


def parse_metadata(raw: bytes, model_id: str, inference_provider: str) -> MetadataOutcome:
    if len(raw) > METADATA_MAX_BYTES:
        return MetadataOutcome(state=MetadataState.FAILED, reason=MetadataReason.RESPONSE_TOO_LARGE)
    measurement = {"response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw)}

    def indeterminate(reason: MetadataReason) -> MetadataOutcome:
        return MetadataOutcome.model_validate(
            measurement | {"state": MetadataState.INDETERMINATE, "reason": reason}
        )

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        return indeterminate(MetadataReason.INVALID_RESPONSE)
    if not isinstance(payload, dict):
        return indeterminate(MetadataReason.INVALID_RESPONSE)
    if ("success" in payload and payload["success"] is not True) or payload.get("code") not in (
        None,
        "",
    ):
        return MetadataOutcome.model_validate(
            measurement | {"state": MetadataState.FAILED, "reason": MetadataReason.PROVIDER_ERROR}
        )
    output = payload.get("output")
    if not isinstance(output, dict):
        return indeterminate(MetadataReason.INVALID_RESPONSE)
    if any(
        type(output.get(key)) is not int or not 0 <= output[key] <= 2147483647
        for key in ("total", "page_no", "page_size")
    ):
        return indeterminate(MetadataReason.INVALID_PAGE)
    if output["page_no"] != METADATA_PAGE_NO or output["page_size"] != METADATA_PAGE_SIZE:
        return indeterminate(MetadataReason.INVALID_PAGE)
    models = output.get("models")
    if not isinstance(models, list):
        return indeterminate(MetadataReason.INVALID_RESPONSE)
    if output["total"] == 0 and models == []:
        return MetadataOutcome.model_validate(measurement | {"state": MetadataState.NOT_FOUND})
    if output["total"] > 1 or len(models) > 1:
        return indeterminate(MetadataReason.RESULT_NOT_UNIQUE)
    if output["total"] != 1 or len(models) != 1:
        return indeterminate(MetadataReason.INCOMPLETE_RESULT)
    model = models[0]
    if (
        not isinstance(model, dict)
        or model.get("model") != model_id
        or model.get("inference_provider") != inference_provider
    ):
        return indeterminate(MetadataReason.MODEL_IDENTITY_MISMATCH)
    info = model.get("model_info", {})
    if not isinstance(info, dict):
        return indeterminate(MetadataReason.INVALID_RESPONSE)

    def limit(key: str) -> ReportedLimit:
        if key not in info:
            return ReportedLimit(state="MISSING")
        if info[key] is None:
            return ReportedLimit(state="NULL")
        return ReportedLimit(state="VALUE", value=info[key])

    try:
        limits = {
            key: limit(key) for key in ("context_window", "max_input_tokens", "max_output_tokens")
        }
    except ValidationError:
        return indeterminate(MetadataReason.INVALID_TOKEN_VALUE)
    try:
        report = MetadataReport.model_validate(
            {
                "model": model["model"],
                "provider": model.get("provider"),
                "inference_provider": model["inference_provider"],
                **limits,
            }
        )
    except ValidationError:
        return indeterminate(MetadataReason.MODEL_IDENTITY_MISMATCH)
    identifier = None
    if payload.get("request_id") is not None:
        try:
            identifier = provider_request_id(payload["request_id"])
        except ValueError:
            return indeterminate(MetadataReason.INVALID_RESPONSE)
    return MetadataOutcome.model_validate(
        measurement
        | {
            "state": MetadataState.REPORTED,
            "report": report,
            "provider_request_id": identifier,
        }
    )


class HttpxModelMetadata:
    def __init__(
        self,
        secrets: ModelSecretPort,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[tuple[str, ...]]] = public_addresses,
    ) -> None:
        self.secrets, self.transport, self.resolver = secrets, transport, resolver

    def _material(self, source: MetadataSourceDefinition) -> ProviderSecretMaterial:
        try:
            material = self.secrets.resolve(source.secret_ref)
            if re.fullmatch(r"[A-Za-z0-9._/-]{8,8192}", material.value.get_secret_value()) is None:
                raise ValueError
            return material
        except (CheckBlocked, ValueError, ValidationError):
            raise MetadataBlocked(MetadataReason.MATERIAL_UNAVAILABLE) from None

    def material_version(self, source: MetadataSourceDefinition) -> str:
        return self._material(source).version

    def prepare(self, source: MetadataSourceDefinition) -> PreparedMetadata:
        material = self._material(source)
        if material.version != source.material_version:
            raise MetadataBlocked(MetadataReason.MATERIAL_VERSION_CHANGED)
        return PreparedMetadata(source, material)

    def observe(self, prepared: PreparedMetadata, model_id: str) -> MetadataOutcome:
        started = time.monotonic()
        result = asyncio.run(self._observe(prepared, model_id))
        if prepared.material.value.get_secret_value() in result.model_dump_json():
            result = MetadataOutcome(
                state=MetadataState.FAILED, reason=MetadataReason.INVALID_RESPONSE
            )
        return result.model_copy(update={"elapsed_ms": int((time.monotonic() - started) * 1000)})

    async def _observe(self, prepared: PreparedMetadata, model_id: str) -> MetadataOutcome:
        deadline = asyncio.get_running_loop().time() + METADATA_TIMEOUT_SECONDS
        client: httpx.AsyncClient | None = None
        response: httpx.Response | None = None
        sent = False
        close_failed = False
        result = MetadataOutcome(
            state=MetadataState.UNKNOWN, reason=MetadataReason.REQUEST_OUTCOME_UNKNOWN
        )
        try:
            async with asyncio.timeout_at(deadline):
                hostname = prepared.source.hostname
                address = await verified_public_address(hostname, self.resolver)
                client = model_https_client(self.transport, METADATA_TIMEOUT_SECONDS)
                target = httpx.URL(f"https://{hostname}/api/v1/models").copy_with(host=address)
                request = client.build_request(
                    "GET",
                    target,
                    params={
                        "model": model_id,
                        "inference_providers": prepared.source.inference_provider,
                        "service_site": prepared.source.service_site,
                        "page_no": METADATA_PAGE_NO,
                        "page_size": METADATA_PAGE_SIZE,
                    },
                    headers={
                        "Host": hostname,
                        "Authorization": f"Bearer {prepared.material.value.get_secret_value()}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    extensions={"sni_hostname": hostname},
                )
                sent = True
                response = await client.send(request, stream=True)
                if response.status_code != 200:
                    reason = (
                        MetadataReason.REDIRECT_REJECTED
                        if 300 <= response.status_code < 400
                        else MetadataReason.PROVIDER_RATE_LIMITED
                        if response.status_code == 429
                        else MetadataReason.PROVIDER_REJECTED
                        if 400 <= response.status_code < 500
                        else MetadataReason.PROVIDER_ERROR
                    )
                    result = MetadataOutcome(state=MetadataState.FAILED, reason=reason)
                elif (
                    response.headers.get("content-encoding", "identity").lower() != "identity"
                    or response.headers.get("content-type", "").split(";")[0].strip().lower()
                    != "application/json"
                ):
                    result = MetadataOutcome(
                        state=MetadataState.FAILED, reason=MetadataReason.INVALID_RESPONSE
                    )
                else:
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw) + len(chunk) > METADATA_MAX_BYTES:
                            result = MetadataOutcome(
                                state=MetadataState.FAILED, reason=MetadataReason.RESPONSE_TOO_LARGE
                            )
                            break
                        raw.extend(chunk)
                    else:
                        result = parse_metadata(
                            bytes(raw), model_id, prepared.source.inference_provider
                        )
        except CheckBlocked as error:
            result = MetadataOutcome(
                state=MetadataState.FAILED, reason=MetadataReason(error.reason.value)
            )
        except (OSError, TimeoutError, httpx.HTTPError):
            result = MetadataOutcome(
                state=MetadataState.UNKNOWN if sent else MetadataState.FAILED,
                reason=MetadataReason.REQUEST_OUTCOME_UNKNOWN
                if sent
                else MetadataReason.TARGET_UNAVAILABLE,
            )
        finally:
            for resource in (response, client):
                if resource is None:
                    continue
                try:
                    async with asyncio.timeout_at(deadline):
                        await resource.aclose()
                except Exception:
                    close_failed = True
        if close_failed:
            return MetadataOutcome(
                state=MetadataState.UNKNOWN, reason=MetadataReason.RESPONSE_CLOSE_FAILED
            )
        return result
