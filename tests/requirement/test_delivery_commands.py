import pytest
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    DecisionOutcome,
    RequirementDetailsDto,
    RequirementError,
    RequirementState,
    StaleRequirementRevision,
    WorkItemActorDenied,
    WorkItemDeliveryResult,
    WorkItemNotFound,
    WorkItemState,
    decide_baseline,
    get_requirement,
    record_repository_binding,
    register_sdd_baseline,
    request_integration_merge,
    request_integration_merge_request,
    start_requirement_preparation,
    start_work_item,
    submit_baseline_confirmation,
)
from control_plane.app.modules.requirement.adapters import SqlAlchemyRequirementRepository
from control_plane.app.modules.requirement.domain import IntegrationDeliveryState
from control_plane.app.shared.idempotency import IdempotencyConflict
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_baseline_gate import _gate_dependencies
from tests.requirement.test_commands import Actor, _create


def _ready_requirement(
    database: IsolatedRequirementDatabase,
    *,
    auto_assign: bool = True,
    bind_repository: bool = True,
    key_suffix: str = "default",
) -> RequirementDetailsDto:
    dependencies = _gate_dependencies()
    created = _create(
        database,
        auto_assign=auto_assign,
        idempotency_key=f"delivery-create-{key_suffix}",
    )
    with database.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=created.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"delivery-prepare-{key_suffix}",
            dependencies=dependencies,
        )
    with database.runtime.begin() as db:
        registered = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"delivery-register-{key_suffix}",
            dependencies=dependencies,
        )
    with database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=registered.baseline.id,
            expected_revision=registered.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"delivery-confirm-{key_suffix}",
            dependencies=dependencies,
        )
    with database.runtime.begin() as db:
        decide_baseline(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="The SDD is executable.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("reviewer-1"),
            idempotency_key=f"delivery-decide-{key_suffix}",
            dependencies=dependencies,
        )
    if bind_repository:
        with database.runtime.begin() as db:
            record_repository_binding(
                db,
                work_item_id=created.work_item.id,
                repository_id=created.work_item.repository_id,
                base_commit_sha="a" * 40,
                task_branch=f"task/{created.work_item.id}",
                expected_revision=created.work_item.revision,
                actor=Actor("source-control"),
                idempotency_key=f"delivery-bind-{key_suffix}",
                dependencies=dependencies,
            )
    with database.runtime.connect() as db:
        return get_requirement(
            db,
            requirement_id=created.requirement.id,
            dependencies=dependencies,
        )


def _started_work_item(
    database: IsolatedRequirementDatabase,
    *,
    key_suffix: str = "default",
) -> WorkItemDeliveryResult:
    dependencies = _gate_dependencies()
    ready = _ready_requirement(database, key_suffix=key_suffix)
    with database.runtime.begin() as db:
        return start_work_item(
            db,
            requirement_id=ready.requirement.id,
            work_item_id=ready.work_items[0].id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"delivery-start-{key_suffix}",
            dependencies=dependencies,
        )


def test_repository_reads_only_the_requirement_required_work_item_states(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    ready = _ready_requirement(isolated_requirement_database, key_suffix="required-states")
    _ready_requirement(isolated_requirement_database, key_suffix="other-required-states")

    with isolated_requirement_database.runtime.connect() as db:
        states = SqlAlchemyRequirementRepository(db).required_work_item_states(ready.requirement.id)

    assert states == (WorkItemState.READY.value,)


def test_current_owner_starts_ready_bound_work_item_atomically(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    ready = _ready_requirement(isolated_requirement_database)

    with isolated_requirement_database.runtime.begin() as db:
        result = start_work_item(
            db,
            requirement_id=ready.requirement.id,
            work_item_id=ready.work_items[0].id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="start-human-work-1",
            dependencies=_gate_dependencies(),
        )

    assert result.requirement.state is RequirementState.IN_PROGRESS
    assert result.requirement.revision == ready.requirement.revision + 1
    assert result.work_item.state is WorkItemState.IN_PROGRESS
    assert result.work_item.integration_delivery_state is IntegrationDeliveryState.IMPLEMENTING
    assert result.work_item.revision == ready.work_items[0].revision + 1
    assert result.outbox_topic is None
    assert not ({"base_commit_sha", "task_branch"} & set(result.work_item.model_dump()))


@pytest.mark.parametrize(
    ("actor", "auto_assign", "bind_repository"),
    [
        (Actor("employee-2"), True, True),
        (Actor("employee-1"), False, True),
        (Actor("employee-1"), True, False),
    ],
)
def test_start_fails_closed_for_owner_assignment_or_repository_mismatch(
    isolated_requirement_database: IsolatedRequirementDatabase,
    actor: Actor,
    auto_assign: bool,
    bind_repository: bool,
) -> None:
    ready = _ready_requirement(
        isolated_requirement_database,
        auto_assign=auto_assign,
        bind_repository=bind_repository,
    )

    with pytest.raises(RequirementError):
        with isolated_requirement_database.runtime.begin() as db:
            start_work_item(
                db,
                requirement_id=ready.requirement.id,
                work_item_id=ready.work_items[0].id,
                expected_revision=ready.requirement.revision,
                actor=actor,
                idempotency_key="start-human-work-denied",
                dependencies=_gate_dependencies(),
            )

    with isolated_requirement_database.owner.connect() as db:
        states = db.execute(
            text(
                "SELECT requirement.state, requirement.revision, work_item.state, "
                "work_item.revision FROM requirement.requirement "
                "JOIN requirement.work_item ON work_item.requirement_id=requirement.id "
                "WHERE requirement.id=:requirement_id"
            ),
            {"requirement_id": ready.requirement.id},
        ).one()
    assert states == (
        "READY",
        ready.requirement.revision,
        ready.work_items[0].state.value,
        ready.work_items[0].revision,
    )


def test_start_rejects_stale_requirement_revision(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    ready = _ready_requirement(isolated_requirement_database)

    with pytest.raises(StaleRequirementRevision):
        with isolated_requirement_database.runtime.begin() as db:
            start_work_item(
                db,
                requirement_id=ready.requirement.id,
                work_item_id=ready.work_items[0].id,
                expected_revision=ready.requirement.revision - 1,
                actor=Actor("employee-1"),
                idempotency_key="start-human-work-stale",
                dependencies=_gate_dependencies(),
            )


def test_start_rejects_work_item_from_another_requirement_without_leaking_it(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    first = _ready_requirement(isolated_requirement_database, key_suffix="first")
    second = _ready_requirement(isolated_requirement_database, key_suffix="second")

    with pytest.raises(WorkItemNotFound):
        with isolated_requirement_database.runtime.begin() as db:
            start_work_item(
                db,
                requirement_id=first.requirement.id,
                work_item_id=second.work_items[0].id,
                expected_revision=first.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="start-cross-requirement",
                dependencies=_gate_dependencies(),
            )


def test_start_replays_without_advancing_revisions_or_audit_twice(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    ready = _ready_requirement(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        first = start_work_item(
            db,
            requirement_id=ready.requirement.id,
            work_item_id=ready.work_items[0].id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="start-human-work-replay",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = start_work_item(
            db,
            requirement_id=ready.requirement.id,
            work_item_id=ready.work_items[0].id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="start-human-work-replay",
            dependencies=dependencies,
        )
    with isolated_requirement_database.owner.connect() as db:
        audit_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='requirement.work_item.started' AND target_id=:work_item_id"
            ),
            {"work_item_id": ready.work_items[0].id},
        ).scalar_one()

    assert replay == first
    assert audit_count == 1


def test_start_rejects_same_key_with_a_different_payload(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    ready = _ready_requirement(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        start_work_item(
            db,
            requirement_id=ready.requirement.id,
            work_item_id=ready.work_items[0].id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="start-human-work-conflict",
            dependencies=_gate_dependencies(),
        )

    with pytest.raises(IdempotencyConflict):
        with isolated_requirement_database.runtime.begin() as db:
            start_work_item(
                db,
                requirement_id=ready.requirement.id,
                work_item_id=ready.work_items[0].id,
                expected_revision=ready.requirement.revision + 1,
                actor=Actor("employee-1"),
                idempotency_key="start-human-work-conflict",
                dependencies=_gate_dependencies(),
            )


def test_request_integration_mr_persists_intent_and_exact_stable_outbox(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    started = _started_work_item(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        result = request_integration_merge_request(
            db,
            requirement_id=started.requirement.id,
            work_item_id=started.work_item.id,
            expected_revision=started.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="request-integration-mr-1",
            dependencies=_gate_dependencies(),
        )
    with isolated_requirement_database.owner.connect() as db:
        outbox = db.execute(
            text(
                "SELECT topic, aggregate_version, payload FROM requirement.outbox_message "
                "WHERE aggregate_id=:requirement_id "
                "AND topic='requirement.integration-merge-request.requested'"
            ),
            {"requirement_id": started.requirement.id},
        ).one()

    assert result.work_item.integration_delivery_state is IntegrationDeliveryState.MR_PENDING
    assert result.outbox_topic == "requirement.integration-merge-request.requested"
    assert outbox == (
        "requirement.integration-merge-request.requested",
        result.requirement.revision,
        {
            "kind": "CREATE_MR",
            "requirementId": result.requirement.id,
            "requirementRevision": result.requirement.revision,
            "workItemId": result.work_item.id,
            "workItemRevision": result.work_item.revision,
            "repositoryId": result.work_item.repository_id,
            "actorId": "employee-1",
        },
    )
    assert not ({"branch", "projectId", "mrIid", "headSha"} & set(outbox.payload))


def test_request_integration_mr_replays_without_a_second_outbox(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    started = _started_work_item(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        first = request_integration_merge_request(
            db,
            requirement_id=started.requirement.id,
            work_item_id=started.work_item.id,
            expected_revision=started.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="request-integration-mr-replay",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = request_integration_merge_request(
            db,
            requirement_id=started.requirement.id,
            work_item_id=started.work_item.id,
            expected_revision=started.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="request-integration-mr-replay",
            dependencies=dependencies,
        )
    with isolated_requirement_database.owner.connect() as db:
        count = db.execute(
            text(
                "SELECT count(*) FROM requirement.outbox_message "
                "WHERE aggregate_id=:requirement_id "
                "AND topic='requirement.integration-merge-request.requested'"
            ),
            {"requirement_id": started.requirement.id},
        ).scalar_one()

    assert replay == first
    assert count == 1


def test_request_integration_merge_persists_binding_reference_only(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    current, binding_id = _merge_ready_work_item(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        result = request_integration_merge(
            db,
            requirement_id=current.requirement.id,
            work_item_id=current.work_items[0].id,
            expected_revision=current.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="request-integration-merge-1",
            dependencies=_gate_dependencies(),
        )
    with isolated_requirement_database.owner.connect() as db:
        payload = db.execute(
            text(
                "SELECT payload FROM requirement.outbox_message "
                "WHERE aggregate_id=:requirement_id "
                "AND topic='requirement.integration-merge.requested'"
            ),
            {"requirement_id": current.requirement.id},
        ).scalar_one()

    assert result.work_item.integration_delivery_state is IntegrationDeliveryState.MERGE_PENDING
    assert result.outbox_topic == "requirement.integration-merge.requested"
    assert payload == {
        "kind": "MERGE_MR",
        "requirementId": result.requirement.id,
        "requirementRevision": result.requirement.revision,
        "workItemId": result.work_item.id,
        "workItemRevision": result.work_item.revision,
        "integrationMergeRequestBindingId": binding_id,
        "repositoryId": result.work_item.repository_id,
        "actorId": "employee-1",
    }
    assert not ({"branch", "projectId", "mrIid", "headSha"} & set(payload))


def _merge_ready_work_item(
    database: IsolatedRequirementDatabase,
    *,
    key_suffix: str = "default",
) -> tuple[RequirementDetailsDto, str]:
    started = _started_work_item(database, key_suffix=key_suffix)
    binding_id = "30000000-0000-0000-0000-000000000301"
    with database.owner.begin() as db:
        db.execute(
            text("UPDATE requirement.requirement SET state='VERIFYING' WHERE id=:requirement_id"),
            {"requirement_id": started.requirement.id},
        )
        db.execute(
            text(
                "UPDATE requirement.work_item SET state='VERIFYING', "
                "integration_delivery_state='MR_OPEN', "
                "integration_merge_request_binding_id=:binding_id "
                "WHERE id=:work_item_id"
            ),
            {"binding_id": binding_id, "work_item_id": started.work_item.id},
        )
    with database.runtime.connect() as db:
        current = get_requirement(
            db,
            requirement_id=started.requirement.id,
            dependencies=_gate_dependencies(),
        )
    return current, binding_id


def test_request_integration_merge_rejects_merge_capable_non_owner(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    current, _binding_id = _merge_ready_work_item(
        isolated_requirement_database,
        key_suffix="non-owner",
    )

    with pytest.raises(WorkItemActorDenied):
        with isolated_requirement_database.runtime.begin() as db:
            request_integration_merge(
                db,
                requirement_id=current.requirement.id,
                work_item_id=current.work_items[0].id,
                expected_revision=current.requirement.revision,
                actor=Actor("merge-operator-1"),
                idempotency_key="request-integration-merge-non-owner",
                dependencies=_gate_dependencies(),
            )
    with isolated_requirement_database.owner.connect() as db:
        delivery_state = db.execute(
            text(
                "SELECT integration_delivery_state FROM requirement.work_item "
                "WHERE id=:work_item_id"
            ),
            {"work_item_id": current.work_items[0].id},
        ).scalar_one()
        outbox_count = db.execute(
            text(
                "SELECT count(*) FROM requirement.outbox_message "
                "WHERE aggregate_id=:requirement_id "
                "AND topic='requirement.integration-merge.requested'"
            ),
            {"requirement_id": current.requirement.id},
        ).scalar_one()

    assert delivery_state == IntegrationDeliveryState.MR_OPEN.value
    assert outbox_count == 0
