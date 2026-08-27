import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryMessageInvalid,
    IntegrationDeliveryRequestKind,
    IntegrationDeliveryRequestMessage,
    IntegrationDeliveryState,
    InvalidRequirementInput,
    RequirementState,
    StaleWorkItemRevision,
    WorkItemDeliveryConflict,
    WorkItemDeliveryResult,
    WorkItemState,
    acknowledge_integration_delivery_request,
    claim_integration_delivery_requests,
    get_integration_delivery_context,
    record_external_merge_drift,
    record_integration_delivery_blocked,
    record_integration_merged,
    record_integration_mr_ready,
    record_integration_reconciliation_pending,
    release_integration_delivery_request,
    request_integration_merge,
    request_integration_merge_request,
)
from control_plane.app.shared.idempotency import IdempotencyConflict
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_baseline_gate import _gate_dependencies
from tests.requirement.test_commands import Actor
from tests.requirement.test_delivery_commands import _started_work_item

NOW = datetime(2026, 8, 26, 3, 0, tzinfo=UTC)
BINDING_ID = "30000000-0000-0000-0000-000000000301"
OTHER_BINDING_ID = "30000000-0000-0000-0000-000000000302"
SYSTEM_ACTOR = Actor("source-control")


def _requested_mr(
    database: IsolatedRequirementDatabase,
    *,
    key_suffix: str = "default",
) -> WorkItemDeliveryResult:
    started = _started_work_item(database, key_suffix=key_suffix)
    with database.runtime.begin() as db:
        return request_integration_merge_request(
            db,
            requirement_id=started.requirement.id,
            work_item_id=started.work_item.id,
            expected_revision=started.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"request-mr-relay-{key_suffix}",
            dependencies=_gate_dependencies(),
        )


def _ready_mr(
    database: IsolatedRequirementDatabase,
    *,
    key_suffix: str = "default",
) -> WorkItemDeliveryResult:
    requested = _requested_mr(database, key_suffix=key_suffix)
    with database.runtime.begin() as db:
        return record_integration_mr_ready(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key=f"effect:{key_suffix}:mr-ready",
            correlation_id=f"source-control:effect:{key_suffix}:mr-ready",
            dependencies=_gate_dependencies(),
        )


def _requested_merge(
    database: IsolatedRequirementDatabase,
    *,
    key_suffix: str = "default",
) -> WorkItemDeliveryResult:
    ready = _ready_mr(database, key_suffix=key_suffix)
    with database.runtime.begin() as db:
        return request_integration_merge(
            db,
            requirement_id=ready.requirement.id,
            work_item_id=ready.work_item.id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"request-merge-relay-{key_suffix}",
            dependencies=_gate_dependencies(),
        )


def test_claim_delivery_requests_only_returns_two_allowlisted_topics(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_merge(isolated_requirement_database)

    with isolated_requirement_database.runtime.begin() as db:
        claimed = claim_integration_delivery_requests(
            db,
            limit=10,
            available_before=NOW,
            lease_until=NOW + timedelta(minutes=1),
            dependencies=_gate_dependencies(),
        )

    assert [item.kind for item in claimed] == [
        IntegrationDeliveryRequestKind.CREATE_MR,
        IntegrationDeliveryRequestKind.MERGE_MR,
    ]
    assert [item.work_item_id for item in claimed] == [
        requested.work_item.id,
        requested.work_item.id,
    ]
    assert [item.actor_id for item in claimed] == ["employee-1", "employee-1"]
    assert claimed[0].integration_merge_request_binding_id is None
    assert claimed[1].integration_merge_request_binding_id == BINDING_ID


def test_claim_replay_is_stable_and_malformed_same_message_is_rejected(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    _requested_mr(isolated_requirement_database, key_suffix="claim-replay")
    dependencies = _gate_dependencies()
    lease_until = NOW + timedelta(minutes=1)
    with isolated_requirement_database.runtime.begin() as db:
        first = claim_integration_delivery_requests(
            db,
            limit=1,
            available_before=NOW,
            lease_until=lease_until,
            dependencies=dependencies,
        )[0]
    with isolated_requirement_database.runtime.begin() as db:
        replay = claim_integration_delivery_requests(
            db,
            limit=1,
            available_before=lease_until,
            lease_until=lease_until + timedelta(minutes=1),
            dependencies=dependencies,
        )[0]

    assert replay.model_copy(update={"attempts": first.attempts}) == first
    assert replay.attempts == first.attempts + 1
    with pytest.raises(ValidationError):
        replay.actor_id = "provider-user"

    with isolated_requirement_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.outbox_message SET payload=payload - 'actorId' "
                "WHERE id=:message_id"
            ),
            {"message_id": first.message_id},
        )
    with pytest.raises(IntegrationDeliveryMessageInvalid):
        with isolated_requirement_database.runtime.begin() as db:
            claim_integration_delivery_requests(
                db,
                limit=1,
                available_before=lease_until + timedelta(minutes=1),
                lease_until=lease_until + timedelta(minutes=2),
                dependencies=dependencies,
            )


def test_claim_rejects_same_message_id_with_different_valid_payload(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    _requested_mr(isolated_requirement_database, key_suffix="payload-conflict")
    dependencies = _gate_dependencies()
    lease_until = NOW + timedelta(minutes=1)
    with isolated_requirement_database.runtime.begin() as db:
        first = claim_integration_delivery_requests(
            db,
            limit=1,
            available_before=NOW,
            lease_until=lease_until,
            dependencies=dependencies,
        )[0]
    with isolated_requirement_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.outbox_message "
                "SET payload=jsonb_set(payload, '{actorId}', to_jsonb(CAST(:actor_id AS TEXT))) "
                "WHERE id=:message_id"
            ),
            {"actor_id": "employee-2", "message_id": first.message_id},
        )

    with pytest.raises(IntegrationDeliveryMessageInvalid):
        with isolated_requirement_database.runtime.begin() as db:
            claim_integration_delivery_requests(
                db,
                limit=1,
                available_before=lease_until,
                lease_until=lease_until + timedelta(minutes=1),
                dependencies=dependencies,
            )


@pytest.mark.parametrize("invalid_revision", [True, -1])
def test_claim_rejects_boolean_or_negative_work_item_revision(
    isolated_requirement_database: IsolatedRequirementDatabase,
    invalid_revision: object,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="strict-revision")
    with isolated_requirement_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.outbox_message "
                "SET payload=jsonb_set(payload, '{workItemRevision}', CAST(:revision AS JSONB)) "
                "WHERE aggregate_id=:requirement_id "
                "AND topic='requirement.integration-merge-request.requested'"
            ),
            {
                "revision": json.dumps(invalid_revision),
                "requirement_id": requested.requirement.id,
            },
        )

    with pytest.raises(IntegrationDeliveryMessageInvalid):
        with isolated_requirement_database.runtime.begin() as db:
            claim_integration_delivery_requests(
                db,
                limit=1,
                available_before=NOW,
                lease_until=NOW + timedelta(minutes=1),
                dependencies=_gate_dependencies(),
            )


def test_concurrent_delivery_claims_skip_locked_rows(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    _requested_mr(isolated_requirement_database, key_suffix="claim-first")
    _requested_mr(isolated_requirement_database, key_suffix="claim-second")
    first_connection = isolated_requirement_database.runtime.connect()
    first_transaction = first_connection.begin()
    try:
        first = claim_integration_delivery_requests(
            first_connection,
            limit=1,
            available_before=NOW,
            lease_until=NOW + timedelta(minutes=1),
            dependencies=_gate_dependencies(),
        )

        def claim_second() -> tuple[IntegrationDeliveryRequestMessage, ...]:
            with isolated_requirement_database.runtime.begin() as db:
                return claim_integration_delivery_requests(
                    db,
                    limit=1,
                    available_before=NOW,
                    lease_until=NOW + timedelta(minutes=1),
                    dependencies=_gate_dependencies(),
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(claim_second)
            try:
                second = future.result(timeout=1)
            except TimeoutError:
                first_transaction.commit()
                pytest.fail("a delivery claim waited on a locked row")
    finally:
        if first_transaction.is_active:
            first_transaction.commit()
        first_connection.close()

    assert len(first) == len(second) == 1
    assert first[0].message_id != second[0].message_id


def test_ack_release_are_topic_scoped_idempotent_and_safe(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    _requested_mr(isolated_requirement_database, key_suffix="ack-release")
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        message = claim_integration_delivery_requests(
            db,
            limit=1,
            available_before=NOW,
            lease_until=NOW + timedelta(minutes=1),
            dependencies=dependencies,
        )[0]
    retry_at = NOW + timedelta(minutes=3)
    with isolated_requirement_database.runtime.begin() as db:
        release_integration_delivery_request(
            db,
            message_id=message.message_id,
            error_code="SOURCE_CONTROL_UNAVAILABLE",
            available_at=retry_at,
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        acknowledge_integration_delivery_request(
            db,
            message_id=message.message_id,
            consumer="SOURCE_CONTROL",
            dependencies=dependencies,
        )
        acknowledge_integration_delivery_request(
            db,
            message_id=message.message_id,
            consumer="SOURCE_CONTROL",
            dependencies=dependencies,
        )
    with isolated_requirement_database.owner.connect() as db:
        row = db.execute(
            text(
                "SELECT state, attempts, available_at, published_at, last_error_code "
                "FROM requirement.outbox_message WHERE id=:message_id"
            ),
            {"message_id": message.message_id},
        ).one()

    assert row.state == "PUBLISHED"
    assert row.attempts == 1
    assert row.available_at == retry_at
    assert row.published_at == dependencies.clock.now()
    assert row.last_error_code is None


def test_context_exposes_stable_requirement_facts_and_request_actor(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="context")

    with isolated_requirement_database.runtime.connect() as db:
        context = get_integration_delivery_context(
            db,
            work_item_id=requested.work_item.id,
            dependencies=_gate_dependencies(),
        )

    assert context.requirement_id == requested.requirement.id
    assert context.requirement_revision == requested.requirement.revision
    assert context.requirement_state is requested.requirement.state
    assert context.work_item_revision == requested.work_item.revision
    assert context.work_item_state is WorkItemState.IN_PROGRESS
    assert context.repository_state.value == "BOUND"
    assert context.human_owner_id == "employee-1"
    assert context.required_capabilities == ("code.change",)
    assert context.base_commit_sha == "a" * 40
    assert context.task_branch == f"task/{requested.work_item.id}"
    assert context.integration_delivery_state is IntegrationDeliveryState.MR_PENDING
    assert context.integration_merge_request_binding_id is None
    assert context.request_actor_id == "employee-1"
    assert not ({"project_id", "mr_iid", "provider_body", "credential"} & set(context.model_dump()))


def test_mr_ready_callback_moves_work_item_to_verifying_without_provider_fields(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="mr-ready")

    with isolated_requirement_database.runtime.begin() as db:
        result = record_integration_mr_ready(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:mr-ready:callback",
            correlation_id="source-control:effect:mr-ready:callback",
            dependencies=_gate_dependencies(),
        )

    assert result.work_item.state is WorkItemState.VERIFYING
    assert result.work_item.integration_delivery_state is IntegrationDeliveryState.MR_OPEN
    assert result.work_item.integration_merge_request_binding_id == BINDING_ID
    assert result.requirement.state.value == "VERIFYING"
    result_fields = set(result.requirement.model_dump()) | set(result.work_item.model_dump())
    assert not ({"project_id", "mr_iid", "head_sha", "provider_body"} & result_fields)


def test_integration_callback_audit_uses_explicit_source_control_correlation(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="audit-correlation")
    dependencies = _gate_dependencies()
    ready_correlation = "source-control:effect:create-audit-correlation"
    merged_correlation = "source-control:effect:merge-audit-correlation"
    with isolated_requirement_database.runtime.begin() as db:
        ready = record_integration_mr_ready(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:audit-correlation:mr-ready",
            correlation_id=ready_correlation,
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        merge_requested = request_integration_merge(
            db,
            requirement_id=ready.requirement.id,
            work_item_id=ready.work_item.id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="request-merge-audit-correlation",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        record_integration_merged(
            db,
            work_item_id=merge_requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=merge_requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:audit-correlation:merged",
            correlation_id=merged_correlation,
            dependencies=dependencies,
        )

    with isolated_requirement_database.owner.connect() as db:
        rows = db.execute(
            text(
                "SELECT action, target_id, correlation_id FROM audit.audit_event "
                "WHERE target_id=:work_item_id AND action IN ("
                "'requirement.integration_delivery.mr_ready', "
                "'requirement.integration_delivery.merged')"
            ),
            {"work_item_id": requested.work_item.id},
        ).all()
    assert {(row.action, row.target_id): row.correlation_id for row in rows} == {
        (
            "requirement.integration_delivery.mr_ready",
            requested.work_item.id,
        ): ready_correlation,
        (
            "requirement.integration_delivery.merged",
            requested.work_item.id,
        ): merged_correlation,
    }


def test_integration_callback_rejects_blank_correlation(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="blank-correlation")

    with pytest.raises(InvalidRequirementInput):
        with isolated_requirement_database.runtime.begin() as db:
            record_integration_mr_ready(
                db,
                work_item_id=requested.work_item.id,
                binding_id=BINDING_ID,
                expected_revision=requested.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:blank-correlation:mr-ready",
                correlation_id=" ",
                dependencies=_gate_dependencies(),
            )


def test_callback_idempotency_conflicts_on_same_key_with_different_binding(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="callback-idempotency")
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        first = record_integration_mr_ready(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:stable:mr-ready",
            correlation_id="source-control:effect:stable:mr-ready",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = record_integration_mr_ready(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:stable:mr-ready",
            correlation_id="source-control:effect:stable:mr-ready-replay",
            dependencies=dependencies,
        )

    assert replay == first
    with isolated_requirement_database.owner.connect() as db:
        events = (
            db.execute(
                text(
                    "SELECT correlation_id FROM audit.audit_event "
                    "WHERE target_id=:work_item_id "
                    "AND action='requirement.integration_delivery.mr_ready'"
                ),
                {"work_item_id": requested.work_item.id},
            )
            .scalars()
            .all()
        )
    assert events == ["source-control:effect:stable:mr-ready"]
    with pytest.raises(IdempotencyConflict):
        with isolated_requirement_database.runtime.begin() as db:
            record_integration_mr_ready(
                db,
                work_item_id=requested.work_item.id,
                binding_id=OTHER_BINDING_ID,
                expected_revision=requested.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:stable:mr-ready",
                correlation_id="source-control:effect:stable:mr-ready",
                dependencies=dependencies,
            )


def test_old_blocked_callback_cannot_regress_a_newer_mr_ready_result(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    ready = _ready_mr(isolated_requirement_database, key_suffix="newer-work-item")

    with pytest.raises(StaleWorkItemRevision):
        with isolated_requirement_database.runtime.begin() as db:
            record_integration_delivery_blocked(
                db,
                work_item_id=ready.work_item.id,
                binding_id=BINDING_ID,
                reason_code=IntegrationDeliveryBlockedReason.MR_CONFLICT,
                expected_revision=ready.work_item.revision - 1,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:old:block",
                correlation_id="source-control:effect:old:block",
                dependencies=_gate_dependencies(),
            )
    with isolated_requirement_database.runtime.connect() as db:
        current = get_integration_delivery_context(
            db,
            work_item_id=ready.work_item.id,
            dependencies=_gate_dependencies(),
        )

    assert current.integration_delivery_state is IntegrationDeliveryState.MR_OPEN
    assert current.integration_merge_request_binding_id == BINDING_ID


def test_reconciliation_pending_and_safe_blocked_retain_stable_binding(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_merge(isolated_requirement_database, key_suffix="pending-blocked")
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        pending = record_integration_reconciliation_pending(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:merge:pending",
            correlation_id="source-control:effect:merge:pending",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        blocked = record_integration_delivery_blocked(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            reason_code=IntegrationDeliveryBlockedReason.MR_CHECKS_BLOCKED,
            expected_revision=pending.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:merge:blocked",
            correlation_id="source-control:effect:merge:blocked",
            dependencies=dependencies,
        )

    assert pending.work_item.state is WorkItemState.VERIFYING
    assert (
        pending.work_item.integration_delivery_state
        is IntegrationDeliveryState.RECONCILIATION_PENDING
    )
    assert blocked.work_item.state is WorkItemState.VERIFYING
    assert blocked.work_item.integration_delivery_state is IntegrationDeliveryState.BLOCKED
    assert (
        blocked.work_item.integration_blocked_reason_code
        is IntegrationDeliveryBlockedReason.MR_CHECKS_BLOCKED
    )
    assert blocked.work_item.integration_merge_request_binding_id == BINDING_ID
    with isolated_requirement_database.owner.connect() as db:
        events = [
            (str(row.action), str(row.correlation_id))
            for row in db.execute(
                text(
                    "SELECT action, correlation_id FROM audit.audit_event "
                    "WHERE target_id=:work_item_id "
                    "AND action IN ('requirement.integration_delivery.blocked', "
                    "'requirement.integration_delivery.reconciliation_pending') "
                    "ORDER BY action"
                ),
                {"work_item_id": requested.work_item.id},
            ).all()
        ]
    assert events == [
        ("requirement.integration_delivery.blocked", "source-control:effect:merge:blocked"),
        (
            "requirement.integration_delivery.reconciliation_pending",
            "source-control:effect:merge:pending",
        ),
    ]


def test_blocked_callback_rejects_non_allowlisted_reason_at_runtime(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="unsafe-reason")

    with pytest.raises(InvalidRequirementInput):
        with isolated_requirement_database.runtime.begin() as db:
            record_integration_delivery_blocked(
                db,
                work_item_id=requested.work_item.id,
                binding_id=None,
                reason_code="provider body included credential",  # type: ignore[arg-type]
                expected_revision=requested.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:unsafe:blocked",
                correlation_id="source-control:effect:unsafe:blocked",
                dependencies=_gate_dependencies(),
            )


def test_merged_stays_verifying_and_replay_does_not_rollback_external_fact(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_merge(isolated_requirement_database, key_suffix="merged")
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        merged = record_integration_merged(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:merge:merged",
            correlation_id="source-control:effect:merge:merged",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = record_integration_merged(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:merge:merged",
            correlation_id="source-control:effect:merge:merged",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.connect() as db:
        current = get_integration_delivery_context(
            db,
            work_item_id=requested.work_item.id,
            dependencies=dependencies,
        )

    assert replay == merged
    assert merged.work_item.state is WorkItemState.VERIFYING
    assert merged.work_item.integration_delivery_state is IntegrationDeliveryState.INTEGRATED
    assert current.integration_delivery_state is IntegrationDeliveryState.INTEGRATED


def test_external_merge_drift_enters_blocked_without_provider_details(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_merge(isolated_requirement_database, key_suffix="external-drift")
    with isolated_requirement_database.runtime.begin() as db:
        result = record_external_merge_drift(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:external-drift",
            correlation_id="source-control:effect:external-drift",
            dependencies=_gate_dependencies(),
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = record_external_merge_drift(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:external-drift",
            correlation_id="source-control:effect:external-drift",
            dependencies=_gate_dependencies(),
        )

    assert replay == result
    assert result.requirement.state is RequirementState.VERIFYING
    assert result.requirement.revision == requested.requirement.revision
    assert replay.requirement.revision == result.requirement.revision
    assert result.work_item.revision == requested.work_item.revision + 1
    assert replay.work_item.revision == result.work_item.revision
    assert result.work_item.state is WorkItemState.VERIFYING
    assert result.work_item.integration_delivery_state is IntegrationDeliveryState.BLOCKED
    assert (
        result.work_item.integration_blocked_reason_code
        is IntegrationDeliveryBlockedReason.EXTERNAL_MERGE_DRIFT
    )
    assert result.work_item.integration_merge_request_binding_id == BINDING_ID
    with isolated_requirement_database.owner.connect() as db:
        events = [
            (str(row.action), str(row.correlation_id))
            for row in db.execute(
                text(
                    "SELECT action, correlation_id FROM audit.audit_event "
                    "WHERE target_id=:work_item_id "
                    "AND action='requirement.integration_delivery.external_merge_drift'"
                ),
                {"work_item_id": requested.work_item.id},
            ).all()
        ]
    assert events == [
        (
            "requirement.integration_delivery.external_merge_drift",
            "source-control:effect:external-drift",
        )
    ]


def test_mr_closed_from_mr_pending_installs_first_binding_once(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="create-closed")
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        closed = record_integration_delivery_blocked(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            reason_code=IntegrationDeliveryBlockedReason.MR_CLOSED,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:create:closed",
            correlation_id="source-control:effect:create:closed",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = record_integration_delivery_blocked(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            reason_code=IntegrationDeliveryBlockedReason.MR_CLOSED,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:create:closed",
            correlation_id="source-control:effect:create:closed",
            dependencies=dependencies,
        )

    assert replay == closed
    assert closed.requirement.state is RequirementState.IN_PROGRESS
    assert closed.requirement.revision == requested.requirement.revision
    assert replay.requirement.revision == closed.requirement.revision
    assert closed.work_item.revision == requested.work_item.revision + 1
    assert replay.work_item.revision == closed.work_item.revision
    assert closed.work_item.state is WorkItemState.IN_PROGRESS
    assert closed.work_item.integration_delivery_state is IntegrationDeliveryState.BLOCKED
    assert (
        closed.work_item.integration_blocked_reason_code
        is IntegrationDeliveryBlockedReason.MR_CLOSED
    )
    assert closed.work_item.integration_merge_request_binding_id == BINDING_ID
    with pytest.raises(WorkItemDeliveryConflict):
        with isolated_requirement_database.runtime.begin() as db:
            record_integration_delivery_blocked(
                db,
                work_item_id=requested.work_item.id,
                binding_id=OTHER_BINDING_ID,
                reason_code=IntegrationDeliveryBlockedReason.MR_CLOSED,
                expected_revision=closed.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:create:closed:other-binding",
                correlation_id="source-control:effect:create:closed:other-binding",
                dependencies=dependencies,
            )


def test_external_merge_drift_from_mr_pending_installs_first_binding_once(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="create-merged")
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        drift = record_external_merge_drift(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:create:external-drift",
            correlation_id="source-control:effect:create:external-drift",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = record_external_merge_drift(
            db,
            work_item_id=requested.work_item.id,
            binding_id=BINDING_ID,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:create:external-drift",
            correlation_id="source-control:effect:create:external-drift",
            dependencies=dependencies,
        )

    assert replay == drift
    assert drift.requirement.state is RequirementState.VERIFYING
    assert drift.requirement.revision == requested.requirement.revision + 1
    assert replay.requirement.revision == drift.requirement.revision
    assert drift.work_item.revision == requested.work_item.revision + 1
    assert replay.work_item.revision == drift.work_item.revision
    assert drift.work_item.state is WorkItemState.VERIFYING
    assert drift.work_item.integration_delivery_state is IntegrationDeliveryState.BLOCKED
    assert (
        drift.work_item.integration_blocked_reason_code
        is IntegrationDeliveryBlockedReason.EXTERNAL_MERGE_DRIFT
    )
    assert drift.work_item.integration_merge_request_binding_id == BINDING_ID
    with pytest.raises(WorkItemDeliveryConflict):
        with isolated_requirement_database.runtime.begin() as db:
            record_external_merge_drift(
                db,
                work_item_id=requested.work_item.id,
                binding_id=OTHER_BINDING_ID,
                expected_revision=drift.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:create:external-drift:other-binding",
                correlation_id="source-control:effect:create:external-drift:other-binding",
                dependencies=dependencies,
            )


def test_first_terminal_binding_rejects_non_mr_pending_delivery(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_mr(isolated_requirement_database, key_suffix="terminal-not-pending")
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        pending = record_integration_reconciliation_pending(
            db,
            work_item_id=requested.work_item.id,
            binding_id=None,
            expected_revision=requested.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:create:reconciliation-pending",
            correlation_id="source-control:effect:create:reconciliation-pending",
            dependencies=dependencies,
        )

    with pytest.raises(WorkItemDeliveryConflict):
        with isolated_requirement_database.runtime.begin() as db:
            record_integration_delivery_blocked(
                db,
                work_item_id=requested.work_item.id,
                binding_id=BINDING_ID,
                reason_code=IntegrationDeliveryBlockedReason.MR_CLOSED,
                expected_revision=pending.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:create:late-closed",
                correlation_id="source-control:effect:create:late-closed",
                dependencies=dependencies,
            )
    with pytest.raises(WorkItemDeliveryConflict):
        with isolated_requirement_database.runtime.begin() as db:
            record_external_merge_drift(
                db,
                work_item_id=requested.work_item.id,
                binding_id=BINDING_ID,
                expected_revision=pending.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:create:late-external-drift",
                correlation_id="source-control:effect:create:late-external-drift",
                dependencies=dependencies,
            )


def test_first_terminal_binding_rejects_old_revision_and_other_work_item(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    first = _requested_mr(isolated_requirement_database, key_suffix="terminal-first")
    second = _requested_mr(isolated_requirement_database, key_suffix="terminal-second")
    dependencies = _gate_dependencies()
    with pytest.raises(StaleWorkItemRevision):
        with isolated_requirement_database.runtime.begin() as db:
            record_external_merge_drift(
                db,
                work_item_id=first.work_item.id,
                binding_id=BINDING_ID,
                expected_revision=first.work_item.revision - 1,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:create:terminal-stale",
                correlation_id="source-control:effect:create:terminal-stale",
                dependencies=dependencies,
            )
    with isolated_requirement_database.runtime.begin() as db:
        record_integration_delivery_blocked(
            db,
            work_item_id=first.work_item.id,
            binding_id=BINDING_ID,
            reason_code=IntegrationDeliveryBlockedReason.MR_CLOSED,
            expected_revision=first.work_item.revision,
            actor=SYSTEM_ACTOR,
            idempotency_key="effect:create:terminal-work-item",
            correlation_id="source-control:effect:create:terminal-work-item",
            dependencies=dependencies,
        )
    with pytest.raises(IdempotencyConflict):
        with isolated_requirement_database.runtime.begin() as db:
            record_integration_delivery_blocked(
                db,
                work_item_id=second.work_item.id,
                binding_id=BINDING_ID,
                reason_code=IntegrationDeliveryBlockedReason.MR_CLOSED,
                expected_revision=second.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:create:terminal-work-item",
                correlation_id="source-control:effect:create:terminal-work-item",
                dependencies=dependencies,
            )


def test_callback_rejects_binding_mismatch_without_overwriting_projection(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requested = _requested_merge(isolated_requirement_database, key_suffix="binding-mismatch")
    with pytest.raises(WorkItemDeliveryConflict):
        with isolated_requirement_database.runtime.begin() as db:
            record_integration_merged(
                db,
                work_item_id=requested.work_item.id,
                binding_id=OTHER_BINDING_ID,
                expected_revision=requested.work_item.revision,
                actor=SYSTEM_ACTOR,
                idempotency_key="effect:merge:wrong-binding",
                correlation_id="source-control:effect:merge:wrong-binding",
                dependencies=_gate_dependencies(),
            )

    with isolated_requirement_database.runtime.connect() as db:
        current = get_integration_delivery_context(
            db,
            work_item_id=requested.work_item.id,
            dependencies=_gate_dependencies(),
        )
    assert current.integration_delivery_state is IntegrationDeliveryState.MERGE_PENDING
    assert current.integration_merge_request_binding_id == BINDING_ID
