import pytest
from sqlalchemy import Engine

from control_plane.app.modules.authorization import (
    SecurityChangeOrchestrator,
    bump_version,
    clear_fence,
    mark_fence,
    principal_version,
)
from tests.authorization.helpers import authorization_dependencies

pytestmark = pytest.mark.integration


def test_fence_generation_prevents_stale_completion_aba(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        first = mark_fence(
            db,
            account_ids=["account-1"],
            reason="organization change",
            dependencies=deps,
        )["account-1"]
    with authorization_rw_engine.begin() as db:
        second = mark_fence(
            db,
            account_ids=["account-1"],
            reason="newer workspace change",
            dependencies=deps,
        )["account-1"]
    assert second > first

    with authorization_rw_engine.begin() as db:
        assert (
            clear_fence(
                db,
                generations={"account-1": first},
                dependencies=deps,
            )
            == set()
        )
    with authorization_rw_engine.connect() as db:
        state = principal_version(db, account_id="account-1", dependencies=deps)
    assert state is not None
    assert state.dirty_generation == second

    with authorization_rw_engine.begin() as db:
        assert clear_fence(
            db,
            generations={"account-1": second},
            dependencies=deps,
        ) == {"account-1"}
    with authorization_rw_engine.connect() as db:
        state = principal_version(db, account_id="account-1", dependencies=deps)
    assert state is not None
    assert state.dirty_generation is None


def test_post_commit_projection_failure_leaves_dirty_until_same_ticket_retry(
    clean_authorization_db: None,
    authorization_rw_engine: Engine,
) -> None:
    deps = authorization_dependencies()
    with authorization_rw_engine.begin() as db:
        initial = bump_version(db, account_id="account-1", dependencies=deps)

    attempts: list[tuple[str, ...]] = []

    def failing_projection(account_ids: tuple[str, ...]) -> None:
        attempts.append(account_ids)
        raise RuntimeError("projection unavailable")

    failing = SecurityChangeOrchestrator(
        authorization_rw_engine,
        deps,
        recompute_membership=failing_projection,
    )
    ticket = failing.begin(reason="committed organization source change")
    with pytest.raises(RuntimeError, match="projection unavailable"):
        failing.complete(
            ticket,
            affected_account_ids=("account-1",),
            recompute_membership=True,
        )

    with authorization_rw_engine.connect() as db:
        dirty = principal_version(db, account_id="account-1", dependencies=deps)
    assert dirty is not None
    assert dirty.version == initial.version
    assert dirty.dirty_generation == ticket.generations["account-1"]

    retry = SecurityChangeOrchestrator(
        authorization_rw_engine,
        deps,
        recompute_membership=lambda account_ids: attempts.append(account_ids),
    )
    assert retry.complete(
        ticket,
        affected_account_ids=("account-1",),
        recompute_membership=True,
    ) == {"account-1"}
    with authorization_rw_engine.connect() as db:
        converged = principal_version(db, account_id="account-1", dependencies=deps)
    assert converged is not None
    assert converged.version == initial.version + 1
    assert converged.dirty_generation is None
    assert attempts == [("account-1",), ("account-1",)]
