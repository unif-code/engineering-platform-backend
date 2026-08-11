from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from control_plane.app.shared.db.settings import SecuritySettings

_MATERIAL_SIZE = 32


class SecretMaterialUnavailable(RuntimeError):
    """Secret material could not be loaded or safely validated."""


@dataclass(frozen=True, slots=True)
class SecretMaterial:
    password_pepper: bytes
    totp_sealing_key: bytes
    idempotency_sealing_key: bytes

    def __post_init__(self) -> None:
        values = (
            self.password_pepper,
            self.totp_sealing_key,
            self.idempotency_sealing_key,
        )
        if not all(len(value) == _MATERIAL_SIZE for value in values) or len(set(values)) != 3:
            raise SecretMaterialUnavailable("secret material is unavailable")

    @classmethod
    def load(cls, path: str | Path) -> "SecretMaterial":
        """Load DEV file material directly; consumers should use SecretManagerPort."""
        try:
            directory = Path(path)
            material = cls(
                password_pepper=(directory / "pepper").read_bytes(),
                totp_sealing_key=(directory / "totp_key").read_bytes(),
                idempotency_sealing_key=(directory / "idempotency_key").read_bytes(),
            )
        except OSError:
            raise SecretMaterialUnavailable("secret material is unavailable") from None
        return material


class SecretManagerPort(Protocol):
    """Stable dependency seam for obtaining validated secret material."""

    def load(self) -> SecretMaterial: ...


@dataclass(frozen=True, slots=True)
class FileSecretManager:
    """DEV-003 adapter that reads mounted files from the configured directory."""

    settings: SecuritySettings

    def load(self) -> SecretMaterial:
        return SecretMaterial.load(self.settings.secret_material_path)
