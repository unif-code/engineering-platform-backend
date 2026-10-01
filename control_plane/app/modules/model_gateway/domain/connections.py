"""Non-secret, server-managed admission data; never a deployment or routing policy."""

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

from control_plane.app.modules.model_gateway.domain import (
    ConnectionRef,
    ProviderKind,
    ProviderModelId,
)

VersionLabel = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
EnvironmentName = Annotated[
    str, StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
]
Region = Literal[
    "cn-beijing", "ap-southeast-1", "us-east-1", "cn-hongkong", "eu-central-1", "ap-northeast-1"
]


class CheckKind(StrEnum):
    BASIC_TEXT = "BASIC_TEXT"
    STREAM_TEXT = "STREAM_TEXT"
    STREAM_STOP = "STREAM_STOP"


PROBE_VERSIONS = {
    CheckKind.BASIC_TEXT: "basic-text-v1",
    CheckKind.STREAM_TEXT: "stream-text-v1",
    CheckKind.STREAM_STOP: "stream-stop-v1",
}
MAX_STREAM_EVENT_BYTES = 16384
MAX_STREAM_EVENTS = 256
ADAPTER_VERSION = "bailian-compatible-v1"
PROBE_TEXT = "Reply with OK."
MAX_COMPLETION_TOKENS = 64
MAX_RESPONSE_BYTES = 65536
PROBE_TIMEOUT_SECONDS = 20
EXECUTION_LEASE_SECONDS = 25


class ConnectionDefinition(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True
    )
    reference: ConnectionRef
    version: VersionLabel
    material_version: VersionLabel
    provider_kind: ProviderKind
    region: Region
    workspace_id: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    allowed_model_ids: tuple[ProviderModelId, ...] = Field(min_length=1, max_length=100)
    secret_ref: str = Field(
        min_length=12, max_length=266, pattern=r"^secret-ref:[A-Za-z0-9][A-Za-z0-9._/-]*$"
    )

    @property
    def hostname(self) -> str:
        return f"{self.workspace_id}.{self.region}.maas.aliyuncs.com"

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class ConnectionManifest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True
    )
    environment: EnvironmentName
    connections: tuple[ConnectionDefinition, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        if len({item.reference for item in self.connections}) != len(self.connections):
            raise ValueError("connection references must be unique")
        return self


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def probe_body(model_id: str, check_kind: CheckKind) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model_id,
        "messages": [{"role": "user", "content": PROBE_TEXT}],
        "stream": check_kind is not CheckKind.BASIC_TEXT,
        "enable_thinking": False,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
    }

    if check_kind is not CheckKind.BASIC_TEXT:
        body["stream_options"] = {"include_usage": True}
    return body
