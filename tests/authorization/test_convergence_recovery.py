from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.authorization import (
    SecurityChangeOrchestrator,
    SecurityChangeSource,
    bump_version,
    principal_version,
)
from tests.authorization.helpers import authorization_dependencies

pytestmark = pytest.mark.integration


def test_committed_source_work_survives_lost_ticket_and_retries_from_database(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)

    source = SecurityChangeSource(
        module="organization",
        actor="admin-1",
        operation="org_set_superior",
        idempotency_key="org-durable-recovery-001",
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
    assert first_retry.reconcile_for_account("account-1") is False

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
    assert recovered.reconcile_for_account("account-1") is True

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


def test_older_work_cannot_clear_or_bump_a_newer_generation(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)
    orchestrator = SecurityChangeOrchestrator(authorization_rw_engine, deps)
    older = orchestrator.begin(
        source=SecurityChangeSource("organization", "admin", "set", "older-key"),
        reason="older source",
        account_ids=("account-1",),
    )
    newer = orchestrator.begin(
        source=SecurityChangeSource("workspace", "admin", "invite", "newer-key"),
        reason="newer source",
        account_ids=("account-1",),
    )
    assert newer.generations["account-1"] > older.generations["account-1"]

    assert orchestrator.complete(older) == set()
    with authorization_rw_engine.connect() as db:
        still_dirty = principal_version(db, account_id="account-1", dependencies=deps)
    assert still_dirty is not None
    assert still_dirty.version == initial.version
    assert still_dirty.dirty_generation == newer.generations["account-1"]

    assert orchestrator.complete(newer) == {"account-1"}
    with authorization_rw_engine.connect() as db:
        converged = principal_version(db, account_id="account-1", dependencies=deps)
    assert converged is not None
    assert converged.version == initial.version + 1
    assert converged.dirty_generation is None


@pytest.mark.parametrize("commit_source", [True, False])
def test_identity_fence_stays_dirty_while_source_transaction_is_in_progress(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
    authorization_identity_engine: Engine,
    commit_source: bool,
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

    # Committed and rolled-back source transactions both recover from current truth.
    assert orchestrator.reconcile_for_account("account-1") is True
    with authorization_rw_engine.connect() as db:
        after = principal_version(db, account_id="account-1", dependencies=deps)
    assert after is not None
    assert after.version == initial.version + 1
    assert after.dirty_generation is None
    assert recomputes == [("account-1",)]
