import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal


class FileReferenceUnavailable(RuntimeError):
    """A safe reason only: never expose a host path or file contents."""

    def __init__(
        self, reason: Literal["UNAVAILABLE", "BOUNDARY", "NOT_REGULAR", "TOO_LARGE", "CHANGED"]
    ) -> None:
        self.reason = reason
        super().__init__(f"File reference unavailable: {reason}")


def relative_file_reference(value: str) -> str:
    relative = PurePosixPath(value)
    if (
        not value
        or len(value) > 255
        or relative.is_absolute()
        or not relative.parts
        or any(part in {".", ".."} for part in relative.parts)
        or any(ord(char) < 32 or ord(char) == 127 or char in "\\:" for char in value)
    ):
        raise ValueError("Invalid relative file reference")
    return str(relative)


def _identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode


def _content_identity(value: os.stat_result) -> tuple[int, ...]:
    return (*_identity(value), value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _reference_chain(root: Path, parts: tuple[str, ...]) -> tuple[tuple[int, ...], ...]:
    result = []
    path = root
    for part in parts:
        path /= part
        entry = path.lstat()
        # Directory contents may change independently; reference identity must not.
        result.append(_identity(entry) if stat.S_ISDIR(entry.st_mode) else _content_identity(entry))
    return tuple(result)


def read_bounded_file(root: Path, reference: str, *, max_bytes: int = 65536) -> bytes:
    """Read exact bytes through pinned directories; reject changed references and files.

    Root-internal aliases keep their existing meaning. No bytes are returned when the
    original reference or the descriptor-backed path changes during the read.
    """
    if not 1 <= max_bytes <= 65536:
        raise ValueError("File read bound is invalid")
    try:
        relative = PurePosixPath(relative_file_reference(reference))
        canonical_root = root.resolve(strict=True)
        root_before = canonical_root.stat()
        candidate = canonical_root.joinpath(*relative.parts).resolve(strict=True)
        inside = candidate.relative_to(canonical_root)
        if not inside.parts:
            raise FileReferenceUnavailable("NOT_REGULAR")
        before = candidate.stat(follow_symlinks=False)
        references = _reference_chain(canonical_root, relative.parts)
        if not stat.S_ISREG(before.st_mode):
            raise FileReferenceUnavailable("NOT_REGULAR")
        if before.st_size > max_bytes:
            raise FileReferenceUnavailable("TOO_LARGE")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        with ExitStack() as stack:
            fd = os.open(canonical_root.anchor, flags | os.O_DIRECTORY)
            stack.callback(os.close, fd)
            checks: list[tuple[int, str, tuple[int, int, int]]] = []
            for name in canonical_root.parts[1:]:
                parent = fd
                fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=parent)
                stack.callback(os.close, fd)
                checks.append((parent, name, _identity(os.fstat(fd))))
            if _identity(os.fstat(fd)) != _identity(root_before):
                raise FileReferenceUnavailable("CHANGED")
            for name in inside.parts[:-1]:
                parent = fd
                fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=parent)
                stack.callback(os.close, fd)
                checks.append((parent, name, _identity(os.fstat(fd))))
            parent = fd
            fd = os.open(inside.name, flags, dir_fd=parent)
            stack.callback(os.close, fd)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise FileReferenceUnavailable("NOT_REGULAR")
            if _content_identity(opened) != _content_identity(before):
                raise FileReferenceUnavailable("CHANGED")
            checks.append((parent, inside.name, _identity(opened)))
            raw = bytearray()
            while len(raw) <= max_bytes:
                chunk = os.read(fd, min(8192, max_bytes + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > max_bytes:
                raise FileReferenceUnavailable("TOO_LARGE")
            after = os.fstat(fd)
            if _content_identity(after) != _content_identity(before) or len(raw) != after.st_size:
                raise FileReferenceUnavailable("CHANGED")
            for parent, name, identity in checks:
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity:
                    raise FileReferenceUnavailable("CHANGED")
            if (
                root.resolve(strict=True) != canonical_root
                or canonical_root.joinpath(*relative.parts).resolve(strict=True) != candidate
                or _reference_chain(canonical_root, relative.parts) != references
            ):
                raise FileReferenceUnavailable("CHANGED")
            return bytes(raw)
    except FileReferenceUnavailable:
        raise
    except (OSError, ValueError, AttributeError, NotImplementedError):
        raise FileReferenceUnavailable("UNAVAILABLE") from None


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
        try:
            raw = read_bounded_file(self.root, match.group(1), max_bytes=self.max_bytes)
            value = raw.decode("utf-8").strip()
            if not value:
                raise ValueError
            return value
        except (FileReferenceUnavailable, UnicodeError, ValueError):
            raise SecretReferenceUnavailable(_UNAVAILABLE) from None
