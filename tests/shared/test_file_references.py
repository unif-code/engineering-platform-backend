import os
from pathlib import Path

import pytest

from control_plane.app.shared.security import file_references


def read(root: Path, relative: str, limit: int = 65536) -> bytes:
    assert hasattr(file_references, "read_bounded_file"), "raw bounded reader is missing"
    return file_references.read_bounded_file(root, relative, max_bytes=limit)


def test_original_bytes_and_root_internal_reference_are_preserved(tmp_path: Path) -> None:
    raw = b" \r\n\xff\x00copy\n "
    (tmp_path / "copy.bin").write_bytes(raw)
    (tmp_path / "alias.bin").symlink_to("copy.bin")
    assert read(tmp_path, "copy.bin") == raw
    assert read(tmp_path, "alias.bin") == raw


@pytest.mark.parametrize(
    "relative", ["../outside", "/etc/passwd", "nested/../../outside", "bad\\name", "bad\x00name"]
)
def test_invalid_or_escaping_reference_is_rejected(tmp_path: Path, relative: str) -> None:
    assert hasattr(file_references, "FileReferenceUnavailable")
    with pytest.raises(file_references.FileReferenceUnavailable):
        read(tmp_path, relative)


def test_outside_link_directory_fifo_and_oversize_are_rejected(tmp_path: Path) -> None:
    assert hasattr(file_references, "FileReferenceUnavailable")
    root = tmp_path / "copies"
    root.mkdir()
    (tmp_path / "outside").write_bytes(b"outside-body")
    (root / "escape").symlink_to(tmp_path / "outside")
    (root / "directory").mkdir()
    os.mkfifo(root / "fifo")
    (root / "oversize").write_bytes(b"12345")
    for name in ("escape", "directory", "fifo", "oversize"):
        with pytest.raises(file_references.FileReferenceUnavailable) as error:
            read(root, name, 4)
        assert str(tmp_path) not in str(error.value)
        assert "outside-body" not in str(error.value)


@pytest.mark.parametrize("replacement", ["file", "outside-link", "fifo", "directory"])
def test_leaf_replacement_before_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    assert hasattr(file_references, "FileReferenceUnavailable")
    root = tmp_path / "root"
    root.mkdir()
    leaf = root / "copy"
    leaf.write_bytes(b"old")
    outside = tmp_path / "outside"
    outside.write_bytes(b"must-not-read")
    original = os.open
    replaced = False

    def swap(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if str(path) == "copy" and not replaced:
            replaced = True
            leaf.unlink()
            if replacement == "file":
                leaf.write_bytes(b"new")
            elif replacement == "outside-link":
                leaf.symlink_to(outside)
            elif replacement == "fifo":
                os.mkfifo(leaf)
            else:
                leaf.mkdir()
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(file_references.FileReferenceUnavailable):
        read(root, "copy")
    assert replaced


@pytest.mark.parametrize("change", ["same-size-content", "path-replacement", "parent-replacement"])
def test_read_time_changes_are_not_complete_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    assert hasattr(file_references, "FileReferenceUnavailable")
    folder = tmp_path / "nested"
    folder.mkdir()
    leaf = folder / "copy"
    leaf.write_bytes(b"old")
    before = leaf.stat()
    original = os.read
    changed = False

    def mutate(fd: int, size: int) -> bytes:
        nonlocal changed
        raw = original(fd, size)
        if not changed:
            changed = True
            if change == "same-size-content":
                leaf.write_bytes(b"new")
                os.utime(leaf, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif change == "path-replacement":
                new = folder / "replacement"
                new.write_bytes(b"new")
                new.replace(leaf)
            else:
                folder.rename(tmp_path / "old-parent")
                folder.mkdir()
                leaf.write_bytes(b"new")
        return raw

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(file_references.FileReferenceUnavailable):
        read(tmp_path, "nested/copy")
    assert changed


def test_secret_consumers_keep_text_semantics_and_existing_limits(tmp_path: Path) -> None:
    from control_plane.app.modules.model_gateway.adapters.secrets import FileModelSecretPort
    from control_plane.app.modules.model_gateway.domain.checks import CheckBlocked
    from control_plane.app.modules.source_control.adapters.secrets import DevSecretReferenceResolver
    from control_plane.app.shared.security.file_references import FileSecretReferenceReader

    (tmp_path / "model").write_bytes(b' \n{"version":"v1","value":"synthetic-secret-only"}\r\n')
    (tmp_path / "pat").write_bytes(b"  synthetic-token-only\r\n")
    model = FileModelSecretPort(tmp_path).resolve("secret-ref:model")
    assert model.version == "v1" and model.value.get_secret_value() == "synthetic-secret-only"
    assert DevSecretReferenceResolver(tmp_path).resolve("secret-ref:pat") == "synthetic-token-only"
    with pytest.raises(ValueError):
        FileSecretReferenceReader(tmp_path, max_bytes=65537)
    (tmp_path / "model").write_bytes(b" " * 65537)
    with pytest.raises(CheckBlocked):
        FileModelSecretPort(tmp_path).resolve("secret-ref:model")
