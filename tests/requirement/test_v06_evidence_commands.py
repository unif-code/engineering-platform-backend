from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import TypedDict

import pytest
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    ArtifactEvidenceReference,
    DeliverySnapshotConflict,
    EvidenceUnavailableOrStale,
    RequirementDependencies,
    RequirementDependencyUnavailable,
    RequirementState,
    StaleRequirementRevision,
    request_integration_baseline,
    submit_external_validation,
)
from control_plane.app.modules.requirement.adapters import SqlAlchemyRequirementRepository
from control_plane.app.shared.idempotency import IdempotencyConflict
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_baseline_gate import ARTIFACT_HASH_V1, _gate_dependencies
from tests.requirement.test_commands import Actor
from tests.requirement.test_delivery_commands import (
    _merge_ready_work_item,
)


class _ExternalValidationCommon(TypedDict):
    requirement_id: str
    work_item_id: str
    integration_merge_commit_sha: str
    reference: str
    notes: str
    artifact_references: tuple[ArtifactEvidenceReference, ...]
    expected_revision: int
    actor: Actor
    idempotency_key: str
    dependencies: RequirementDependencies


class _ExternalValidationCommand(_ExternalValidationCommon):
    target_commit_sha: str


def _integrated_requirement(
    database: IsolatedRequirementDatabase,
    *,
    key_suffix: str,
) -> tuple[str, str, int, int]:
    current, _binding_id = _merge_ready_work_item(database, key_suffix=key_suffix)
    requirement_id = current.requirement.id
    work_item_id = current.work_items[0].id
    with database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.work_item SET integration_delivery_state='INTEGRATED', "
                "state='VERIFYING' WHERE id=:work_item_id"
            ),
            {"work_item_id": work_item_id},
        )
    return (
        requirement_id,
        work_item_id,
        current.requirement.revision,
        current.requirement.requirement_version,
    )


def test_freeze_rejects_tampered_current_set_without_snapshot_or_outbox(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    database = isolated_requirement_database
    requirement_id, _, revision, version = _integrated_requirement(database, key_suffix="bad-set")
    with database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.requirement SET required_work_item_set_hash=:hash WHERE id=:id"
            ),
            {"hash": "sha256:" + "0" * 64, "id": requirement_id},
        )
    with pytest.raises(RequirementDependencyUnavailable):
        with database.runtime.begin() as db:
            request_integration_baseline(
                db,
                requirement_id=requirement_id,
                expected_revision=revision,
                expected_requirement_version=version,
                actor=Actor("employee-1"),
                idempotency_key="bad-set-freeze",
                dependencies=_gate_dependencies(),
            )
    with database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM requirement.requirement_delivery_snapshot "
                    "WHERE requirement_id=:id"
                ),
                {"id": requirement_id},
            ).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM requirement.outbox_message WHERE aggregate_id=:id "
                    "AND topic='requirement.integration-baseline.requested'"
                ),
                {"id": requirement_id},
            ).scalar_one()
            == 0
        )


def test_submit_external_validation_is_versioned_idempotent_and_sanitized(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_id, work_item_id, revision, requirement_version = _integrated_requirement(
        isolated_requirement_database,
        key_suffix="v06-external-validation",
    )
    dependencies = _gate_dependencies()
    command: _ExternalValidationCommand = {
        "requirement_id": requirement_id,
        "work_item_id": work_item_id,
        "target_commit_sha": "b" * 40,
        "integration_merge_commit_sha": "c" * 40,
        "reference": "urn:jenkins:platform:42?token=must-not-persist#console",
        "notes": "Manually verified this exact integration commit.",
        "artifact_references": (
            ArtifactEvidenceReference(
                artifact_id="sdd-1",
                artifact_version="version-1",
                artifact_hash=ARTIFACT_HASH_V1,
            ),
        ),
        "expected_revision": revision,
        "actor": Actor("employee-1"),
        "idempotency_key": "v06-external-validation",
        "dependencies": dependencies,
    }
    with isolated_requirement_database.runtime.begin() as db:
        first = submit_external_validation(db, **command)
    command["dependencies"] = replace(dependencies, artifacts=None)
    with isolated_requirement_database.runtime.begin() as db:
        replay = submit_external_validation(db, **command)

    assert replay == first
    assert first.requirement.state is RequirementState.VERIFYING
    assert first.requirement.requirement_version == requirement_version + 1
    assert first.requirement.revision == revision + 1
    assert first.submission.reference == "urn:jenkins:platform:42"
    assert first.outbox_topic == "requirement.external-validation.submitted"
    with isolated_requirement_database.owner.connect() as db:
        row = (
            db.execute(
                text(
                    "SELECT payload FROM requirement.outbox_message "
                    "WHERE aggregate_id=:requirement_id "
                    "AND topic='requirement.external-validation.submitted'"
                ),
                {"requirement_id": requirement_id},
            )
            .mappings()
            .one()
        )
        audit_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE target_id=:work_item_id "
                "AND action='requirement.external_validation.submitted'"
            ),
            {"work_item_id": work_item_id},
        ).scalar_one()
        idempotent_status = db.execute(
            text(
                "SELECT http_status FROM requirement.idempotency_record "
                "WHERE idempotency_key='v06-external-validation'"
            )
        ).scalar_one()

    assert row["payload"]["reference"] == "urn:jenkins:platform:42"
    assert "must-not-persist" not in str(row["payload"])
    assert "token" not in str(row["payload"])
    assert audit_count == 1
    assert idempotent_status == 200


def test_submit_external_validation_rejects_same_key_with_changed_commit(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_id, work_item_id, revision, _version = _integrated_requirement(
        isolated_requirement_database,
        key_suffix="v06-external-conflict",
    )
    dependencies = _gate_dependencies()
    common: _ExternalValidationCommon = {
        "requirement_id": requirement_id,
        "work_item_id": work_item_id,
        "integration_merge_commit_sha": "c" * 40,
        "reference": "urn:jenkins:platform:42",
        "notes": "Manual validation.",
        "artifact_references": (
            ArtifactEvidenceReference(
                artifact_id="sdd-1",
                artifact_version="version-1",
                artifact_hash=ARTIFACT_HASH_V1,
            ),
        ),
        "expected_revision": revision,
        "actor": Actor("employee-1"),
        "idempotency_key": "v06-external-conflict",
        "dependencies": dependencies,
    }
    with isolated_requirement_database.runtime.begin() as db:
        submit_external_validation(db, target_commit_sha="b" * 40, **common)
    with pytest.raises(IdempotencyConflict):
        with isolated_requirement_database.runtime.begin() as db:
            submit_external_validation(db, target_commit_sha="d" * 40, **common)


def test_submit_external_validation_classifies_unavailable_artifacts(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_id, work_item_id, revision, _version = _integrated_requirement(
        isolated_requirement_database,
        key_suffix="v06-external-artifacts-unavailable",
    )
    dependencies = replace(_gate_dependencies(), artifacts=None)

    with pytest.raises(EvidenceUnavailableOrStale, match="Artifact reader"):
        with isolated_requirement_database.runtime.begin() as db:
            submit_external_validation(
                db,
                requirement_id=requirement_id,
                work_item_id=work_item_id,
                target_commit_sha="b" * 40,
                integration_merge_commit_sha="c" * 40,
                reference="urn:jenkins:platform:unavailable",
                notes="Manual validation.",
                artifact_references=(
                    ArtifactEvidenceReference(
                        artifact_id="sdd-1",
                        artifact_version="version-1",
                        artifact_hash=ARTIFACT_HASH_V1,
                    ),
                ),
                expected_revision=revision,
                actor=Actor("employee-1"),
                idempotency_key="v06-external-artifacts-unavailable",
                dependencies=dependencies,
            )


def test_request_integration_baseline_freezes_exact_set_and_outbox(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_id, work_item_id, revision, requirement_version = _integrated_requirement(
        isolated_requirement_database,
        key_suffix="v06-snapshot",
    )
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        result = request_integration_baseline(
            db,
            requirement_id=requirement_id,
            expected_revision=revision,
            expected_requirement_version=requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-snapshot",
            dependencies=dependencies,
        )

    assert result.snapshot.requirement_id == requirement_id
    assert result.snapshot.requirement_version == requirement_version
    assert result.snapshot.work_item_ids == (work_item_id,)
    assert result.requirement.revision == revision + 1
    assert result.requirement.requirement_version == requirement_version
    assert result.outbox_topic == "requirement.integration-baseline.requested"
    with isolated_requirement_database.owner.connect() as db:
        snapshot_count, outbox_count = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM requirement.requirement_delivery_snapshot "
                " WHERE requirement_id=:requirement_id), "
                "(SELECT count(*) FROM requirement.outbox_message "
                " WHERE aggregate_id=:requirement_id "
                " AND topic='requirement.integration-baseline.requested')"
            ),
            {"requirement_id": requirement_id},
        ).one()
    assert (snapshot_count, outbox_count) == (1, 1)


def test_request_integration_baseline_rejects_duplicate_semantic_snapshot(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_id, _work_item_id, revision, requirement_version = _integrated_requirement(
        isolated_requirement_database,
        key_suffix="v06-snapshot-duplicate",
    )
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        first = request_integration_baseline(
            db,
            requirement_id=requirement_id,
            expected_revision=revision,
            expected_requirement_version=requirement_version,
            actor=Actor("employee-1"),
            idempotency_key="v06-snapshot-duplicate-first",
            dependencies=dependencies,
        )

    with pytest.raises(DeliverySnapshotConflict, match="already exists"):
        with isolated_requirement_database.runtime.begin() as db:
            request_integration_baseline(
                db,
                requirement_id=requirement_id,
                expected_revision=first.requirement.revision,
                expected_requirement_version=requirement_version,
                actor=Actor("employee-1"),
                idempotency_key="v06-snapshot-duplicate-second",
                dependencies=dependencies,
            )

    with isolated_requirement_database.owner.connect() as db:
        snapshot_count, outbox_count = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM requirement.requirement_delivery_snapshot "
                " WHERE requirement_id=:requirement_id), "
                "(SELECT count(*) FROM requirement.outbox_message "
                " WHERE aggregate_id=:requirement_id "
                " AND topic='requirement.integration-baseline.requested')"
            ),
            {"requirement_id": requirement_id},
        ).one()
    assert (snapshot_count, outbox_count) == (1, 1)


def test_concurrent_duplicate_baseline_requests_return_one_domain_conflict(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_id, _work_item_id, revision, requirement_version = _integrated_requirement(
        isolated_requirement_database,
        key_suffix="v06-snapshot-concurrent",
    )
    dependencies = _gate_dependencies()
    ready = Barrier(2)

    def request(key: str) -> str:
        ready.wait(timeout=5)
        try:
            with isolated_requirement_database.runtime.begin() as db:
                request_integration_baseline(
                    db,
                    requirement_id=requirement_id,
                    expected_revision=revision,
                    expected_requirement_version=requirement_version,
                    actor=Actor("employee-1"),
                    idempotency_key=key,
                    dependencies=dependencies,
                )
        except (DeliverySnapshotConflict, StaleRequirementRevision) as error:
            return type(error).__name__
        return "CREATED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(
            pool.map(
                request,
                ("v06-snapshot-concurrent-a", "v06-snapshot-concurrent-b"),
            )
        )

    assert outcomes.count("CREATED") == 1
    assert (
        sum(
            outcome in {"DeliverySnapshotConflict", "StaleRequirementRevision"}
            for outcome in outcomes
        )
        == 1
    )
    with isolated_requirement_database.owner.connect() as db:
        snapshot_count = db.execute(
            text(
                "SELECT count(*) FROM requirement.requirement_delivery_snapshot "
                "WHERE requirement_id=:requirement_id"
            ),
            {"requirement_id": requirement_id},
        ).scalar_one()
    assert snapshot_count == 1


def test_request_integration_baseline_rejects_non_integrated_required_item(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    current, _binding_id = _merge_ready_work_item(
        isolated_requirement_database,
        key_suffix="v06-snapshot-not-integrated",
    )
    with pytest.raises(DeliverySnapshotConflict, match="INTEGRATED"):
        with isolated_requirement_database.runtime.begin() as db:
            request_integration_baseline(
                db,
                requirement_id=current.requirement.id,
                expected_revision=current.requirement.revision,
                expected_requirement_version=current.requirement.requirement_version,
                actor=Actor("employee-1"),
                idempotency_key="v06-snapshot-not-integrated",
                dependencies=_gate_dependencies(),
            )
    with isolated_requirement_database.owner.connect() as db:
        count = db.execute(
            text(
                "SELECT count(*) FROM requirement.requirement_delivery_snapshot "
                "WHERE requirement_id=:requirement_id"
            ),
            {"requirement_id": current.requirement.id},
        ).scalar_one()
    assert count == 0


def test_evidence_commands_use_requirement_repository_port_only() -> None:
    repository = SqlAlchemyRequirementRepository
    assert hasattr(repository, "insert_delivery_snapshot")
    assert hasattr(repository, "advance_evidence_input")
