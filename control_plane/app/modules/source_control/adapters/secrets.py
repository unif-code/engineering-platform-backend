from dataclasses import dataclass
from pathlib import Path

from control_plane.app.modules.source_control import SourceControlDependencyUnavailable
from control_plane.app.shared.security.file_references import (
    FileSecretReferenceReader,
    SecretReferenceUnavailable,
)


@dataclass(frozen=True, slots=True)
class DevSecretReferenceResolver:
    root: Path
    max_bytes: int = 65536

    def __post_init__(self) -> None:
        FileSecretReferenceReader(self.root, self.max_bytes)

    def resolve(self, reference: str) -> str:
        try:
            return FileSecretReferenceReader(self.root, self.max_bytes).resolve(reference)
        except SecretReferenceUnavailable:
            raise SourceControlDependencyUnavailable(
                "Source Control secret is unavailable"
            ) from None
