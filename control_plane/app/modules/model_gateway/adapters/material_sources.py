import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict

from control_plane.app.modules.model_gateway.domain.connections import (
    EnvironmentName,
    VersionLabel,
    digest,
)
from control_plane.app.modules.model_gateway.domain.dossiers import (
    ExternalVersion,
    Sha256,
    SourceReference,
)
from control_plane.app.modules.model_gateway.domain.source_checks import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_ENTRIES,
    SourceCheckReason,
    SourceInspection,
)
from control_plane.app.shared.security.file_references import (
    FileReferenceUnavailable,
    read_bounded_file,
    relative_file_reference,
)


class MaterialSourceEntry(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True
    )
    source_id: VersionLabel
    source_version: VersionLabel
    source_reference: SourceReference
    external_version: ExternalVersion | None
    relative_path: Annotated[
        str,
        StringConstraints(min_length=1, max_length=255),
        AfterValidator(relative_file_reference),
    ]
    copy_sha256: Sha256


class MaterialSourceManifest(BaseModel):
    """Server-approved metadata only; byte bounds apply to manifest and every copy."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=to_camel,
        populate_by_name=True,
        json_schema_extra={
            "x-maxManifestBytes": MAX_SOURCE_BYTES,
            "x-maxCopyBytes": MAX_SOURCE_BYTES,
        },
    )
    schema_version: Literal[1]
    environment: EnvironmentName
    sources: tuple[MaterialSourceEntry, ...] = Field(max_length=MAX_SOURCE_ENTRIES)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest schema version must be an integer")
        return value

    @model_validator(mode="after")
    def unique_mapping(self) -> Self:
        keys = {(entry.source_reference, entry.external_version) for entry in self.sources}
        if len(keys) != len(self.sources):
            raise ValueError("source reference/version mappings must be unique")
        return self


class ModelMaterialSourceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MODEL_GATEWAY_", extra="ignore")
    environment: str = "DEV"
    material_sources_path: Path | None = None
    material_sources_root: Path | None = None


@dataclass(frozen=True)
class FileModelMaterialSource:
    settings: ModelMaterialSourceSettings

    def inspect(self, source_reference: str, external_version: str | None) -> SourceInspection:
        manifest_path, root = (
            self.settings.material_sources_path,
            self.settings.material_sources_root,
        )
        if manifest_path is None or root is None:
            return SourceInspection(reason=SourceCheckReason.SOURCE_DIRECTORY_UNCONFIGURED)
        try:
            manifest = MaterialSourceManifest.model_validate_json(
                read_bounded_file(
                    manifest_path.parent, manifest_path.name, max_bytes=MAX_SOURCE_BYTES
                )
            )
        except (FileReferenceUnavailable, ValidationError, ValueError):
            return SourceInspection(reason=SourceCheckReason.SOURCE_DIRECTORY_INVALID)
        if manifest.environment != self.settings.environment:
            return SourceInspection(reason=SourceCheckReason.SOURCE_ENVIRONMENT_MISMATCH)
        entry = next(
            (
                item
                for item in manifest.sources
                if (item.source_reference, item.external_version)
                == (source_reference, external_version)
            ),
            None,
        )
        if entry is None:
            return SourceInspection(reason=SourceCheckReason.SOURCE_NOT_APPROVED)
        identity = dict(
            source_id=entry.source_id,
            source_version=entry.source_version,
            entry_fingerprint=digest(
                {
                    "environment": manifest.environment,
                    "schemaVersion": manifest.schema_version,
                    "entry": entry.model_dump(mode="json"),
                }
            ),
            approved_copy_sha256=entry.copy_sha256,
        )
        try:
            raw = read_bounded_file(root, entry.relative_path, max_bytes=MAX_SOURCE_BYTES)
        except FileReferenceUnavailable as error:
            reason = {
                "TOO_LARGE": SourceCheckReason.SOURCE_COPY_TOO_LARGE,
                "NOT_REGULAR": SourceCheckReason.SOURCE_COPY_NOT_REGULAR,
                "CHANGED": SourceCheckReason.SOURCE_COPY_CHANGED,
            }.get(error.reason, SourceCheckReason.SOURCE_COPY_UNAVAILABLE)
            return SourceInspection.model_validate(identity | {"reason": reason})
        if not raw:
            return SourceInspection.model_validate(
                identity | {"reason": SourceCheckReason.SOURCE_COPY_EMPTY}
            )
        actual = hashlib.sha256(raw).hexdigest()
        return SourceInspection(
            **identity,
            observed_sha256=actual,
            observed_bytes=len(raw),
            reason=None
            if actual == entry.copy_sha256
            else SourceCheckReason.APPROVED_COPY_HASH_MISMATCH,
        )
