from datetime import UTC, datetime

from sqlalchemy import Engine

from control_plane.app.modules.requirement.adapters import SqlAlchemyRequirementRepository

REQUIREMENT_ID = "10000000-0000-0000-0000-000000000101"
WORK_ITEM_ID = "10000000-0000-0000-0000-000000000102"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000101"
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def _insert_requirement(repository: SqlAlchemyRequirementRepository) -> None:
    repository.insert_requirement(
        id=REQUIREMENT_ID,
        workspace_id=WORKSPACE_ID,
        type="feat",
        title="Govern delivery",
        description="Create an auditable delivery workflow.",
        acceptance_criteria=("baseline approved",),
        created_by="employee-1",
        initial_repository_id="repository-1",
        route_snapshot_version=1,
        route_snapshot_hash="sha256:route-1",
        state="CREATED",
        record_state="ACTIVE",
        requirement_version=1,
        required_work_item_set_version=1,
        required_work_item_set_hash="sha256:work-items-1",
        revision=1,
        now=NOW,
    )


def test_repository_persists_requirement_work_item_and_binding_request(
    isolated_requirement_rw_engine: Engine,
) -> None:
    with isolated_requirement_rw_engine.begin() as db:
        repository = SqlAlchemyRequirementRepository(db)
        _insert_requirement(repository)
        repository.insert_work_item(
            id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            created_by="employee-1",
            human_owner_id=None,
            executor_type="HUMAN",
            executor_id=None,
            required_capabilities=("code.change",),
            assignment_state="UNASSIGNED",
            repository_state="WAITING_REPOSITORY",
            state="DRAFT",
            repository_id="repository-1",
            revision=1,
            now=NOW,
        )
        repository.insert_outbox(
            id="10000000-0000-0000-0000-000000000103",
            topic="requirement.repository-binding.requested",
            aggregate_type="REQUIREMENT",
            aggregate_id=REQUIREMENT_ID,
            aggregate_version=1,
            payload={"workItemId": WORK_ITEM_ID, "repositoryId": "repository-1"},
            now=NOW,
        )

    with isolated_requirement_rw_engine.connect() as db:
        repository = SqlAlchemyRequirementRepository(db)
        requirement = repository.requirement_by_id(REQUIREMENT_ID)
        work_items = repository.work_items(REQUIREMENT_ID)
        messages = repository.outbox_by_aggregate(REQUIREMENT_ID, aggregate_version=1)

    assert requirement is not None
    assert (requirement["title"], requirement["state"], requirement["revision"]) == (
        "Govern delivery",
        "CREATED",
        1,
    )
    assert [(str(item["id"]), item["state"], item["repository_state"]) for item in work_items] == [
        (WORK_ITEM_ID, "DRAFT", "WAITING_REPOSITORY")
    ]
    assert [(message["topic"], message["payload"]) for message in messages] == [
        (
            "requirement.repository-binding.requested",
            {"workItemId": WORK_ITEM_ID, "repositoryId": "repository-1"},
        )
    ]


def test_requirement_state_update_is_compare_and_swap(
    isolated_requirement_rw_engine: Engine,
) -> None:
    with isolated_requirement_rw_engine.begin() as db:
        repository = SqlAlchemyRequirementRepository(db)
        _insert_requirement(repository)
        updated = repository.update_requirement_state(
            REQUIREMENT_ID,
            expected_revision=1,
            state="PREPARING",
            now=NOW,
        )
        stale = repository.update_requirement_state(
            REQUIREMENT_ID,
            expected_revision=1,
            state="READY",
            now=NOW,
        )

    assert updated is not None
    assert (updated["state"], updated["revision"]) == ("PREPARING", 2)
    assert stale is None
