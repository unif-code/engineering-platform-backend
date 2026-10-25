from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from control_plane.app.modules.model_gateway.domain.connections import EnvironmentName
from control_plane.app.modules.model_gateway.domain.metadata import (
    METADATA_MAX_BYTES,
    MetadataBlocked,
    MetadataModel,
    MetadataReason,
    MetadataSourceDefinition,
)
from control_plane.app.shared.security.file_references import (
    FileReferenceUnavailable,
    read_bounded_file,
)


class MetadataSourceManifest(MetadataModel):
    schema_version: Literal[1]
    environment: EnvironmentName
    sources: tuple[MetadataSourceDefinition, ...] = Field(max_length=100)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        if len({x.connection_ref for x in self.sources}) != len(self.sources):
            raise ValueError("metadata source must be unique per connection")
        if len({x.source_id for x in self.sources}) != len(self.sources):
            raise ValueError("metadata source IDs must be unique")
        return self


class MetadataSourceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MODEL_GATEWAY_", extra="ignore")
    environment: str = "DEV"
    metadata_sources_path: Path | None = None


class FileMetadataSourceDirectory:
    def __init__(self, settings: MetadataSourceSettings) -> None:
        self.settings = settings

    def resolve(self, reference: str | None) -> tuple[str, MetadataSourceDefinition]:
        path = self.settings.metadata_sources_path
        if path is None:
            raise MetadataBlocked(MetadataReason.SOURCE_UNCONFIGURED)
        try:
            manifest = MetadataSourceManifest.model_validate_json(
                read_bounded_file(path.parent, path.name, max_bytes=METADATA_MAX_BYTES)
            )
        except (FileReferenceUnavailable, ValidationError, ValueError):
            raise MetadataBlocked(MetadataReason.SOURCE_UNAVAILABLE) from None
        if manifest.environment != self.settings.environment:
            raise MetadataBlocked(MetadataReason.SOURCE_SCOPE_MISMATCH)
        value = next((x for x in manifest.sources if x.connection_ref == reference), None)
        if value is None:
            raise MetadataBlocked(MetadataReason.SOURCE_NOT_APPROVED)
        return manifest.environment, value
