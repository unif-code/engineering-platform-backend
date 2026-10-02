from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from control_plane.app.modules.model_gateway import CatalogDependencies, CatalogError
from control_plane.app.modules.model_gateway.domain import Deployment, DeploymentState, ProviderKind
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckBlocked,
    CheckInputSnapshot,
    CheckReason,
    CheckState,
    ConnectionCheck,
    InputCurrentness,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    CheckKind,
    ConnectionDefinition,
)
from control_plane.app.modules.model_gateway.domain.dossiers import (
    CreateValidationDossier,
    ValidationDossier,
)
from tests.model_gateway.test_probe import CONNECTION

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def _request(**values: Any) -> CreateValidationDossier:
    return CreateValidationDossier.model_validate(values)


def setup_service() -> tuple[Any, Any, ConnectionCheck, SimpleNamespace, list[Any]]:
    from control_plane.app.modules.model_gateway import application

    assert hasattr(application, "dossiers"), "dossier application is missing"
    cls = import_module(application.__name__ + ".dossiers").ModelValidationDossiers
    candidate = Deployment(
        id=str(uuid4()),
        deployment_key="test-model",
        display_name="Test model",
        provider_kind=ProviderKind.BAILIAN_COMPATIBLE_MODE,
        provider_model_id="synthetic-model",
        declared_capabilities=[],
        connection_ref=str(CONNECTION["reference"]),
        revision=1,
        state=DeploymentState.DRAFT,
        created_by="admin",
        created_at=NOW,
        updated_by="admin",
        updated_at=NOW,
    )
    connection = ConnectionDefinition.model_validate(CONNECTION)
    check = ConnectionCheck(
        id=str(uuid4()),
        deployment_id=candidate.id,
        revision=3,
        requested_by="admin",
        requested_at=NOW,
        check_kind=CheckKind.BASIC_TEXT,
        input=CheckInputSnapshot.capture(candidate, connection, "TEST", CheckKind.BASIC_TEXT),
        state=CheckState.UNKNOWN,
        reason=CheckReason.REQUEST_OUTCOME_UNKNOWN,
        attempt=1,
        execution_token=str(uuid4()),
        started_at=NOW,
        deadline_at=NOW + timedelta(seconds=25),
        finished_at=NOW,
        elapsed_ms=None,
        provider_request_id=None,
        reported_model_id=None,
        usage=None,
        material_currentness=InputCurrentness.CURRENT,
    )

    class Store:
        db = object()
        deployment = candidate
        checks: dict[str, ConnectionCheck] = {}
        dossiers: dict[str, ValidationDossier] = {}

        def get(self, identifier: str, *, for_update: bool = False) -> Deployment | None:
            return self.deployment if identifier == self.deployment.id else None

        def check(self, identifier: str, *, for_update: bool = False) -> ConnectionCheck | None:
            return self.checks.get(identifier)

        def insert_dossier(self, value: ValidationDossier) -> None:
            self.dossiers[value.id] = value

        def dossier(self, identifier: str) -> ValidationDossier | None:
            return self.dossiers.get(identifier)

    audits: list[Any] = []
    store = Store()
    store.checks = {check.id: check}
    directory = SimpleNamespace(resolve=lambda _: ("TEST", connection))
    dependencies = CatalogDependencies(
        audit=SimpleNamespace(append_in_transaction=lambda db, event: audits.append(event)),
        now=lambda: NOW,
        new_id=uuid4,
    )
    return cls(store, dependencies, directory), store, check, directory, audits


def test_terminal_unknown_snapshot_and_material_expiration_do_not_become_verified() -> None:
    service, store, check, directory, audits = setup_service()
    request = _request(
        materials=[
            {
                "category": "PRICING",
                "title": "Declared price source",
                "source_reference": "urn:provider:price",
                "expires_at": NOW + timedelta(seconds=1),
                "declared_content_sha256": "a" * 64,
            }
        ],
        check_ids=[check.id],
    )
    dossier = service.create(store.deployment.id, request, expected_revision=1, actor="admin")
    assert store.deployment.revision == 1
    assert dossier.materials[0].provenance == "DECLARED"
    assert dossier.checks[0].result_summary.state == "UNKNOWN"
    assert dossier.checks[0].input_digest == check.input.input_digest
    assert len(audits) == 1 and "urn:" not in audits[0].reason
    serialized = dossier.model_dump_json()
    restored = ValidationDossier.model_validate_json(serialized)
    before = service.project(restored, now=NOW)
    after = service.project(restored, now=NOW + timedelta(seconds=1))
    from control_plane.app.modules.model_gateway.api.dto import ValidationDossierDetailDto

    public = ValidationDossierDetailDto.model_validate(before.model_dump()).model_dump(
        mode="json", by_alias=True
    )
    assert public["snapshot"]["snapshotHash"] == dossier.snapshot_hash
    assert public["snapshot"]["checks"][0]["resultSummary"]["state"] == "UNKNOWN"
    assert before.snapshot == after.snapshot == restored
    assert before.material_statuses[0].expiration == "NOT_EXPIRED"
    assert after.material_statuses[0].expiration == "EXPIRED"
    assert before.currentness == after.currentness == "CURRENT"
    assert {item.category: item.status for item in after.material_coverage}["PRICING"] == "EXPIRED"
    assert {item.category: item.status for item in before.material_coverage}["HEALTH"] == "MISSING"
    assert sum(item.status == "PROVIDED" for item in before.check_coverage) == 1
    assert service.etag(before) != service.etag(after)
    assert dossier.model_dump_json() == serialized

    directory.resolve = lambda _: (_ for _ in ()).throw(
        CheckBlocked(CheckReason.CONNECTION_UNAVAILABLE)
    )
    unavailable = service.project(restored, now=NOW)
    assert unavailable.currentness == "UNVERIFIABLE"
    store.deployment = store.deployment.model_copy(update={"revision": 2})
    assert service.project(restored, now=NOW).currentness == "STALE"


def test_unavailable_or_changed_check_never_rewrites_frozen_reference() -> None:
    service, store, check, _, _ = setup_service()
    dossier = service.create(
        store.deployment.id, _request(check_ids=[check.id]), expected_revision=1, actor="admin"
    )
    store.checks.clear()
    missing = service.project(dossier, now=NOW)
    assert missing.currentness == "UNVERIFIABLE"
    assert missing.check_coverage[0].currentness_reasons == ("CHECK_UNAVAILABLE",)
    store.checks[check.id] = check.model_copy(update={"revision": check.revision + 1})
    changed = service.project(dossier, now=NOW)
    assert changed.currentness == "UNVERIFIABLE"
    assert changed.check_coverage[0].currentness_reasons == ("CHECK_SNAPSHOT_MISMATCH",)
    assert changed.snapshot == dossier == missing.snapshot


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "UNKNOWN", "BLOCKED"])
def test_all_terminal_results_are_recorded_without_requiring_success(state: str) -> None:
    service, store, check, _, _ = setup_service()
    store.checks[check.id] = check.model_copy(update={"state": CheckState(state)})
    saved = service.create(
        store.deployment.id,
        _request(check_ids=[check.id]),
        expected_revision=1,
        actor="admin",
    )
    assert saved.checks[0].result_summary.state == state


@pytest.mark.parametrize(
    "case",
    ["active", "missing", "foreign", "old-input", "duplicate-kind", "stale-candidate", "archived"],
)
def test_registration_rejects_inconsistent_references_without_writing_a_dossier(case: str) -> None:
    service, store, check, _, audits = setup_service()
    ids = [check.id]
    if case == "active":
        store.checks[check.id] = check.model_copy(update={"state": CheckState.RUNNING})
    elif case == "missing":
        ids = [str(uuid4())]
    elif case == "foreign":
        store.checks[check.id] = check.model_copy(update={"deployment_id": str(uuid4())})
    elif case == "old-input":
        store.checks[check.id] = check.model_copy(
            update={"input": check.input.model_copy(update={"deployment_revision": 2})}
        )
    elif case == "duplicate-kind":
        second = check.model_copy(update={"id": str(uuid4())})
        store.checks[second.id] = second
        ids.append(second.id)
    elif case == "stale-candidate":
        store.deployment = store.deployment.model_copy(update={"revision": 2})
    else:
        store.deployment = store.deployment.model_copy(update={"state": "ARCHIVED"})
    with pytest.raises(CatalogError):
        service.create(
            store.deployment.id,
            _request(check_ids=ids),
            expected_revision=1,
            actor="admin",
        )
    assert not store.dossiers and not audits


def test_material_only_dossier_missing_check_and_no_expiry_keep_honest_gaps() -> None:
    service, store, _, _, _ = setup_service()
    dossier = service.create(
        store.deployment.id,
        _request(
            materials=[
                {
                    "category": "HEALTH",
                    "title": "Declared source",
                    "source_reference": "urn:provider:health",
                }
            ]
        ),
        expected_revision=1,
        actor="admin",
    )
    projection = service.project(dossier, now=NOW)
    assert projection.material_statuses[0].expiration == "NOT_DECLARED"
    assert all(value.status == "NOT_PROVIDED" for value in projection.check_coverage)
    assert {item.category: item.status for item in projection.material_coverage}[
        "HEALTH"
    ] == "DECLARED"
    assert not {"ready", "verified", "active"} & projection.model_dump().keys()
