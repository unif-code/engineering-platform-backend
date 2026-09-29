from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.source_control import (
    ArtifactReference,
    EvidenceMessageConflict,
    EvidenceStale,
    ExternalValidationRequestEnvelope,
    IntegrationBaselineRequestEnvelope,
    SourceControlDependencies,
    accept_external_validation,
    accept_integration_baseline_request,
    get_integration_baseline_evidence,
    process_integration_baseline_request,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlEvidenceRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.ports import RequirementEvidencePort
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_commands import FixedRandom
from tests.source_control.test_migration import _insert_integration_graph

NOW = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000301"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000301"
REPOSITORY_ID = "10000000-0000-0000-0000-000000000301"
BINDING_ID = "71000000-0000-0000-0000-000000000301"
HEAD_SHA = "b" * 40
MERGE_SHA = "c" * 40
SNAPSHOT_ID = "92000000-0000-0000-0000-000000000601"
SNAPSHOT_HASH = "sha256:" + "e" * 64
SET_HASH = "sha256:" + "f" * 64


class FixedClock:
    def now(self) -> datetime:
        return NOW


def _dependencies(source: IsolatedSourceControlDatabase) -> SourceControlDependencies:
    return SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=source.runtime,
        requirement=None,
        eligibility=None,
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=FixedClock(),
        random=FixedRandom(),
        evidence_repository_factory=SqlAlchemySourceControlEvidenceRepository,
        requirement_evidence=Mock(spec=RequirementEvidencePort),
    )


def _seed_merged_integration(source: IsolatedSourceControlDatabase) -> None:
    with source.owner.begin() as db:
        _insert_integration_graph(db)
        db.execute(
            text(
                "UPDATE source_control.merge_request_observation "
                "SET observed_at=:observed_at "
                "WHERE id='80000000-0000-0000-0000-000000000301'"
            ),
            {"observed_at": NOW - timedelta(seconds=1)},
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_observation "
                "(id, binding_id, head_sha, state, merge_commit_sha, "
                "external_merge_user_id, merged_at, observation_digest, observed_at) "
                "VALUES ('80000000-0000-0000-0000-000000000302', :binding_id, "
                ":head_sha, 'MERGED', :merge_sha, '42', :now, 'sha256:merged', :now)"
            ),
            {
                "binding_id": BINDING_ID,
                "head_sha": HEAD_SHA,
                "merge_sha": MERGE_SHA,
                "now": NOW,
            },
        )


def _validation(
    *,
    message_id: str,
    target_commit_sha: str = HEAD_SHA,
) -> ExternalValidationRequestEnvelope:
    return ExternalValidationRequestEnvelope(
        message_id=message_id,
        payload_hash="sha256:" + "1" * 64,
        requirement_id=REQUIREMENT_ID,
        requirement_version=7,
        work_item_id=WORK_ITEM_ID,
        work_item_revision=9,
        repository_id=REPOSITORY_ID,
        integration_merge_request_binding_id=BINDING_ID,
        target_commit_sha=target_commit_sha,
        integration_merge_commit_sha=MERGE_SHA,
        reference="https://jenkins.example.test/job/platform/42?token=discard#console",
        notes="Manually verified the exact merged commit.",
        artifact_references=(
            ArtifactReference(
                artifact_id="30000000-0000-0000-0000-000000000601",
                artifact_version="2",
                artifact_hash="sha256:" + "d" * 64,
            ),
        ),
        submitted_by="employee-1",
        submitted_at=NOW,
        attempts=1,
    )


def _request(
    *,
    message_id: str,
    payload_hash: str = "sha256:" + "2" * 64,
) -> IntegrationBaselineRequestEnvelope:
    return IntegrationBaselineRequestEnvelope(
        message_id=message_id,
        payload_hash=payload_hash,
        delivery_snapshot_id=SNAPSHOT_ID,
        delivery_snapshot_hash=SNAPSHOT_HASH,
        requirement_id=REQUIREMENT_ID,
        requirement_version=7,
        required_work_item_set_version=3,
        required_work_item_set_hash=SET_HASH,
        work_item_ids=(WORK_ITEM_ID,),
        requested_by="employee-1",
        attempts=1,
    )


def test_external_validation_and_snapshot_generate_exact_immutable_evidence(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    dependencies = _dependencies(isolated_source_control_database)
    validation = _validation(message_id="93000000-0000-0000-0000-000000000601")
    request = _request(message_id="94000000-0000-0000-0000-000000000601")

    with isolated_source_control_database.runtime.begin() as db:
        accepted = accept_external_validation(db, validation, dependencies=dependencies)
    with isolated_source_control_database.runtime.begin() as db:
        replayed = accept_external_validation(
            db,
            validation.model_copy(update={"attempts": 2}),
            dependencies=dependencies,
        )
    with isolated_source_control_database.runtime.begin() as db:
        accepted_request = accept_integration_baseline_request(
            db,
            request,
            dependencies=dependencies,
        )
    with isolated_source_control_database.runtime.begin() as db:
        evidence = process_integration_baseline_request(
            db,
            message_id=request.message_id,
            generated_by="SYSTEM:SOURCE_CONTROL",
            dependencies=dependencies,
        )
    with isolated_source_control_database.runtime.connect() as db:
        read_back = get_integration_baseline_evidence(
            db,
            evidence_id=evidence.id,
            dependencies=dependencies,
        )

    assert replayed == accepted
    assert accepted.reference == "https://jenkins.example.test/job/platform/42"
    assert accepted_request is True
    assert read_back == evidence
    assert evidence.delivery_snapshot_id == SNAPSHOT_ID
    assert evidence.delivery_snapshot_hash == SNAPSHOT_HASH
    assert evidence.work_item_ids == (WORK_ITEM_ID,)
    assert evidence.items[0].task_commit_sha == HEAD_SHA
    assert evidence.items[0].integration_merge_commit_sha == MERGE_SHA
    assert evidence.items[0].external_validation.id == validation.message_id
    assert evidence.evidence_hash.startswith("sha256:")

    with isolated_source_control_database.owner.connect() as db:
        counts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM source_control.external_validation_reference), "
                "(SELECT count(*) FROM source_control.evidence_request_inbox), "
                "(SELECT count(*) FROM source_control.integration_baseline_evidence), "
                "(SELECT count(*) FROM source_control.integration_baseline_evidence_item)"
            )
        ).one()
        actions = tuple(
            db.execute(
                text(
                    "SELECT action FROM audit.audit_event "
                    "WHERE action LIKE 'source_control.evidence.%' ORDER BY action"
                )
            ).scalars()
        )
    assert counts == (1, 1, 1, 1)
    assert actions == (
        "source_control.evidence.external_validation_accepted",
        "source_control.evidence.generated",
        "source_control.evidence.request_accepted",
    )


def test_generated_evidence_is_stale_after_the_latest_observation_head_changes(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    dependencies = _dependencies(isolated_source_control_database)
    validation = _validation(message_id="93000000-0000-0000-0000-000000000611")
    request = _request(message_id="94000000-0000-0000-0000-000000000611")
    with isolated_source_control_database.runtime.begin() as db:
        accept_external_validation(db, validation, dependencies=dependencies)
        accept_integration_baseline_request(db, request, dependencies=dependencies)
        evidence = process_integration_baseline_request(
            db,
            message_id=request.message_id,
            generated_by="SYSTEM:SOURCE_CONTROL",
            dependencies=dependencies,
        )

    changed_head_sha = "d" * 40
    with isolated_source_control_database.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_observation "
                "(id, binding_id, head_sha, state, merge_commit_sha, "
                "external_merge_user_id, merged_at, observation_digest, observed_at) "
                "VALUES ('80000000-0000-0000-0000-000000000311', :binding_id, "
                ":head_sha, 'MERGED', :merge_sha, '42', :observed_at, "
                "'sha256:changed-head', :observed_at)"
            ),
            {
                "binding_id": BINDING_ID,
                "head_sha": changed_head_sha,
                "merge_sha": MERGE_SHA,
                "observed_at": NOW + timedelta(seconds=1),
            },
        )
    with isolated_source_control_database.runtime.connect() as db:
        stale = get_integration_baseline_evidence(
            db,
            evidence_id=evidence.id,
            dependencies=dependencies,
        )

    assert stale.currentness.current is False
    assert stale.currentness.state == "STALE"
    assert stale.currentness.items[0].binding_id == BINDING_ID
    assert stale.currentness.items[0].latest_observation_head_sha == changed_head_sha
    assert "OBSERVATION_HEAD_CHANGED" in stale.currentness.items[0].reasons


def test_generated_evidence_is_stale_after_a_newer_exact_validation(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    dependencies = _dependencies(isolated_source_control_database)
    original = _validation(message_id="93000000-0000-0000-0000-000000000631")
    request = _request(message_id="94000000-0000-0000-0000-000000000631")
    with isolated_source_control_database.runtime.begin() as db:
        accept_external_validation(db, original, dependencies=dependencies)
        accept_integration_baseline_request(db, request, dependencies=dependencies)
        evidence = process_integration_baseline_request(
            db,
            message_id=request.message_id,
            generated_by="SYSTEM:SOURCE_CONTROL",
            dependencies=dependencies,
        )

    newer = original.model_copy(
        update={
            "message_id": "93000000-0000-0000-0000-000000000632",
            "payload_hash": "sha256:" + "3" * 64,
            "reference": "urn:jenkins:platform:43",
            "notes": "A newer validation supersedes the Evidence input.",
        }
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_external_validation(db, newer, dependencies=dependencies)
    with isolated_source_control_database.runtime.connect() as db:
        stale = get_integration_baseline_evidence(
            db,
            evidence_id=evidence.id,
            dependencies=dependencies,
        )

    assert stale.currentness.current is False
    assert stale.currentness.state == "STALE"
    assert stale.currentness.items[0].latest_external_validation_reference_id == newer.message_id
    assert "VALIDATION_CHANGED" in stale.currentness.items[0].reasons


def test_external_validation_rejects_a_commit_not_proven_by_merged_observation(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    with pytest.raises(EvidenceStale, match="target commit"):
        with isolated_source_control_database.runtime.begin() as db:
            accept_external_validation(
                db,
                _validation(
                    message_id="93000000-0000-0000-0000-000000000602",
                    target_commit_sha="9" * 40,
                ),
                dependencies=_dependencies(isolated_source_control_database),
            )


def test_external_validation_uses_only_the_current_integration_binding(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    current_binding_id = "71000000-0000-0000-0000-000000000312"
    current_effect_id = "60000000-0000-0000-0000-000000000312"
    current_observation_id = "80000000-0000-0000-0000-000000000312"
    current_head_sha = "d" * 40
    current_merge_sha = "e" * 40
    with isolated_source_control_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE source_control.merge_request_binding "
                "SET superseded_at=clock_timestamp() WHERE id=:binding_id"
            ),
            {"binding_id": BINDING_ID},
        )
        db.execute(
            text(
                "INSERT INTO source_control.source_control_effect "
                "(id, effect_key, operation, subject_key, payload, work_item_id, "
                "requirement_id, repository_id, request_fingerprint, attempts, state, "
                "requirement_callback_state, completed_at) VALUES "
                "(:effect_id, :effect_key, 'CREATE_INTEGRATION_MR', :subject_key, "
                "jsonb_build_object('branchBindingId', "
                "'70000000-0000-0000-0000-000000000301', "
                "'headSha', CAST(:head_sha AS TEXT)), "
                ":work_item_id, :requirement_id, :repository_id, "
                "'sha256:current-integration-binding', 1, 'SUCCEEDED', 'PENDING', now())"
            ),
            {
                "effect_id": current_effect_id,
                "effect_key": (
                    f"source-control:create-integration-mr:{WORK_ITEM_ID}:{current_head_sha}"
                ),
                "subject_key": f"integration-work-item:{WORK_ITEM_ID}:{current_head_sha}",
                "head_sha": current_head_sha,
                "work_item_id": WORK_ITEM_ID,
                "requirement_id": REQUIREMENT_ID,
                "repository_id": REPOSITORY_ID,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_binding "
                "(id, kind, work_item_id, requirement_id, workspace_id, repository_id, "
                "branch_binding_id, external_project_id, merge_request_iid, "
                "source_branch, target_branch, create_effect_id, head_sha, "
                "creation_origin) VALUES "
                "(:binding_id, 'INTEGRATION', :work_item_id, :requirement_id, "
                "'20000000-0000-0000-0000-000000000301', :repository_id, "
                "'70000000-0000-0000-0000-000000000301', '101', 43, "
                "'feat/wi-301-source-control', 'dev', :effect_id, :head_sha, "
                "'PLATFORM_CREATED')"
            ),
            {
                "binding_id": current_binding_id,
                "work_item_id": WORK_ITEM_ID,
                "requirement_id": REQUIREMENT_ID,
                "repository_id": REPOSITORY_ID,
                "effect_id": current_effect_id,
                "head_sha": current_head_sha,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_observation "
                "(id, binding_id, head_sha, state, merge_commit_sha, "
                "external_merge_user_id, merged_at, observation_digest, observed_at) "
                "VALUES (:observation_id, :binding_id, :head_sha, 'MERGED', :merge_sha, "
                "'42', :now, 'sha256:current-merged', :now)"
            ),
            {
                "observation_id": current_observation_id,
                "binding_id": current_binding_id,
                "head_sha": current_head_sha,
                "merge_sha": current_merge_sha,
                "now": NOW + timedelta(seconds=1),
            },
        )

    dependencies = _dependencies(isolated_source_control_database)
    with pytest.raises(EvidenceStale, match="does not match the WorkItem"):
        with isolated_source_control_database.runtime.begin() as db:
            accept_external_validation(
                db,
                _validation(message_id="93000000-0000-0000-0000-000000000641"),
                dependencies=dependencies,
            )

    current_validation = _validation(message_id="93000000-0000-0000-0000-000000000642").model_copy(
        update={
            "payload_hash": "sha256:" + "4" * 64,
            "integration_merge_request_binding_id": current_binding_id,
            "target_commit_sha": current_head_sha,
            "integration_merge_commit_sha": current_merge_sha,
        }
    )
    with isolated_source_control_database.runtime.begin() as db:
        accepted = accept_external_validation(
            db,
            current_validation,
            dependencies=dependencies,
        )

    assert accepted.integration_merge_request_binding_id == current_binding_id
    assert accepted.target_commit_sha == current_head_sha


def test_same_external_validation_content_with_a_new_message_id_reuses_the_fact(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    dependencies = _dependencies(isolated_source_control_database)
    original = _validation(message_id="93000000-0000-0000-0000-000000000621")
    retried = original.model_copy(
        update={
            "message_id": "93000000-0000-0000-0000-000000000622",
            "payload_hash": "sha256:" + "2" * 64,
        }
    )

    with isolated_source_control_database.runtime.begin() as db:
        accepted = accept_external_validation(db, original, dependencies=dependencies)
    with isolated_source_control_database.runtime.begin() as db:
        replayed = accept_external_validation(db, retried, dependencies=dependencies)

    assert replayed == accepted
    with isolated_source_control_database.owner.connect() as db:
        count = db.execute(
            text("SELECT count(*) FROM source_control.external_validation_reference")
        ).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("payload_hash", "sha256:" + "5" * 64, id="payload-hash"),
        pytest.param("requirement_version", 8, id="requirement-version"),
        pytest.param("work_item_revision", 10, id="work-item-revision"),
        pytest.param(
            "repository_id",
            "10000000-0000-0000-0000-000000000399",
            id="repository-id",
        ),
        pytest.param(
            "submitted_at",
            NOW + timedelta(seconds=1),
            id="submitted-at",
        ),
    ],
)
def test_same_external_validation_message_with_changed_envelope_is_a_conflict(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    field: str,
    value: object,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    dependencies = _dependencies(isolated_source_control_database)
    original = _validation(message_id="93000000-0000-0000-0000-000000000651")
    changed = original.model_copy(update={field: value})

    with isolated_source_control_database.runtime.begin() as db:
        accept_external_validation(db, original, dependencies=dependencies)
    with pytest.raises(EvidenceMessageConflict):
        with isolated_source_control_database.runtime.begin() as db:
            accept_external_validation(db, changed, dependencies=dependencies)


def test_same_evidence_request_message_with_changed_payload_is_a_conflict(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    dependencies = _dependencies(isolated_source_control_database)
    original = _request(message_id="94000000-0000-0000-0000-000000000603")
    changed = _request(
        message_id=original.message_id,
        payload_hash="sha256:" + "3" * 64,
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_integration_baseline_request(db, original, dependencies=dependencies)
    with pytest.raises(EvidenceMessageConflict):
        with isolated_source_control_database.runtime.begin() as db:
            accept_integration_baseline_request(db, changed, dependencies=dependencies)
