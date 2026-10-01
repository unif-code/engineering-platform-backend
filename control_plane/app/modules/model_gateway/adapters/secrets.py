from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked, CheckReason
from control_plane.app.modules.model_gateway.ports.checks import ProviderSecretMaterial
from control_plane.app.shared.security.file_references import (
    FileSecretReferenceReader,
    SecretReferenceUnavailable,
)


@dataclass(frozen=True)
class FileModelSecretPort:
    root: Path | None

    def resolve(self, reference: str) -> ProviderSecretMaterial:
        try:
            if self.root is None:
                raise ValueError
            return ProviderSecretMaterial.model_validate_json(
                FileSecretReferenceReader(self.root).resolve(reference)
            )
        except (SecretReferenceUnavailable, ValueError, ValidationError):
            raise CheckBlocked(CheckReason.MATERIAL_UNAVAILABLE) from None
