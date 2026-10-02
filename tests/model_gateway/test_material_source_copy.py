import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

REFERENCE = "https://docs.example.org/model%20spec"


def module() -> Any:
    name = "control_plane.app.modules.model_gateway.adapters.material_sources"
    assert importlib.util.find_spec(name) is not None, "material source Port is missing"
    return __import__(name, fromlist=["FileModelMaterialSource"])


def source(tmp_path: Path, raw: bytes = b" copy\r\n") -> tuple[Any, Path, Path, dict[str, Any]]:
    library = module()
    root = tmp_path / "copies"
    root.mkdir()
    (root / "model.bin").write_bytes(raw)
    entry = {
        "sourceId": "provider-doc",
        "sourceVersion": "copy-v1",
        "sourceReference": REFERENCE,
        "externalVersion": "2026-10",
        "relativePath": "model.bin",
        "copySha256": hashlib.sha256(raw).hexdigest(),
    }
    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, "environment": "TEST", "sources": [entry]}))
    settings = library.ModelMaterialSourceSettings(
        _env_file=None,
        environment="TEST",
        material_sources_path=manifest,
        material_sources_root=root,
    )
    return library.FileModelMaterialSource(settings), root, manifest, entry


@pytest.mark.parametrize(
    "raw", [b" leading and trailing \r\n", b"line1\nline2\r\n", b"\xff\x00\x01\xfe"]
)
def test_source_uses_exact_actual_bytes_without_text_normalization(
    tmp_path: Path, raw: bytes
) -> None:
    port, root, _, entry = source(tmp_path, raw)
    value = port.inspect(REFERENCE, "2026-10")
    assert value.reason is None
    assert value.observed_sha256 == hashlib.sha256(raw).hexdigest()
    assert value.observed_bytes == len(raw)
    assert value.source_id == entry["sourceId"] and value.source_version == "copy-v1"
    assert len(value.entry_fingerprint) == 64
    assert (
        str(root) not in value.model_dump_json() and "relative_path" not in value.model_dump_json()
    )


def test_same_size_and_mtime_replacement_is_remeasured_under_the_same_source_version(
    tmp_path: Path,
) -> None:
    port, root, _, _ = source(tmp_path, b"first")
    first = port.inspect(REFERENCE, "2026-10")
    leaf = root / "model.bin"
    before = leaf.stat()
    replacement = root / "new.bin"
    replacement.write_bytes(b"other")
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(leaf)
    later = port.inspect(REFERENCE, "2026-10")
    assert later.source_version == first.source_version
    assert later.entry_fingerprint == first.entry_fingerprint
    assert later.observed_bytes == first.observed_bytes == 5
    assert later.observed_sha256 == hashlib.sha256(b"other").hexdigest()
    assert later.observed_sha256 != first.observed_sha256
    assert later.reason == "APPROVED_COPY_HASH_MISMATCH"


@pytest.mark.parametrize("mode", ["empty", "oversize", "missing", "outside-link", "fifo"])
def test_incomplete_or_disallowed_copy_has_no_fabricated_measurements(
    tmp_path: Path, mode: str
) -> None:
    port, root, _, _ = source(tmp_path)
    leaf = root / "model.bin"
    leaf.unlink()
    if mode == "empty":
        leaf.write_bytes(b"")
    elif mode == "oversize":
        leaf.write_bytes(b"x" * 65537)
    elif mode == "outside-link":
        outside = tmp_path / "not-approved"
        outside.write_bytes(b"never read")
        leaf.symlink_to(outside)
    elif mode == "fifo":
        os.mkfifo(leaf)
    value = port.inspect(REFERENCE, "2026-10")
    assert value.reason is not None
    assert value.observed_sha256 is value.observed_bytes is None
    assert value.source_id == "provider-doc"


@pytest.mark.parametrize(
    "change",
    [
        {"schemaVersion": 2},
        {"schemaVersion": True},
        {"environment": "OTHER"},
        {"extra": "unapproved"},
        {"sources": []},
    ],
)
def test_invalid_or_unapproved_directory_blocks_without_a_default_mapping(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    port, _, manifest, entry = source(tmp_path)
    manifest.write_text(
        json.dumps({"schemaVersion": 1, "environment": "TEST", "sources": [entry]} | change)
    )
    value = port.inspect(REFERENCE, "2026-10")
    assert value.reason is not None and value.observed_sha256 is None


def test_mapping_is_exact_including_null_version_and_normalized_reference(tmp_path: Path) -> None:
    port, _, manifest, entry = source(tmp_path)
    assert port.inspect(REFERENCE, None).reason == "SOURCE_NOT_APPROVED"
    assert port.inspect(REFERENCE + "/extra", "2026-10").reason == "SOURCE_NOT_APPROVED"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "environment": "TEST",
                "sources": [
                    entry,
                    entry | {"sourceReference": REFERENCE + "?variant=1#part"},
                ],
            }
        )
    )
    assert port.inspect(REFERENCE, "2026-10").reason == "SOURCE_DIRECTORY_INVALID"


def test_unconfigured_source_and_declared_hash_comparison_remain_limited(tmp_path: Path) -> None:
    library = module()
    empty = library.FileModelMaterialSource(
        library.ModelMaterialSourceSettings(
            _env_file=None,
            environment="TEST",
            material_sources_path=None,
            material_sources_root=None,
        )
    )
    assert empty.inspect(REFERENCE, None).reason == "SOURCE_DIRECTORY_UNCONFIGURED"
    from control_plane.app.modules.model_gateway.domain.source_checks import compare_declared_source

    port, _, _, entry = source(tmp_path)
    observation = port.inspect(REFERENCE, "2026-10")
    assert compare_declared_source(entry["copySha256"], observation)[0] == "MATCHED"
    assert compare_declared_source("0" * 64, observation)[0] == "MISMATCH"
    assert compare_declared_source(None, None) == ("BLOCKED", "DECLARED_HASH_MISSING")


def test_manifest_has_explicit_bounds_and_forbids_arbitrary_targets(tmp_path: Path) -> None:
    library = module()
    schema = library.MaterialSourceManifest.model_json_schema(by_alias=True)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["sources"]["maxItems"] == 100
    port, _, manifest, entry = source(tmp_path)
    for override in (
        {"relativePath": "../outside"},
        {"copySha256": "fake"},
        {"url": "https://other.example.org"},
    ):
        manifest.write_text(
            json.dumps({"schemaVersion": 1, "environment": "TEST", "sources": [entry | override]})
        )
        assert port.inspect(REFERENCE, "2026-10").reason == "SOURCE_DIRECTORY_INVALID"
