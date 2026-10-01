from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked, CheckReason
from control_plane.app.modules.model_gateway.domain.connections import (
    ConnectionDefinition,
    ConnectionManifest,
)


class ModelConnectionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MODEL_GATEWAY_", extra="ignore")
    environment: str = "DEV"
    connections_path: Path | None = None


class FileConnectionDirectory:
    """Only the non-secret manifest is mounted into Control Plane."""

    def __init__(self, settings: ModelConnectionSettings) -> None:
        self.settings = settings

    def resolve(self, reference: str | None) -> tuple[str, ConnectionDefinition]:
        if reference is None:
            raise CheckBlocked(CheckReason.CONNECTION_MISSING)
        try:
            if self.settings.connections_path is None:
                raise ValueError
            with self.settings.connections_path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError
            manifest = ConnectionManifest.model_validate_json(raw)
            if manifest.environment != self.settings.environment:
                raise ValueError
            connection = next(
                (item for item in manifest.connections if item.reference == reference), None
            )
            if connection is None:
                raise ValueError
            return manifest.environment, connection
        except (OSError, ValueError, ValidationError):
            raise CheckBlocked(CheckReason.CONNECTION_UNAVAILABLE) from None
