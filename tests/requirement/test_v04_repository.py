from datetime import UTC, datetime

from sqlalchemy import Engine

from control_plane.app.modules.requirement.adapters import SqlAlchemyRequirementRepository

NOW = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)
REQUIREMENT_ID = "10000000-0000-0000-0000-000000000401"
WORK_ITEM_ID = "10000000-0000-0000-0000-000000000402"
ARTIFACT_ID = "10000000-0000-0000-0000-000000000403"


def _seed(repository: SqlAlchemyRequirementRepository) -> None:
    repository.insert_requirement(
        id=REQUIREMENT_ID,
        workspace_id="20000000-0000-0000-0000-000000000401",
        type="feat",
        title="Plan the delivery",
        description="Persist immutable planning facts.",
        acceptance_criteria=("facts are immutable",),
        created_by="employee-1",
        initial_repository_id="repository-1",
        route_snapshot_version=2,
        route_snapshot_hash="sha256:route-v2",
        route_snapshot={
            "requirementType": "feat",
            "requiredCapabilities": ["code.change"],
            "steps": ["writing-plans"],
            "version": 2,
        },
        state="PREPARING",
        record_state="ACTIVE",
        requirement_version=1,
        required_work_item_set_version=1,
        required_work_item_set_hash="sha256:work-items-v1",
        revision=1,
        now=NOW,
    )
    repository.insert_work_item(
        id=WORK_ITEM_ID,
        requirement_id=REQUIREMENT_ID,
        created_by="employee-1",
        human_owner_id="employee-1",
        executor_type="HUMAN",
        executor_id="employee-1",
        required_capabilities=("code.change",),
        assignment_state="ASSIGNED",
        repository_state="WAITING_REPOSITORY",
        state="DRAFT",
        repository_id="repository-1",
        revision=1,
        now=NOW,
    )


def test_repository_persists_and_reads_exact_immutable_sdd_artifact_version(
    isolated_requirement_rw_engine: Engine,
) -> None:
    with isolated_requirement_rw_engine.begin() as db:
        repository = SqlAlchemyRequirementRepository(db)
        _seed(repository)
        inserted = repository.insert_sdd_artifact_version(
            artifact_id=ARTIFACT_ID,
            version=1,
            requirement_id=REQUIREMENT_ID,
            sha256="sha256:" + "a" * 64,
            state="AVAILABLE",
            media_type="text/markdown; charset=utf-8",
            trust="TRUSTED_PLAIN_TEXT",
            content="# Plan\n",
            created_by="employee-1",
            now=NOW,
        )
        exact = repository.sdd_artifact_version(
            REQUIREMENT_ID,
            ARTIFACT_ID,
            1,
        )
        latest = repository.latest_sdd_artifact_version(
            REQUIREMENT_ID,
            ARTIFACT_ID,
        )

    assert inserted == exact == latest
    assert inserted["content"] == "# Plan\n"
    assert inserted["version"] == 1


def test_repository_supersedes_one_assignment_and_appends_the_next_revision(
    isolated_requirement_rw_engine: Engine,
) -> None:
    first_id = "10000000-0000-0000-0000-000000000404"
    second_id = "10000000-0000-0000-0000-000000000405"
    with isolated_requirement_rw_engine.begin() as db:
        repository = SqlAlchemyRequirementRepository(db)
        _seed(repository)
        first = repository.insert_work_item_assignment(
            id=first_id,
            work_item_id=WORK_ITEM_ID,
            assignee_id="employee-1",
            assigned_by="employee-1",
            reason="INITIAL_ASSIGNMENT",
            revision=1,
            now=NOW,
        )
        superseded = repository.supersede_work_item_assignment(
            first_id,
            expected_revision=1,
            now=NOW,
        )
        second = repository.insert_work_item_assignment(
            id=second_id,
            work_item_id=WORK_ITEM_ID,
            assignee_id="employee-2",
            assigned_by="employee-1",
            reason="BALANCE_LOAD",
            revision=2,
            now=NOW,
        )
        current = repository.current_work_item_assignment(
            WORK_ITEM_ID,
            for_update=True,
        )

    assert first["superseded_at"] is None
    assert superseded is not None
    assert superseded["superseded_at"] == NOW
    assert current == second
    assert (current["assignee_id"], current["revision"]) == ("employee-2", 2)
