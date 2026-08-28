import hashlib
from uuid import UUID

import pytest

from control_plane.app.modules.requirement import (
    ArtifactState,
    ArtifactTrust,
    InvalidRequirementInput,
    SddArtifactNotFound,
    StaleBaselineSubject,
    StaleRequirementRevision,
    create_sdd_artifact,
    get_sdd_artifact,
    start_requirement_preparation,
)
from control_plane.app.modules.requirement.adapters import SqlAlchemySddArtifactReader
from control_plane.app.shared.idempotency import IdempotencyConflict
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_commands import Actor, _create, _dependencies


def _prepared(
    database: IsolatedRequirementDatabase,
    *,
    suffix: str,
) -> tuple[str, int]:
    created = _create(database, idempotency_key=f"create-sdd-{suffix}")
    with database.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=created.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"prepare-sdd-{suffix}",
            dependencies=_dependencies(),
        )
    return prepared.id, prepared.revision


def test_create_sdd_artifact_normalizes_bytes_and_allocates_immutable_versions(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_id, revision = _prepared(isolated_requirement_database, suffix="versions")
    with isolated_requirement_database.runtime.begin() as db:
        first = create_sdd_artifact(
            db,
            requirement_id=requirement_id,
            artifact_id=None,
            content="# Plan\r\n\rBody\r",
            expected_revision=revision,
            actor=Actor("employee-1"),
            idempotency_key="artifact-create-v1",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        second = create_sdd_artifact(
            db,
            requirement_id=requirement_id,
            artifact_id=first.artifact.artifact_id,
            content="# Plan v2\n",
            expected_revision=first.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="artifact-create-v2",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.connect() as db:
        exact = get_sdd_artifact(
            db,
            requirement_id=requirement_id,
            artifact_id=first.artifact.artifact_id,
            artifact_version=1,
            dependencies=_dependencies(),
        )
    snapshot = SqlAlchemySddArtifactReader(isolated_requirement_database.runtime).get_snapshot(
        first.artifact.artifact_id, "1"
    )

    assert UUID(first.artifact.artifact_id)
    assert first.artifact.version == 1
    assert first.artifact.content == "# Plan\n\nBody\n"
    assert (
        first.artifact.sha256
        == "sha256:" + hashlib.sha256(first.artifact.content.encode("utf-8")).hexdigest()
    )
    assert first.requirement.revision == revision + 1
    assert second.artifact.artifact_id == first.artifact.artifact_id
    assert second.artifact.version == 2
    assert second.requirement.revision == first.requirement.revision + 1
    assert exact == first.artifact
    assert snapshot.model_dump(mode="json") == {
        "id": first.artifact.artifact_id,
        "version": "1",
        "sha256": first.artifact.sha256,
        "state": ArtifactState.AVAILABLE.value,
        "media_type": "text/markdown; charset=utf-8",
        "trust": ArtifactTrust.TRUSTED_PLAIN_TEXT.value,
    }


def test_create_sdd_artifact_replays_and_rejects_payload_conflict(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_id, revision = _prepared(isolated_requirement_database, suffix="replay")

    def create(content: str) -> object:
        with isolated_requirement_database.runtime.begin() as db:
            return create_sdd_artifact(
                db,
                requirement_id=requirement_id,
                artifact_id=None,
                content=content,
                expected_revision=revision,
                actor=Actor("employee-1"),
                idempotency_key="artifact-replay-key",
                dependencies=_dependencies(),
            )

    first = create("# Stable\n")
    replay = create("# Stable\n")
    with pytest.raises(IdempotencyConflict):
        create("# Different\n")

    assert replay == first
    with isolated_requirement_database.owner.connect() as db:
        count = db.exec_driver_sql(
            "SELECT count(*) FROM requirement.sdd_artifact_version"
        ).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    "content",
    ["", " \r\n\t", "x" * 200_001],
    ids=["empty", "blank", "too-large"],
)
def test_create_sdd_artifact_rejects_invalid_markdown_content(
    isolated_requirement_database: IsolatedRequirementDatabase,
    content: str,
) -> None:
    requirement_id, revision = _prepared(
        isolated_requirement_database,
        suffix=f"invalid-{len(content)}",
    )
    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(InvalidRequirementInput):
            create_sdd_artifact(
                db,
                requirement_id=requirement_id,
                artifact_id=None,
                content=content,
                expected_revision=revision,
                actor=Actor("employee-1"),
                idempotency_key=f"artifact-invalid-{len(content)}",
                dependencies=_dependencies(),
            )


def test_sdd_artifact_identity_never_crosses_requirement_boundary(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    first_requirement, first_revision = _prepared(
        isolated_requirement_database,
        suffix="owner-one",
    )
    second_requirement, second_revision = _prepared(
        isolated_requirement_database,
        suffix="owner-two",
    )
    with isolated_requirement_database.runtime.begin() as db:
        created = create_sdd_artifact(
            db,
            requirement_id=first_requirement,
            artifact_id=None,
            content="# Private to one Requirement\n",
            expected_revision=first_revision,
            actor=Actor("employee-1"),
            idempotency_key="artifact-owner-one",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.connect() as db:
        with pytest.raises(SddArtifactNotFound):
            get_sdd_artifact(
                db,
                requirement_id=second_requirement,
                artifact_id=created.artifact.artifact_id,
                artifact_version=1,
                dependencies=_dependencies(),
            )
    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(SddArtifactNotFound):
            create_sdd_artifact(
                db,
                requirement_id=second_requirement,
                artifact_id=created.artifact.artifact_id,
                content="# Illicit v2\n",
                expected_revision=second_revision,
                actor=Actor("employee-1"),
                idempotency_key="artifact-cross-owner",
                dependencies=_dependencies(),
            )


def test_create_sdd_artifact_requires_preparing_state_and_current_requirement_revision(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        idempotency_key="create-sdd-state-guard",
    )
    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(StaleBaselineSubject):
            create_sdd_artifact(
                db,
                requirement_id=created.requirement.id,
                artifact_id=None,
                content="# Too early\n",
                expected_revision=created.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="artifact-too-early",
                dependencies=_dependencies(),
            )

    requirement_id, revision = _prepared(
        isolated_requirement_database,
        suffix="stale-revision",
    )
    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(StaleRequirementRevision):
            create_sdd_artifact(
                db,
                requirement_id=requirement_id,
                artifact_id=None,
                content="# Stale\n",
                expected_revision=revision - 1,
                actor=Actor("employee-1"),
                idempotency_key="artifact-stale-revision",
                dependencies=_dependencies(),
            )
