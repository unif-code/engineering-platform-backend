from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import datetime
from threading import Event

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.authorization import (
    SecurityChangeOrchestrator,
    SecurityChangeSource,
    bump_version,
    principal_version,
)
from control_plane.app.modules.authorization.adapters import (
    SqlAlchemyAuthorizationRepository,
)
from tests.authorization.helpers import authorization_dependencies

pytestmark = pytest.mark.integration


def _committed_xid(engine: Engine) -> str:
    with engine.begin() as db:
        return str(db.execute(text("SELECT pg_current_xact_id()")).scalar_one())


def test_committed_source_work_survives_lost_ticket_and_retries_from_database(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)
        source_xid = str(db.execute(text("SELECT pg_current_xact_id()")).scalar_one())

    source = SecurityChangeSource(
        module="organization",
        actor="admin-1",
        operation="org_set_superior",
        idempotency_key="org-durable-recovery-001",
        source_transaction_id=source_xid,
    )
    registration = SecurityChangeOrchestrator(
        authorization_rw_engine,
        deps,
    )
    ticket = registration.begin(
        source=source,
        reason="organization structure change",
        affected_account_ids=("account-1",),
        recompute_membership=True,
    )

    # The source transaction committed, but the process lost the Python ticket.
    # A fresh orchestrator must reconstruct only the persisted work item.
    attempts: list[tuple[str, ...]] = []

    def transient_failure(account_ids: tuple[str, ...]) -> None:
        attempts.append(account_ids)
        raise RuntimeError("injected projection failure")

    first_retry = SecurityChangeOrchestrator(
        authorization_rw_engine,
        replace(deps),
        recompute_membership=transient_failure,
    )
    assert first_retry.reconcile_pending() is False

    with authorization_rw_engine.connect() as db:
        dirty = principal_version(db, account_id="account-1", dependencies=deps)
        persisted = (
            db.execute(
                text(
                    "SELECT status, generation_map, affected_account_ids "
                    'FROM "authorization".convergence_work WHERE id=:id'
                ),
                {"id": ticket.id},
            )
            .mappings()
            .one()
        )
    assert dirty is not None
    assert dirty.version == initial.version
    assert dirty.dirty_generation == ticket.generations["account-1"]
    assert persisted["status"] == "PENDING"
    assert persisted["generation_map"] == {"account-1": ticket.generations["account-1"]}
    assert persisted["affected_account_ids"] == ["account-1"]

    recovered = SecurityChangeOrchestrator(
        authorization_rw_engine,
        replace(deps),
        recompute_membership=lambda account_ids: attempts.append(account_ids),
    )
    assert recovered.reconcile_pending() is True

    with authorization_rw_engine.connect() as db:
        converged = principal_version(db, account_id="account-1", dependencies=deps)
        status = db.execute(
            text('SELECT status FROM "authorization".convergence_work WHERE id=:id'),
            {"id": ticket.id},
        ).scalar_one()
    assert converged is not None
    assert converged.version == initial.version + 1
    assert converged.dirty_generation is None
    assert status == "COMPLETED"
    assert attempts == [("account-1",), ("account-1",)]


def test_completed_source_replay_does_not_open_a_new_generation(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        bump_version(db, account_id="account-1", dependencies=deps)
    source = SecurityChangeSource(
        module="workspace",
        actor="owner-1",
        operation="workspace_invite_leader",
        idempotency_key="workspace-replay-convergence-001",
        source_transaction_id=_committed_xid(authorization_rw_engine),
    )
    recomputes: list[tuple[str, ...]] = []
    orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        deps,
        recompute_membership=lambda account_ids: recomputes.append(account_ids),
    )
    ticket = orchestrator.begin(
        source=source,
        reason="workspace leader change",
        account_ids=("account-1",),
        affected_account_ids=("account-1",),
        recompute_membership=True,
    )
    assert orchestrator.complete(ticket) == {"account-1"}
    with authorization_rw_engine.connect() as db:
        first = db.execute(
            text(
                "SELECT version, fence_generation, dirty_generation "
                "FROM \"authorization\".principal_version WHERE account_id='account-1'"
            )
        ).one()

    replay = orchestrator.begin(
        source=source,
        reason="workspace leader change",
        account_ids=("account-1",),
        affected_account_ids=("account-1",),
        recompute_membership=True,
    )
    assert replay.id == ticket.id
    assert replay.completed is True
    assert orchestrator.complete(replay) == set()
    with authorization_rw_engine.connect() as db:
        after = db.execute(
            text(
                "SELECT version, fence_generation, dirty_generation "
                "FROM \"authorization\".principal_version WHERE account_id='account-1'"
            )
        ).one()
        work_count = db.execute(
            text(
                'SELECT count(*) FROM "authorization".convergence_work '
                "WHERE idempotency_key='workspace-replay-convergence-001'"
            )
        ).scalar_one()
    assert after == first
    assert recomputes == [("account-1",)]
    assert work_count == 1


def test_concurrent_reconcilers_run_external_projection_once(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)
    entered = Event()
    release = Event()
    recomputes: list[tuple[str, ...]] = []

    def blocking_recompute(account_ids: tuple[str, ...]) -> None:
        recomputes.append(account_ids)
        entered.set()
        assert release.wait(timeout=5)

    first_orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        deps,
        recompute_membership=blocking_recompute,
    )
    ticket = first_orchestrator.begin(
        source=SecurityChangeSource(
            module="organization",
            actor="admin-1",
            operation="org_set_superior",
            idempotency_key="org-concurrent-reconcile-001",
            source_transaction_id=_committed_xid(authorization_rw_engine),
        ),
        reason="organization structure change",
        account_ids=("account-1",),
        affected_account_ids=("account-1",),
        recompute_membership=True,
    )
    second_orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        deps,
        recompute_membership=blocking_recompute,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_orchestrator.complete, ticket)
        assert entered.wait(timeout=3)
        second = pool.submit(second_orchestrator.reconcile_for_account, "account-1")
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.3)
        finally:
            release.set()
        assert first.result(timeout=5) == {"account-1"}
        assert second.result(timeout=5) is True

    with authorization_rw_engine.connect() as db:
        converged = principal_version(db, account_id="account-1", dependencies=deps)
    assert converged is not None
    assert converged.version == initial.version + 1
    assert converged.dirty_generation is None
    assert recomputes == [("account-1",)]


def test_older_committed_work_bumps_once_without_clearing_a_newer_generation(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)
    orchestrator = SecurityChangeOrchestrator(authorization_rw_engine, deps)
    older = orchestrator.begin(
        source=SecurityChangeSource(
            "organization",
            "admin",
            "set",
            "older-key",
            source_transaction_id=_committed_xid(authorization_rw_engine),
        ),
        reason="older source",
        account_ids=("account-1",),
    )
    newer = orchestrator.begin(
        source=SecurityChangeSource(
            "workspace",
            "admin",
            "invite",
            "newer-key",
            source_transaction_id=_committed_xid(authorization_rw_engine),
        ),
        reason="newer source",
        account_ids=("account-1",),
    )
    assert newer.generations["account-1"] > older.generations["account-1"]

    assert orchestrator.complete(older) == {"account-1"}
    with authorization_rw_engine.connect() as db:
        still_dirty = principal_version(db, account_id="account-1", dependencies=deps)
    assert still_dirty is not None
    assert still_dirty.version == initial.version + 1
    assert still_dirty.dirty_generation == newer.generations["account-1"]

    assert orchestrator.complete(newer) == {"account-1"}
    with authorization_rw_engine.connect() as db:
        converged = principal_version(db, account_id="account-1", dependencies=deps)
    assert converged is not None
    assert converged.version == initial.version + 2
    assert converged.dirty_generation is None


def test_newer_committed_work_bumps_once_but_keeps_older_in_progress_work_dirty(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
) -> None:
    """Completing B/gen2 must not erase the still-pending A/gen1 fence."""
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)
    attempts: list[tuple[str, ...]] = []

    def fail_first_projection(account_ids: tuple[str, ...]) -> None:
        attempts.append(account_ids)
        if len(attempts) == 1:
            raise RuntimeError("injected source B projection failure")

    orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        deps,
        recompute_membership=fail_first_projection,
    )

    source_a = authorization_identity_engine.connect()
    transaction_a = source_a.begin()
    source_b = authorization_identity_engine.connect()
    transaction_b = source_b.begin()
    try:
        xid_a = str(source_a.execute(text("SELECT pg_current_xact_id()")).scalar_one())
        older = orchestrator.begin(
            source=SecurityChangeSource(
                "identity",
                "account-1",
                "source-a",
                "two-source-a",
                source_transaction_id=xid_a,
            ),
            reason="source A remains in progress",
            account_ids=("account-1",),
        )
        xid_b = str(source_b.execute(text("SELECT pg_current_xact_id()")).scalar_one())
        newer = orchestrator.begin(
            source=SecurityChangeSource(
                "identity",
                "account-1",
                "source-b",
                "two-source-b",
                source_transaction_id=xid_b,
            ),
            reason="source B commits first",
            account_ids=("account-1",),
            affected_account_ids=("account-1",),
            recompute_membership=True,
        )
        transaction_b.commit()

        with pytest.raises(RuntimeError, match="source B projection failure"):
            orchestrator.complete(newer)
        with authorization_rw_engine.connect() as db:
            after_failed_b = principal_version(db, account_id="account-1", dependencies=deps)
        assert after_failed_b is not None
        assert after_failed_b.version == initial.version
        assert after_failed_b.dirty_generation == newer.generations["account-1"]

        retry = SecurityChangeOrchestrator(
            authorization_rw_engine,
            replace(deps),
            recompute_membership=fail_first_projection,
        )
        assert retry.complete(newer) == {"account-1"}
        with authorization_rw_engine.connect() as db:
            after_b = principal_version(db, account_id="account-1", dependencies=deps)
        assert after_b is not None
        assert after_b.version == initial.version + 1
        assert after_b.dirty_generation == older.generations["account-1"]

        transaction_a.commit()
        assert orchestrator.complete(older) == {"account-1"}
    finally:
        if transaction_b.is_active:
            transaction_b.rollback()
        if transaction_a.is_active:
            transaction_a.rollback()
        source_b.close()
        source_a.close()

    with authorization_rw_engine.connect() as db:
        converged = principal_version(db, account_id="account-1", dependencies=deps)
    assert converged is not None
    assert converged.version == initial.version + 2
    assert converged.dirty_generation is None
    assert attempts == [("account-1",), ("account-1",)]


def test_new_registration_waits_for_older_reconciler_principal_lock(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    entered = Event()
    release = Event()

    class BlockingSettlementRepository(SqlAlchemyAuthorizationRepository):
        def settle_pending_principal(
            self,
            work_id: str,
            account_id: str,
            *,
            bump_version: bool,
            now: datetime,
        ) -> object:
            result = super().settle_pending_principal(
                work_id,
                account_id,
                bump_version=bump_version,
                now=now,
            )
            entered.set()
            assert release.wait(timeout=5)
            return result

    base_dependencies = authorization_dependencies()
    blocking_dependencies = replace(
        base_dependencies,
        repository_factory=BlockingSettlementRepository,
    )
    old = SecurityChangeOrchestrator(
        authorization_rw_engine,
        blocking_dependencies,
    ).begin(
        source=SecurityChangeSource(
            "organization",
            "admin",
            "older-lock-holder",
            "older-lock-key",
            source_transaction_id=_committed_xid(authorization_rw_engine),
        ),
        reason="older lock holder",
        account_ids=("account-1",),
    )
    completing = SecurityChangeOrchestrator(
        authorization_rw_engine,
        blocking_dependencies,
    )
    registering = SecurityChangeOrchestrator(
        authorization_rw_engine,
        base_dependencies,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(completing.complete, old)
        assert entered.wait(timeout=3)
        registration = pool.submit(
            registering.begin,
            source=SecurityChangeSource(
                "workspace",
                "admin",
                "newer-registration",
                "newer-registration-key",
                source_transaction_id=_committed_xid(authorization_rw_engine),
            ),
            reason="newer registration",
            account_ids=("account-1",),
        )
        try:
            with pytest.raises(FutureTimeout):
                registration.result(timeout=0.3)
        finally:
            release.set()
        assert completion.result(timeout=5) == {"account-1"}
        newer = registration.result(timeout=5)

    with authorization_rw_engine.connect() as db:
        state = principal_version(db, account_id="account-1", dependencies=base_dependencies)
    assert state is not None
    assert state.version == 2
    assert state.dirty_generation == newer.generations["account-1"]
    assert newer.generations["account-1"] > old.generations["account-1"]


def test_later_source_claim_after_aborted_attempt_owns_a_new_work_item(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)
    orchestrator = SecurityChangeOrchestrator(authorization_rw_engine, deps)

    first_connection = authorization_identity_engine.connect()
    first_transaction = first_connection.begin()
    try:
        first_xid = str(first_connection.execute(text("SELECT pg_current_xact_id()")).scalar_one())
        first = orchestrator.begin(
            source=SecurityChangeSource(
                "organization",
                "admin-1",
                "org_set_superior",
                "retry-after-abort-key",
                source_transaction_id=first_xid,
                request_fingerprint="fingerprint-first",
                idempotency_claim_id="00000000-0000-0000-0000-000000000921",
            ),
            reason="first source aborts",
            account_ids=("account-1",),
        )
        first_transaction.rollback()
    finally:
        if first_transaction.is_active:
            first_transaction.rollback()
        first_connection.close()
    assert orchestrator.reconcile_for_account("account-1") is True

    second_connection = authorization_identity_engine.connect()
    second_transaction = second_connection.begin()
    try:
        second_xid = str(
            second_connection.execute(text("SELECT pg_current_xact_id()")).scalar_one()
        )
        second = orchestrator.begin(
            source=SecurityChangeSource(
                "organization",
                "admin-1",
                "org_set_superior",
                "retry-after-abort-key",
                source_transaction_id=second_xid,
                request_fingerprint="fingerprint-second",
                idempotency_claim_id="00000000-0000-0000-0000-000000000922",
            ),
            reason="second source owns the new claim",
            account_ids=("account-1",),
        )
        second_transaction.commit()
    finally:
        if second_transaction.is_active:
            second_transaction.rollback()
        second_connection.close()

    assert first.created is True
    assert second.created is True
    assert second.id != first.id
    assert second.generations["account-1"] > first.generations["account-1"]
    assert orchestrator.complete(second) == {"account-1"}
    with authorization_rw_engine.connect() as db:
        final = principal_version(db, account_id="account-1", dependencies=deps)
        status_rows = db.execute(
            text(
                "SELECT idempotency_claim_id::text, status "
                'FROM "authorization".convergence_work '
                "WHERE idempotency_key='retry-after-abort-key'"
            )
        ).all()
        statuses: dict[str, str] = {str(claim_id): str(status) for claim_id, status in status_rows}
    assert final is not None
    assert final.version == initial.version + 1
    assert final.dirty_generation is None
    assert statuses == {
        "00000000-0000-0000-0000-000000000921": "CANCELLED",
        "00000000-0000-0000-0000-000000000922": "COMPLETED",
    }


@pytest.mark.parametrize(
    ("commit_source", "expected_status", "expected_bump", "expected_recomputes"),
    [
        (True, "COMPLETED", 1, [("account-1",)]),
        (False, "CANCELLED", 0, []),
    ],
)
def test_identity_fence_stays_dirty_while_source_transaction_is_in_progress(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    commit_source: bool,
    expected_status: str,
    expected_bump: int,
    expected_recomputes: list[tuple[str, ...]],
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)
    recomputes: list[tuple[str, ...]] = []
    orchestrator = SecurityChangeOrchestrator(
        authorization_rw_engine,
        deps,
        recompute_membership=lambda account_ids: recomputes.append(account_ids),
    )

    source_connection = authorization_identity_engine.connect()
    source_transaction = source_connection.begin()
    try:
        source_xid = str(
            source_connection.execute(text("SELECT pg_current_xact_id()")).scalar_one()
        )
        orchestrator.identity_change(
            "account-1",
            actor="identity-admin",
            operation="identity_status_change",
            idempotency_key=f"identity-ordering-{commit_source}",
            source_transaction_id=source_xid,
        )

        # A concurrent request must not reconcile uncommitted source truth.
        assert orchestrator.reconcile_for_account("account-1") is False
        with authorization_rw_engine.connect() as db:
            during = principal_version(db, account_id="account-1", dependencies=deps)
        assert during is not None
        assert during.version == initial.version
        assert during.dirty_generation is not None
        assert recomputes == []

        if commit_source:
            source_transaction.commit()
        else:
            source_transaction.rollback()
    finally:
        if source_transaction.is_active:
            source_transaction.rollback()
        source_connection.close()

    # A committed source converges. An aborted source is cancelled without recompute/bump.
    assert orchestrator.reconcile_for_account("account-1") is True
    with authorization_rw_engine.connect() as db:
        after = principal_version(db, account_id="account-1", dependencies=deps)
        status = db.execute(
            text(
                'SELECT status FROM "authorization".convergence_work '
                "WHERE idempotency_key=:idempotency_key"
            ),
            {"idempotency_key": f"identity-ordering-{commit_source}"},
        ).scalar_one()
    assert after is not None
    assert after.version == initial.version + expected_bump
    assert after.dirty_generation is None
    assert status == expected_status
    assert recomputes == expected_recomputes


def test_unknown_source_transaction_stays_pending_and_dirty(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    orchestrator = SecurityChangeOrchestrator(authorization_rw_engine, deps)
    ticket = orchestrator.begin(
        source=SecurityChangeSource(
            "identity",
            "account-1",
            "unknown-source",
            "unknown-source-key",
        ),
        reason="source xid unavailable",
        account_ids=("account-1",),
    )

    assert orchestrator.reconcile_pending() is False

    with authorization_rw_engine.connect() as db:
        state = principal_version(db, account_id="account-1", dependencies=deps)
        status = db.execute(
            text('SELECT status FROM "authorization".convergence_work WHERE id=:id'),
            {"id": ticket.id},
        ).scalar_one()
    assert state is not None
    assert state.version == 1
    assert state.dirty_generation == ticket.generations["account-1"]
    assert status == "PENDING"
