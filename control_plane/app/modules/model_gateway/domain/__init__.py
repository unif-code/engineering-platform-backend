import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

_CREDENTIAL = re.compile(
    r"(?i)(?:\bsk-[a-z0-9_-]+|\bbearer\s+\S+|\b(?:api[_-]?key|access[_-]?token)\s*[:=]|"
    r"-----BEGIN .*PRIVATE KEY-----)"
)


def _non_secret(value: str) -> str:
    if _CREDENTIAL.search(value):
        raise ValueError("credentials are not candidate metadata")
    return value


DeploymentKey = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
    AfterValidator(_non_secret),
]
ProviderModelId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"),
    AfterValidator(_non_secret),
]
ConnectionRef = Annotated[
    str,
    StringConstraints(
        min_length=18,
        max_length=128,
        pattern=r"^model-connection:[a-z][a-z0-9]*([._-][a-z0-9]+)*$",
    ),
    AfterValidator(_non_secret),
]
Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
    AfterValidator(_non_secret),
]
ArchiveReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    AfterValidator(_non_secret),
]
TokenLimit = Annotated[int, Field(strict=True, gt=0, le=2147483647)]


class ProviderKind(StrEnum):
    BAILIAN_COMPATIBLE_MODE = "BAILIAN_COMPATIBLE_MODE"


class DeclaredCapability(StrEnum):
    CHAT = "chat"
    CODING = "coding"
    SEARCH = "search"
    THINKING = "thinking"


def _unique_capabilities(values: list[DeclaredCapability]) -> list[DeclaredCapability]:
    if len(set(values)) != len(values):
        raise ValueError("declared capabilities must be unique")
    return values


DeclaredCapabilities = Annotated[
    list[DeclaredCapability],
    Field(max_length=4, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_unique_capabilities),
]


class DeploymentState(StrEnum):
    DRAFT = "DRAFT"
    ARCHIVED = "ARCHIVED"


class DeploymentDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: DisplayName
    provider_kind: ProviderKind
    provider_model_id: ProviderModelId
    declared_capabilities: DeclaredCapabilities
    connection_ref: ConnectionRef | None = None
    declared_context_window: TokenLimit | None = None
    declared_max_output_tokens: TokenLimit | None = None
    description: Description | None = None


class CreateDeployment(DeploymentDeclaration):
    deployment_key: DeploymentKey


class PatchDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra={"minProperties": 1})

    display_name: DisplayName | SkipJsonSchema[None] = None
    provider_kind: ProviderKind | SkipJsonSchema[None] = None
    provider_model_id: ProviderModelId | SkipJsonSchema[None] = None
    declared_capabilities: DeclaredCapabilities | SkipJsonSchema[None] = None
    connection_ref: ConnectionRef | None = None
    declared_context_window: TokenLimit | None = None
    declared_max_output_tokens: TokenLimit | None = None
    description: Description | None = None

    @model_validator(mode="after")
    def valid_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one editable field is required")
        for field in (
            "display_name",
            "provider_kind",
            "provider_model_id",
            "declared_capabilities",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class Deployment(CreateDeployment):
    id: str
    revision: int = Field(ge=1)
    state: DeploymentState
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    archived_by: str | None = None
    archived_at: datetime | None = None
    archive_reason: ArchiveReason | None = None


class CatalogError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
