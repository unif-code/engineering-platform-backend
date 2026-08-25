from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import Connection, text

from control_plane.app.modules.audit import AuditEnvelope, TransactionalAuditAppender
from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.requirement import (
    AssignmentState,
    CreateRequirementResult,
    RepositoryBindingRequestMissing,
    RepositoryState,
    RequirementDependencies,
    RequirementState,
    RequirementType,
    WorkItemState,
    create_requirement,
    start_requirement_preparation,
)
from control_plane.app.modules.requirement.adapters import SqlAlchemyRequirementRepository
from control_plane.app.modules.requirement.ports import AssignmentGuardPort, RouteSnapshot
from control_plane.app.shared.idempotency import IdempotencyConflict
from control_plane.app.shared.security import SecretMaterial
from tests.requirement.conftest import IsolatedRequirementDatabase

WORKSPACE_ID = "20000000-0000-0000-0000-000000000301"
NOW = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Actor:
    account_id: str


class FixedClock:
    def now(self) -> datetime:
        return NOW


class RandomValues:
    def uuid4(self) -> object:
        return uuid4()


class StaticSecrets:
    def load(self) -> SecretMaterial:
        return SecretMaterial(b"p" * 32, b"t" * 32, b"i" * 32)


@dataclass(frozen=True, slots=True)
class StaticRouteSnapshots:
    def current(self, requirement_type: RequirementType) -> RouteSnapshot:
        assert requirement_type is RequirementType.FEAT
        return RouteSnapshot(
            version=1,
            snapshot_hash="sha256:route-1",
            required_capabilities=("code.change",),
        )


@dataclass(frozen=True, slots=True)
class StaticAssignmentGuard:
    allowed: bool

    def can_auto_assign(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool:
        assert actor_id == "employee-1"
        assert workspace_id == WORKSPACE_ID
        assert repository_id == "repository-1"
        assert required_capabilities == ("code.change",)
        return self.allowed


@dataclass(frozen=True, slots=True)
class BlockingAssignmentGuard:
    entered: Event
    release: Event

    def can_auto_assign(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool:
        del actor_id, workspace_id, repository_id, required_capabilities
        self.entered.set()
        assert self.release.wait(timeout=5)
        return True


class FailingAudit:
    def append_in_transaction(self, db: Connection, envelope: AuditEnvelope) -> None:
        del db, envelope
        raise RuntimeError("audit unavailable")


def _dependencies(
    *,
    auto_assign: bool = True,
    assignment_guard: AssignmentGuardPort | None = None,
    audit: TransactionalAuditAppender | None = None,
) -> RequirementDependencies:
    return RequirementDependencies(
        repository_factory=SqlAlchemyRequirementRepository,
        audit=audit or SqlAlchemyTransactionalAuditAppender(),
        clock=FixedClock(),
        random=RandomValues(),
        route_snapshots=StaticRouteSnapshots(),
        assignment_guard=assignment_guard or StaticAssignmentGuard(auto_assign),
        secret_manager=StaticSecrets(),
    )


def _create(
    database: IsolatedRequirementDatabase,
    *,
    auto_assign: bool = True,
    idempotency_key: str = "requirement-create-0001",
) -> CreateRequirementResult:
    with database.runtime.begin() as db:
        return create_requirement(
            db,
            workspace_id=WORKSPACE_ID,
            requirement_type=RequirementType.FEAT,
            title="  Govern delivery  ",
            description="  Create an auditable workflow.  ",
            acceptance_criteria=("  baseline approved  ",),
            initial_repository_id="repository-1",
            actor=Actor("employee-1"),
            idempotency_key=idempotency_key,
            dependencies=_dependencies(auto_assign=auto_assign),
        )


def test_create_requirement_persists_initial_work_item_outbox_and_audit_atomically(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(isolated_requirement_database)

    with isolated_requirement_database.owner.connect() as db:
        requirement = db.execute(
            text(
                "SELECT title, description, acceptance_criteria, state, revision "
                "FROM requirement.requirement WHERE id=:id"
            ),
            {"id": created.requirement.id},
        ).one()
        work_item = db.execute(
            text(
                "SELECT human_owner_id, assignment_state, repository_state, state "
                "FROM requirement.work_item WHERE id=:id"
            ),
            {"id": created.work_item.id},
        ).one()
        outbox = db.execute(
            text(
                "SELECT topic, payload, state FROM requirement.outbox_message "
                "WHERE aggregate_id=:id"
            ),
            {"id": created.requirement.id},
        ).one()
        audit_actions = list(
            db.execute(
                text(
                    "SELECT action FROM audit.audit_event "
                    "WHERE target_id IN (:requirement_id, :work_item_id) ORDER BY action"
                ),
                {
                    "requirement_id": created.requirement.id,
                    "work_item_id": created.work_item.id,
                },
            ).scalars()
        )

    assert created.requirement.state is RequirementState.CREATED
    assert requirement == (
        "Govern delivery",
        "Create an auditable workflow.",
        ["baseline approved"],
        "CREATED",
        1,
    )
    assert created.work_item.assignment_state is AssignmentState.ASSIGNED
    assert work_item == ("employee-1", "ASSIGNED", "WAITING_REPOSITORY", "DRAFT")
    assert outbox == (
        "requirement.repository-binding.requested",
        {"repositoryId": "repository-1", "workItemId": created.work_item.id},
        "PENDING",
    )
    assert audit_actions == [
        "requirement.created",
        "requirement.work_item.initialized",
    ]


def test_create_requirement_leaves_work_item_unassigned_when_guard_denies(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(isolated_requirement_database, auto_assign=False)

    assert created.work_item.human_owner_id is None
    assert created.work_item.assignment_state is AssignmentState.UNASSIGNED
    assert created.work_item.repository_state is RepositoryState.WAITING_REPOSITORY
    assert created.work_item.state is WorkItemState.DRAFT


def test_start_preparation_requires_durable_binding_request(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    missing_request_id = "10000000-0000-0000-0000-000000000309"
    with isolated_requirement_database.runtime.begin() as db:
        SqlAlchemyRequirementRepository(db).insert_requirement(
            id=missing_request_id,
            workspace_id=WORKSPACE_ID,
            type="feat",
            title="Missing request",
            description="No binding request was persisted.",
            acceptance_criteria=("blocked",),
            created_by="employee-1",
            initial_repository_id="repository-1",
            route_snapshot_version=1,
            route_snapshot_hash="sha256:route-1",
            state="CREATED",
            record_state="ACTIVE",
            requirement_version=1,
            required_work_item_set_version=1,
            required_work_item_set_hash="sha256:missing",
            revision=1,
            now=NOW,
        )
        with pytest.raises(RepositoryBindingRequestMissing):
            start_requirement_preparation(
                db,
                requirement_id=missing_request_id,
                expected_revision=1,
                actor=Actor("employee-1"),
                idempotency_key="requirement-start-missing-0001",
                dependencies=_dependencies(),
            )


def test_start_preparation_advances_created_requirement_after_durable_request(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=1,
            actor=Actor("employee-1"),
            idempotency_key="requirement-start-0001",
            dependencies=_dependencies(),
        )

    assert prepared.state is RequirementState.PREPARING
    assert prepared.revision == 2


def test_start_preparation_replays_after_the_state_has_advanced(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = _create(isolated_requirement_database, idempotency_key="create-for-start-replay")
    with isolated_requirement_database.runtime.begin() as db:
        first = start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=1,
            actor=Actor("employee-1"),
            idempotency_key="requirement-start-replay-0001",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=1,
            actor=Actor("employee-1"),
            idempotency_key="requirement-start-replay-0001",
            dependencies=_dependencies(),
        )
    with isolated_requirement_database.owner.connect() as db:
        started_count = db.execute(
            text(
                "SELECT count(*) FROM audit.audit_event "
                "WHERE action='requirement.preparation.started'"
            )
        ).scalar_one()

    assert replay == first
    assert started_count == 1


def test_create_requirement_replays_the_same_result_for_the_same_idempotency_key(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    first = _create(isolated_requirement_database, idempotency_key="requirement-replay-0001")
    replay = _create(isolated_requirement_database, idempotency_key="requirement-replay-0001")

    with isolated_requirement_database.owner.connect() as db:
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM requirement.requirement), "
                "(SELECT count(*) FROM requirement.work_item), "
                "(SELECT count(*) FROM requirement.outbox_message), "
                "(SELECT count(*) FROM audit.audit_event WHERE action LIKE 'requirement.%')"
            )
        ).one()

    assert replay == first
    assert counts == (1, 1, 1, 2)


def test_create_requirement_rejects_same_key_with_different_payload(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    _create(isolated_requirement_database, idempotency_key="requirement-conflict-0001")

    with isolated_requirement_database.runtime.begin() as db, pytest.raises(IdempotencyConflict):
        create_requirement(
            db,
            workspace_id=WORKSPACE_ID,
            requirement_type=RequirementType.FEAT,
            title="Different title",
            description="Create an auditable workflow.",
            acceptance_criteria=("baseline approved",),
            initial_repository_id="repository-1",
            actor=Actor("employee-1"),
            idempotency_key="requirement-conflict-0001",
            dependencies=_dependencies(),
        )


def test_create_requirement_rolls_back_claim_and_facts_when_audit_fails(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    with pytest.raises(RuntimeError, match="audit unavailable"):
        with isolated_requirement_database.runtime.begin() as db:
            create_requirement(
                db,
                workspace_id=WORKSPACE_ID,
                requirement_type=RequirementType.FEAT,
                title="Rollback",
                description="Every fact must roll back.",
                acceptance_criteria=("nothing persists",),
                initial_repository_id="repository-1",
                actor=Actor("employee-1"),
                idempotency_key="requirement-rollback-0001",
                dependencies=_dependencies(audit=FailingAudit()),
            )
    with isolated_requirement_database.owner.connect() as db:
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM requirement.requirement), "
                "(SELECT count(*) FROM requirement.work_item), "
                "(SELECT count(*) FROM requirement.outbox_message), "
                "(SELECT count(*) FROM requirement.idempotency_record)"
            )
        ).one()

    assert counts == (0, 0, 0, 0)


def test_concurrent_same_key_creates_one_requirement_and_exact_replay(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    entered = Event()
    release = Event()
    dependencies = _dependencies(
        assignment_guard=BlockingAssignmentGuard(entered=entered, release=release)
    )

    def send() -> CreateRequirementResult:
        with isolated_requirement_database.runtime.begin() as db:
            return create_requirement(
                db,
                workspace_id=WORKSPACE_ID,
                requirement_type=RequirementType.FEAT,
                title="Concurrent",
                description="Only one aggregate is allowed.",
                acceptance_criteria=("one aggregate",),
                initial_repository_id="repository-1",
                actor=Actor("employee-1"),
                idempotency_key="requirement-concurrent-0001",
                dependencies=dependencies,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send)
        assert entered.wait(timeout=3)
        second = pool.submit(send)
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.5)
        finally:
            release.set()
        results = [first.result(timeout=3), second.result(timeout=3)]

    with isolated_requirement_database.owner.connect() as db:
        counts = db.execute(
            text(
                "SELECT (SELECT count(*) FROM requirement.requirement), "
                "(SELECT count(*) FROM requirement.work_item), "
                "(SELECT count(*) FROM requirement.outbox_message), "
                "(SELECT count(*) FROM requirement.idempotency_record)"
            )
        ).one()

    assert results[0] == results[1]
    assert counts == (1, 1, 1, 1)
