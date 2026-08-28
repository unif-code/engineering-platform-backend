from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.app.modules.authorization import Scope
from control_plane.app.modules.identity import AccountStatus
from control_plane.app.modules.requirement.adapters import (
    ComposedGateReviewerGuard,
    WorkspaceOwnerGatePolicy,
)
from control_plane.app.modules.requirement.adapters import gates as gate_module

WORKSPACE_ID = "20000000-0000-0000-0000-000000000451"


class ReadOnlyEngine:
    @contextmanager
    def connect(self) -> Iterator[object]:
        yield object()


def test_workspace_owner_policy_returns_the_exact_code_owned_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_workspace(_db: object, **kwargs: Any) -> object:
        assert kwargs["workspace_id"] == WORKSPACE_ID
        return SimpleNamespace(owner_id="owner-1")

    monkeypatch.setattr(gate_module, "get_workspace", get_workspace)
    policy = WorkspaceOwnerGatePolicy(
        workspace_engine=ReadOnlyEngine(),  # type: ignore[arg-type]
        workspace_dependencies=object(),  # type: ignore[arg-type]
    ).requirement_baseline(workspace_id=WORKSPACE_ID)

    assert policy.model_dump(mode="json") == {
        "version": 1,
        "default_reviewer_id": "owner-1",
        "policy_code": "REQUIREMENT_BASELINE_WORKSPACE_OWNER",
        "snapshot_hash": (
            "sha256:bdfadcc2d2c32fdb9fdf327d45a231cd2e5cb9bf3028f4e09d527fdb50dd8ea2"
        ),
    }


@pytest.mark.parametrize(
    ("status", "formal_member", "granted", "expected"),
    [
        (AccountStatus.ENABLED, True, True, True),
        (AccountStatus.DISABLED, True, True, False),
        (AccountStatus.ENABLED, False, True, False),
        (AccountStatus.ENABLED, True, False, False),
    ],
)
def test_reviewer_guard_rechecks_identity_membership_and_workspace_capability(
    monkeypatch: pytest.MonkeyPatch,
    status: AccountStatus,
    formal_member: bool,
    granted: bool,
    expected: bool,
) -> None:
    def get_account(_db: object, **kwargs: Any) -> object:
        assert kwargs["account_id"] == "reviewer-1"
        return SimpleNamespace(status=status)

    def is_formal_member(_db: object, **kwargs: Any) -> bool:
        assert kwargs == {
            "workspace_id": WORKSPACE_ID,
            "account_id": "reviewer-1",
            "dependencies": kwargs["dependencies"],
        }
        return formal_member

    def effective_grants(_db: object, **kwargs: Any) -> list[object]:
        assert kwargs["principal_id"] == "reviewer-1"
        assert kwargs["capability"] == "requirement.baseline.decide"
        assert kwargs["scope"] == Scope.workspace(WORKSPACE_ID)
        return [object()] if granted else []

    monkeypatch.setattr(gate_module, "get_account", get_account)
    monkeypatch.setattr(gate_module, "is_formal_member", is_formal_member)
    monkeypatch.setattr(gate_module, "effective_grants", effective_grants)
    guard = ComposedGateReviewerGuard(
        identity_engine=ReadOnlyEngine(),  # type: ignore[arg-type]
        identity_dependencies=object(),  # type: ignore[arg-type]
        workspace_engine=ReadOnlyEngine(),  # type: ignore[arg-type]
        workspace_dependencies=object(),  # type: ignore[arg-type]
        authorization_engine=ReadOnlyEngine(),  # type: ignore[arg-type]
        authorization_dependencies=object(),  # type: ignore[arg-type]
    )

    assert guard.can_decide(actor_id="reviewer-1", workspace_id=WORKSPACE_ID) is expected


@pytest.mark.parametrize("stage", ["identity", "workspace", "authorization"])
def test_reviewer_guard_preserves_dependency_failures_for_fail_closed_503(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    def get_account(_db: object, **_kwargs: Any) -> object:
        if stage == "identity":
            raise RuntimeError("identity unavailable")
        return SimpleNamespace(status=AccountStatus.ENABLED)

    def is_formal_member(_db: object, **_kwargs: Any) -> bool:
        if stage == "workspace":
            raise RuntimeError("workspace unavailable")
        return True

    def effective_grants(_db: object, **_kwargs: Any) -> list[object]:
        if stage == "authorization":
            raise RuntimeError("authorization unavailable")
        return [object()]

    monkeypatch.setattr(gate_module, "get_account", get_account)
    monkeypatch.setattr(gate_module, "is_formal_member", is_formal_member)
    monkeypatch.setattr(gate_module, "effective_grants", effective_grants)
    guard = ComposedGateReviewerGuard(
        identity_engine=ReadOnlyEngine(),  # type: ignore[arg-type]
        identity_dependencies=object(),  # type: ignore[arg-type]
        workspace_engine=ReadOnlyEngine(),  # type: ignore[arg-type]
        workspace_dependencies=object(),  # type: ignore[arg-type]
        authorization_engine=ReadOnlyEngine(),  # type: ignore[arg-type]
        authorization_dependencies=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match=f"{stage} unavailable"):
        guard.can_decide(actor_id="reviewer-1", workspace_id=WORKSPACE_ID)
