import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.requirement import (
    AssignmentState,
    CreateRequirementResult,
    InvalidRequirementInput,
    RepositoryBindingMessageInvalid,
    RepositoryBindingRequestMessage,
    RequirementDependencies,
    RequirementState,
    RequirementType,
    acknowledge_repository_binding_request,
    claim_repository_binding_requests,
    create_requirement,
    get_repository_binding_context,
    release_repository_binding_request,
)
from control_plane.app.modules.requirement.adapters import SqlAlchemyRequirementRepository
from control_plane.app.modules.requirement.ports import RouteSnapshot
from control_plane.app.shared.security import SecretMaterial
from tests.requirement.conftest import IsolatedRequirementDatabase

NOW = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)
WORKSPACE_ID = "20000000-0000-0000-0000-000000000401"


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


class StaticRouteSnapshots:
    def current(self, requirement_type: RequirementType) -> RouteSnapshot:
        assert requirement_type is RequirementType.FEAT
        payload = {
            "requirementType": requirement_type.value,
            "requiredCapabilities": ["code.change"],
            "steps": [],
            "version": 1,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return RouteSnapshot(
            version=1,
            snapshot_hash=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            required_capabilities=("code.change",),
        )


class StaticAssignmentGuard:
    def can_auto_assign(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: str,
        required_capabilities: tuple[str, ...],
    ) -> bool:
        del actor_id, workspace_id, repository_id, required_capabilities
        return True


class DurableDenialAudit:
    def append(self, envelope: object) -> None:
        del envelope


def dependencies() -> RequirementDependencies:
    return RequirementDependencies(
        repository_factory=SqlAlchemyRequirementRepository,
        audit=SqlAlchemyTransactionalAuditAppender(),
        denial_audit=DurableDenialAudit(),
        clock=FixedClock(),
        random=RandomValues(),
        route_snapshots=StaticRouteSnapshots(),
        assignment_guard=StaticAssignmentGuard(),
        secret_manager=StaticSecrets(),
    )


def create_assigned_requirement(
    database: IsolatedRequirementDatabase,
) -> CreateRequirementResult:
    with database.runtime.begin() as db:
        return create_requirement(
            db,
            workspace_id=WORKSPACE_ID,
            requirement_type=RequirementType.FEAT,
            title="Create governed task branch",
            description="Bind the WorkItem to an authorized repository.",
            acceptance_criteria=("branch binding is auditable",),
            initial_repository_id="repository-source-control-1",
            actor=Actor("employee-source-control-1"),
            idempotency_key=f"create-{uuid4()}",
            dependencies=dependencies(),
        )


def test_claim_leases_existing_binding_request_without_publishing(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = create_assigned_requirement(isolated_requirement_database)
    lease_until = NOW + timedelta(minutes=1)

    with isolated_requirement_database.runtime.begin() as db:
        messages = claim_repository_binding_requests(
            db,
            limit=10,
            available_before=NOW,
            lease_until=lease_until,
            dependencies=dependencies(),
        )

    with isolated_requirement_database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT state, attempts, available_at FROM requirement.outbox_message WHERE id=:id"
            ),
            {"id": messages[0].message_id},
        ).one()

    assert [(message.work_item_id, message.repository_id) for message in messages] == [
        (created.work_item.id, created.work_item.repository_id)
    ]
    assert row == ("PENDING", 1, lease_until)


def test_claim_leaves_messages_unavailable_until_their_retry_time(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = create_assigned_requirement(isolated_requirement_database)
    retry_at = NOW + timedelta(minutes=5)
    with isolated_requirement_database.runtime.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.outbox_message SET state='FAILED', available_at=:retry_at "
                "WHERE aggregate_id=:requirement_id"
            ),
            {"retry_at": retry_at, "requirement_id": created.requirement.id},
        )

    with isolated_requirement_database.runtime.begin() as db:
        messages = claim_repository_binding_requests(
            db,
            limit=10,
            available_before=NOW,
            lease_until=NOW + timedelta(minutes=1),
            dependencies=dependencies(),
        )

    with isolated_requirement_database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT state, attempts, available_at "
                "FROM requirement.outbox_message WHERE aggregate_id=:requirement_id"
            ),
            {"requirement_id": created.requirement.id},
        ).one()

    assert messages == ()
    assert row == ("FAILED", 0, retry_at)


def test_ack_is_idempotent_and_moves_created_requirement_to_preparing(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    create_assigned_requirement(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        message = claim_repository_binding_requests(
            db,
            limit=1,
            available_before=NOW,
            lease_until=NOW + timedelta(minutes=1),
            dependencies=dependencies(),
        )[0]

    with isolated_requirement_database.runtime.begin() as db:
        first = acknowledge_repository_binding_request(
            db,
            message_id=message.message_id,
            consumer="SOURCE_CONTROL",
            dependencies=dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = acknowledge_repository_binding_request(
            db,
            message_id=message.message_id,
            consumer="SOURCE_CONTROL",
            dependencies=dependencies(),
        )

    with isolated_requirement_database.owner.connect() as db:
        outbox_state = db.execute(
            text("SELECT state FROM requirement.outbox_message WHERE id=:id"),
            {"id": message.message_id},
        ).scalar_one()

    assert first.state is RequirementState.PREPARING
    assert first.revision == 2
    assert replay == first
    assert outbox_state == "PUBLISHED"


def test_context_exposes_current_facts_without_private_rows(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = create_assigned_requirement(isolated_requirement_database)

    with isolated_requirement_database.runtime.connect() as db:
        context = get_repository_binding_context(
            db,
            work_item_id=created.work_item.id,
            dependencies=dependencies(),
        )

    assert context.requirement_id == created.requirement.id
    assert context.requirement_type is RequirementType.FEAT
    assert context.requirement_title == "Create governed task branch"
    assert context.workspace_id == WORKSPACE_ID
    assert context.work_item_revision == created.work_item.revision
    assert context.assignment_state is AssignmentState.ASSIGNED
    assert context.human_owner_id == "employee-source-control-1"
    assert context.required_capabilities == ("code.change",)
    assert context.repository_id == "repository-source-control-1"


def test_release_records_only_a_safe_error_code_and_retry_time(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    create_assigned_requirement(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        message = claim_repository_binding_requests(
            db,
            limit=1,
            available_before=NOW,
            lease_until=NOW + timedelta(minutes=1),
            dependencies=dependencies(),
        )[0]

    retry_at = NOW + timedelta(minutes=3)
    with isolated_requirement_database.runtime.begin() as db:
        release_repository_binding_request(
            db,
            message_id=message.message_id,
            error_code="SOURCE_CONTROL_UNAVAILABLE",
            available_at=retry_at,
            dependencies=dependencies(),
        )

    with isolated_requirement_database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT state, last_error_code, available_at, published_at "
                "FROM requirement.outbox_message WHERE id=:id"
            ),
            {"id": message.message_id},
        ).one()

    assert row == ("FAILED", "SOURCE_CONTROL_UNAVAILABLE", retry_at, None)


def test_claim_rejects_a_malformed_historical_binding_payload(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created = create_assigned_requirement(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.outbox_message "
                "SET payload=CAST(:payload AS JSONB) WHERE aggregate_id=:requirement_id"
            ),
            {
                "payload": '{"workItemId":"missing-repository"}',
                "requirement_id": created.requirement.id,
            },
        )

    with pytest.raises(RepositoryBindingMessageInvalid):
        with isolated_requirement_database.runtime.begin() as db:
            claim_repository_binding_requests(
                db,
                limit=1,
                available_before=NOW,
                lease_until=NOW + timedelta(minutes=1),
                dependencies=dependencies(),
            )


def test_release_rejects_an_error_code_that_could_expose_provider_details(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    create_assigned_requirement(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        message = claim_repository_binding_requests(
            db,
            limit=1,
            available_before=NOW,
            lease_until=NOW + timedelta(minutes=1),
            dependencies=dependencies(),
        )[0]

    with pytest.raises(InvalidRequirementInput):
        with isolated_requirement_database.runtime.begin() as db:
            release_repository_binding_request(
                db,
                message_id=message.message_id,
                error_code="provider timeout response included credential material",
                available_at=NOW + timedelta(minutes=3),
                dependencies=dependencies(),
            )

    with isolated_requirement_database.owner.connect() as db:
        row = db.execute(
            text("SELECT state, last_error_code FROM requirement.outbox_message WHERE id=:id"),
            {"id": message.message_id},
        ).one()

    assert row == ("PENDING", None)


def test_concurrent_claims_skip_locked_messages_and_return_disjoint_ids(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    create_assigned_requirement(isolated_requirement_database)
    create_assigned_requirement(isolated_requirement_database)
    first_connection = isolated_requirement_database.runtime.connect()
    first_transaction = first_connection.begin()
    try:
        first = claim_repository_binding_requests(
            first_connection,
            limit=1,
            available_before=NOW,
            lease_until=NOW + timedelta(minutes=1),
            dependencies=dependencies(),
        )

        def claim_second() -> tuple[RepositoryBindingRequestMessage, ...]:
            with isolated_requirement_database.runtime.begin() as db:
                return claim_repository_binding_requests(
                    db,
                    limit=1,
                    available_before=NOW,
                    lease_until=NOW + timedelta(minutes=1),
                    dependencies=dependencies(),
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(claim_second)
            try:
                second = future.result(timeout=1)
            except TimeoutError:
                first_transaction.commit()
                pytest.fail("a concurrent claim waited on a leased row instead of skipping it")
    finally:
        if first_transaction.is_active:
            first_transaction.commit()
        first_connection.close()

    assert len(first) == len(second) == 1
    assert first[0].message_id != second[0].message_id
