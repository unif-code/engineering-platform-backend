import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from control_plane.app.modules.model_gateway import CatalogDependencies, CatalogError
from control_plane.app.modules.model_gateway.domain import Deployment
from control_plane.app.modules.model_gateway.domain.connections import digest
from control_plane.app.modules.model_gateway.domain.dossiers import (
    DeclaredMaterial,
    ValidationDossier,
)
from control_plane.app.modules.model_gateway.domain.source_checks import (
    MaterialSourceCheck,
    SourceInspection,
)
from tests.model_gateway.test_material_source_copy import REFERENCE, source

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def setup_service(
    tmp_path: Path, declaration: Literal["matching", "different", "missing"] = "matching"
) -> tuple[Any, Any, Any, Path, Path, dict[str, Any], list[Any]]:
    name = "control_plane.app.modules.model_gateway.application.source_checks"
    assert importlib.util.find_spec(name) is not None, "source check application is missing"
    service_class = __import__(
        name, fromlist=["ModelMaterialSourceChecks"]
    ).ModelMaterialSourceChecks
    port, root, manifest, entry = source(tmp_path, b"first")
    candidate = Deployment.model_validate(
        {
            "id": str(uuid4()),
            "deployment_key": "test-model",
            "display_name": "Test model",
            "provider_kind": "BAILIAN_COMPATIBLE_MODE",
            "provider_model_id": "synthetic-model",
            "declared_capabilities": [],
            "revision": 1,
            "state": "DRAFT",
            "created_by": "admin",
            "created_at": NOW,
            "updated_by": "admin",
            "updated_at": NOW,
        }
    )
    material = DeclaredMaterial.model_validate(
        {
            "category": "CONTEXT_LIMITS",
            "title": "Declared source",
            "source_reference": REFERENCE,
            "external_version": "2026-10",
            "expires_at": NOW + timedelta(days=1),
            "declared_content_sha256": entry["copySha256"]
            if declaration == "matching"
            else "0" * 64
            if declaration == "different"
            else None,
        }
    )
    payload = {
        "id": str(uuid4()),
        "deployment_id": candidate.id,
        "candidate_revision": 1,
        "created_by": "admin",
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
        "materials": [material.model_dump(mode="json")],
        "checks": [],
    }
    dossier = ValidationDossier.model_validate(payload | {"snapshot_hash": digest(payload)})

    class Store:
        db = object()
        deployment = candidate
        archive: ValidationDossier
        records: dict[str, MaterialSourceCheck] = {}

        def get(self, identifier: str, *, for_update: bool = False) -> Deployment | None:
            return self.deployment if identifier == self.deployment.id else None

        def dossier(self, identifier: str) -> ValidationDossier | None:
            return self.archive if identifier == self.archive.id else None

        def insert_source_check(self, value: MaterialSourceCheck) -> None:
            self.records[value.id] = value

        def source_check(self, identifier: str) -> MaterialSourceCheck | None:
            return self.records.get(identifier)

    class CountingPort:
        calls = 0

        def inspect(self, reference: str, external_version: str | None) -> SourceInspection:
            self.calls += 1
            return cast(SourceInspection, port.inspect(reference, external_version))

    store, counted = Store(), CountingPort()
    store.archive = dossier
    audits: list[Any] = []
    dependencies = CatalogDependencies(
        now=lambda: NOW,
        new_id=uuid4,
        audit=SimpleNamespace(append_in_transaction=lambda db, event: audits.append(event)),
    )
    return (
        service_class(store, dependencies, counted),
        store,
        counted,
        root,
        manifest,
        entry,
        audits,
    )


@pytest.mark.parametrize(
    "declaration,result",
    [("matching", "MATCHED"), ("different", "MISMATCH"), ("missing", "BLOCKED")],
)
def test_record_uses_measured_bytes_and_never_upgrades_dossier_provenance(
    tmp_path: Path, declaration: Any, result: str
) -> None:
    service, store, port, _, _, entry, audits = setup_service(tmp_path, declaration)
    value = service.create(
        store.deployment.id, store.archive.id, material_index=0, expected_revision=1, actor="admin"
    )
    assert value.result == result
    assert value.dossier_snapshot_hash == store.archive.snapshot_hash
    assert store.archive.materials[0].provenance == "DECLARED"
    assert store.deployment.revision == 1 and len(audits) == 1
    assert str(tmp_path) not in audits[0].reason
    assert "source_reference" not in audits[0].reason
    assert MaterialSourceCheck.model_validate_json(value.model_dump_json()) == value
    from control_plane.app.modules.model_gateway.api.dto import MaterialSourceCheckDetailDto

    public = MaterialSourceCheckDetailDto.model_validate(
        service.project(value, now=NOW).model_dump()
    ).model_dump(mode="json", by_alias=True)
    assert public["snapshot"]["snapshotHash"] == value.snapshot_hash
    assert public["snapshot"]["result"] == result
    if declaration == "missing":
        assert port.calls == 0 and value.observed_sha256 is value.observed_bytes is None
        assert value.reason == "DECLARED_HASH_MISSING"
        assert service.project(value, now=NOW).currentness == "UNVERIFIABLE"
        assert port.calls == 0
    else:
        assert value.observed_sha256 == entry["copySha256"] and value.observed_bytes == 5
        assert service.project(value, now=NOW).currentness == "CURRENT"


def test_actual_content_change_is_stale_even_when_the_manifest_cannot_approve_new_bytes(
    tmp_path: Path,
) -> None:
    service, store, _, root, _, _, _ = setup_service(tmp_path)
    record = service.create(
        store.deployment.id, store.archive.id, material_index=0, expected_revision=1, actor="admin"
    )
    original = record.model_dump_json()
    before = service.project(record, now=NOW)
    (root / "model.bin").write_bytes(b"other")
    later = service.project(record, now=NOW)
    assert later.currentness == "STALE" and "SOURCE_CONTENT_CHANGED" in later.currentness_reasons
    assert later.snapshot.result == "MATCHED" and later.snapshot.model_dump_json() == original
    assert service.etag(before) != service.etag(later)
    (root / "model.bin").unlink()
    assert service.project(record, now=NOW).currentness == "UNVERIFIABLE"
    store.deployment = store.deployment.model_copy(update={"revision": 2})
    assert service.project(record, now=NOW).currentness == "STALE"


def test_mapping_change_and_expiry_are_independent_from_the_historical_result(
    tmp_path: Path,
) -> None:
    service, store, _, _, manifest, entry, _ = setup_service(tmp_path, "different")
    record = service.create(
        store.deployment.id, store.archive.id, material_index=0, expected_revision=1, actor="admin"
    )
    expired = service.project(record, now=NOW + timedelta(days=2))
    assert expired.currentness == "CURRENT" and expired.material_expiration == "EXPIRED"
    assert expired.snapshot.result == "MISMATCH"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "environment": "TEST",
                "sources": [entry | {"sourceVersion": "copy-v2"}],
            }
        )
    )
    changed = service.project(record, now=NOW)
    assert (
        changed.currentness == "STALE" and "SOURCE_BINDING_CHANGED" in changed.currentness_reasons
    )
    manifest.write_text(json.dumps({"schemaVersion": 1, "environment": "TEST", "sources": []}))
    assert service.project(record, now=NOW).currentness == "STALE"


@pytest.mark.parametrize(
    "case", ["candidate-version", "archive", "foreign-dossier", "old-dossier", "index"]
)
def test_invalid_binding_never_reads_the_copy(tmp_path: Path, case: str) -> None:
    service, store, port, _, _, _, audits = setup_service(tmp_path)
    dossier_id, index = store.archive.id, 0
    if case == "candidate-version":
        store.deployment = store.deployment.model_copy(update={"revision": 2})
    elif case == "archive":
        store.deployment = store.deployment.model_copy(update={"state": "ARCHIVED"})
    elif case == "foreign-dossier":
        store.archive = store.archive.model_copy(update={"deployment_id": str(uuid4())})
    elif case == "old-dossier":
        store.archive = store.archive.model_copy(update={"candidate_revision": 2})
    else:
        index = 1
    with pytest.raises(CatalogError):
        service.create(
            store.deployment.id,
            dossier_id,
            material_index=index,
            expected_revision=1,
            actor="admin",
        )
    assert port.calls == 0 and not store.records and not audits
