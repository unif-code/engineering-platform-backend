import hashlib
import json

import pytest
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    AssignmentState,
    RepositoryBindingBlockedReason,
    RepositoryState,
    StaleWorkItemRevision,
    WorkItemAssigneeIneligible,
    add_work_item,
    assign_work_item,
    record_repository_binding,
    record_repository_binding_blocked,
    register_sdd_baseline,
    start_requirement_preparation,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_baseline_gate import _gate_dependencies, _prepare
from tests.requirement.test_commands import Actor, _create, _dependencies


def _set_hash(work_item_ids: list[str]) -> str:
    canonical = json.dumps(sorted(work_item_ids), separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def test_add_work_item_uses_frozen_route_versions_plan_and_clears_stale_baseline(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-stale-baseline",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        added = add_work_item(
            db,
            requirement_id=created.requirement.id,
            repository_id="repository-2",
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-add-work-item",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = add_work_item(
            db,
            requirement_id=created.requirement.id,
            repository_id="repository-2",
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-add-work-item",
            dependencies=dependencies,
        )

    assert replay == added
    assert added.requirement.requirement_version == 2
    assert added.requirement.required_work_item_set_version == 2
    assert added.requirement.current_sdd_baseline_id is None
    assert added.requirement.required_work_item_set_hash == _set_hash(
        [created.work_item.id, added.work_item.id]
    )
    assert added.work_item.required_capabilities == ("code.change",)
    assert added.work_item.assignment_state is AssignmentState.ASSIGNED
    assert added.assignment is not None
    assert added.assignment.assignee_id == "employee-1"
    with isolated_requirement_database.owner.connect() as db:
        facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM requirement.work_item WHERE requirement_id=:id), "
                "(SELECT count(*) FROM requirement.outbox_message WHERE aggregate_id=:id), "
                "(SELECT count(*) FROM requirement.sdd_baseline WHERE requirement_id=:id)"
            ),
            {"id": created.requirement.id},
        ).one()
    assert facts == (2, 2, 1)


def test_assign_and_reassign_work_item_append_responsibility_history_with_cas(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        auto_assign=False,
        idempotency_key="planning-create-unassigned",
    )
    with isolated_requirement_database.runtime.begin() as db:
        start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=created.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-prepare-unassigned",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        assigned = assign_work_item(
            db,
            requirement_id=created.requirement.id,
            work_item_id=created.work_item.id,
            human_owner_id="employee-2",
            reason="Primary implementer",
            expected_revision=created.work_item.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-assign-employee-2",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        reassigned = assign_work_item(
            db,
            requirement_id=created.requirement.id,
            work_item_id=created.work_item.id,
            human_owner_id="employee-3",
            reason="Balance delivery load",
            expected_revision=assigned.work_item.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-assign-employee-3",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = assign_work_item(
            db,
            requirement_id=created.requirement.id,
            work_item_id=created.work_item.id,
            human_owner_id="employee-3",
            reason="Balance delivery load",
            expected_revision=assigned.work_item.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-assign-employee-3",
            dependencies=_dependencies(),
        )

    assert replay == reassigned
    assert assigned.work_item.human_owner_id == "employee-2"
    assert assigned.assignment.revision == 1
    assert reassigned.work_item.human_owner_id == "employee-3"
    assert reassigned.work_item.revision == 3
    assert reassigned.assignment.revision == 2
    with isolated_requirement_database.owner.connect() as db:
        history = db.execute(
            text(
                "SELECT assignee_id, revision, superseded_at IS NULL "
                "FROM requirement.work_item_assignment "
                "WHERE work_item_id=:id ORDER BY revision"
            ),
            {"id": created.work_item.id},
        ).all()
    assert [tuple(row) for row in history] == [
        ("employee-2", 1, False),
        ("employee-3", 2, True),
    ]


def test_assign_work_item_fails_closed_for_ineligible_candidate_and_stale_writer(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        auto_assign=False,
        idempotency_key="planning-create-denied",
    )
    with isolated_requirement_database.runtime.begin() as db:
        start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=created.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-prepare-denied",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(WorkItemAssigneeIneligible):
            assign_work_item(
                db,
                requirement_id=created.requirement.id,
                work_item_id=created.work_item.id,
                human_owner_id="employee-2",
                reason="Not currently eligible",
                expected_revision=created.work_item.revision,
                actor=Actor("employee-1"),
                idempotency_key="planning-assign-denied",
                dependencies=_dependencies(auto_assign=False),
            )
    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(StaleWorkItemRevision):
            assign_work_item(
                db,
                requirement_id=created.requirement.id,
                work_item_id=created.work_item.id,
                human_owner_id="employee-2",
                reason="Stale writer",
                expected_revision=99,
                actor=Actor("employee-1"),
                idempotency_key="planning-assign-stale",
                dependencies=_dependencies(),
            )


def test_reassignment_preserves_an_immutable_bound_branch(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        idempotency_key="planning-create-bound",
    )
    with isolated_requirement_database.runtime.begin() as db:
        start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=created.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-prepare-bound",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        bound = record_repository_binding(
            db,
            work_item_id=created.work_item.id,
            repository_id="repository-1",
            base_commit_sha="b" * 40,
            task_branch="work-items/immutable-binding",
            expected_revision=created.work_item.revision,
            actor=Actor("SYSTEM"),
            idempotency_key="planning-bind-before-reassign",
            correlation_id="source-control:effect:planning-bound",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        reassigned = assign_work_item(
            db,
            requirement_id=created.requirement.id,
            work_item_id=created.work_item.id,
            human_owner_id="employee-2",
            reason="New implementer",
            expected_revision=bound.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-reassign-bound",
            dependencies=_dependencies(),
        )

    assert reassigned.work_item.repository_state is RepositoryState.BOUND
    assert reassigned.work_item.base_commit_sha == "b" * 40
    assert reassigned.work_item.task_branch == "work-items/immutable-binding"
    assert reassigned.assignment.revision == 2


def test_assignment_recovers_owner_block_and_reemits_binding_request(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(
        isolated_requirement_database,
        auto_assign=False,
        idempotency_key="planning-create-owner-blocked",
    )
    with isolated_requirement_database.runtime.begin() as db:
        start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=created.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-prepare-owner-blocked",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        blocked = record_repository_binding_blocked(
            db,
            work_item_id=created.work_item.id,
            repository_id="repository-1",
            reason_code=RepositoryBindingBlockedReason.OWNER_UNASSIGNED,
            expected_revision=created.work_item.revision,
            actor=Actor("SYSTEM"),
            idempotency_key="planning-owner-blocked",
            correlation_id="source-control:work-item:planning-owner-blocked",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        assigned = assign_work_item(
            db,
            requirement_id=created.requirement.id,
            work_item_id=created.work_item.id,
            human_owner_id="employee-2",
            reason="Resolve missing owner",
            expected_revision=blocked.revision,
            actor=Actor("employee-1"),
            idempotency_key="planning-resolve-owner-block",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.owner.connect() as db:
        binding_request_count = db.execute(
            text(
                "SELECT count(*) FROM requirement.outbox_message "
                "WHERE aggregate_id=:id "
                "AND topic='requirement.repository-binding.requested'"
            ),
            {"id": created.requirement.id},
        ).scalar_one()

    assert assigned.work_item.repository_state is RepositoryState.WAITING_REPOSITORY
    assert assigned.work_item.repository_blocked_reason_code is None
    assert assigned.work_item.repository_blocked_at is None
    assert binding_request_count == 2
