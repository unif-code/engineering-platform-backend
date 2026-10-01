import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class SecretReferenceUnavailable(RuntimeError):
    """Mounted reference is unavailable; never includes its contents or host path."""


_DEV_REFERENCE = re.compile(r"^secret-ref:([A-Za-z0-9][A-Za-z0-9._/-]{0,254})$")
_UNAVAILABLE = "Secret reference is unavailable"


@dataclass(frozen=True, slots=True)
class FileSecretReferenceReader:
    root: Path
    max_bytes: int = 65536

    def __post_init__(self) -> None:
        if not 1 <= self.max_bytes <= 65536:
            raise ValueError("Secret read bound is invalid")

    def resolve(self, reference: str) -> str:
        match = _DEV_REFERENCE.fullmatch(reference)
        if match is None:
            raise SecretReferenceUnavailable(_UNAVAILABLE)
        relative = PurePosixPath(match.group(1))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise SecretReferenceUnavailable(_UNAVAILABLE)
        try:
            root = self.root.resolve(strict=True)
            candidate = root.joinpath(*relative.parts).resolve(strict=True)
            candidate.relative_to(root)
            if not candidate.is_file() or candidate.stat().st_size > self.max_bytes:
                raise OSError
            with candidate.open("rb") as stream:
                raw = stream.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise OSError
            value = raw.decode("utf-8").strip()
            if not value:
                raise ValueError
            return value
        except (OSError, UnicodeError, ValueError):
            raise SecretReferenceUnavailable(_UNAVAILABLE) from None
