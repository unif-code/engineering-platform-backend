from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

import control_plane.app.modules.authorization as authorization

NOW = datetime(2026, 9, 4, tzinfo=UTC)


@pytest.mark.parametrize(
    "denial",
    [
        None,
        "missing_grant",
        "disabled",
        "uninitialized",
        "removed_member",
        "dirty",
        "version_drift",
        "expired",
        "account_drift",
        "membership_drift",
        "fence_drift",
        "missing_principal",
        "wrong_scope",
        "not_started",
    ],
)
def test_current_qualification_is_fail_closed_and_checks_final_grant_validity(
    denial: str | None,
) -> None:
    runtime_type = getattr(authorization, "ActorQualificationRuntime", None)
    assert runtime_type is not None, "Authorization must own a shared actor qualification runtime"
    from control_plane.app.modules.authorization.application.qualification import evaluate
    from control_plane.app.modules.authorization.domain.qualification import (
        ActorAccountFacts,
        ActorWorkspaceFacts,
    )

    calls = 0

    class Repository:
        def principal_version(self, account_id: str) -> dict[str, Any] | None:
            nonlocal calls
            calls += 1
            if denial == "missing_principal":
                return None
            return dict(
                account_id=account_id,
                version=4 + (denial == "version_drift" and calls > 1),
                fence_generation=7 + (denial == "fence_drift" and calls > 1),
                dirty_generation=7 if denial == "dirty" else None,
                dirty_reason="membership" if denial == "dirty" else None,
                updated_at=NOW,
            )

        def effective_grants(
            self, principal_id: str, capability: str, scope_type: str, scope_id: str, now: datetime
        ) -> list[dict[str, Any]]:
            if denial == "missing_grant" and capability == "code.change":
                return []
            return [
                dict(
                    id=capability,
                    principal_id=principal_id,
                    capability=capability,
                    scope_type="WORKSPACE",
                    scope_id="other" if denial == "wrong_scope" else "workspace",
                    source="MANUAL",
                    valid_from=NOW + timedelta(milliseconds=500)
                    if denial == "not_started"
                    else NOW - timedelta(days=1),
                    valid_to=NOW + timedelta(seconds=1),
                    status="ACTIVE",
                    version=3,
                    created_at=NOW,
                    updated_at=NOW,
                )
            ]

    account = ActorAccountFacts(
        account_id="human",
        version=5,
        status="DISABLED" if denial == "disabled" else "ENABLED",
        initialized=denial != "uninitialized",
    )
    membership = ActorWorkspaceFacts(
        workspace_id="workspace",
        version=12,
        member_source=None if denial == "removed_member" else "OWNER",
        member_computed_at=NOW,
        archived=False,
    )
    tick = 0

    def now() -> datetime:
        nonlocal tick
        tick += 1
        return NOW + timedelta(seconds=1) if denial == "expired" and tick > 2 else NOW

    dependencies = SimpleNamespace(clock=SimpleNamespace(now=now))
    reads = {"account": 0, "membership": 0}

    def read_account(_: str) -> ActorAccountFacts:
        reads["account"] += 1
        return (
            account.model_copy(update={"version": 6})
            if denial == "account_drift" and reads["account"] > 1
            else account
        )

    def read_membership(*_: str) -> ActorWorkspaceFacts:
        reads["membership"] += 1
        return (
            membership.model_copy(update={"version": 13})
            if denial == "membership_drift" and reads["membership"] > 1
            else membership
        )

    facts = SimpleNamespace(account=read_account, membership=read_membership)
    result = evaluate(
        cast(Any, Repository()),
        actor_id="human",
        workspace_id="workspace",
        required_capabilities=("merge_request.review", "code.change"),
        facts=facts,
        dependencies=cast(Any, dependencies),
    )
    assert result.eligible is (denial is None)
    assert result.account is not None and result.workspace is not None
    assert result.account.version == 5
    assert result.workspace.version == 12
    if denial != "missing_principal":
        assert result.principal is not None and result.principal.version >= 4
    assert result.required_capabilities == ("merge_request.review", "code.change")
    assert result.snapshot_hash.startswith("sha256:")
