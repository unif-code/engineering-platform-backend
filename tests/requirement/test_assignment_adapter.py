from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.app.modules.authorization import Scope
from control_plane.app.modules.identity import AccountStatus
from control_plane.app.modules.requirement.adapters import (
    ComposedAutomaticAssignmentGuard,
)
from control_plane.app.modules.requirement.adapters import assignment as assignment_module

ACTOR_ID = "employee-1"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000401"
REPOSITORY_ID = "10000000-0000-0000-0000-000000000401"
CAPABILITIES = ("code.change", "artifact.write")


@dataclass(frozen=True, slots=True)
class FacadeState:
    account_status: AccountStatus = AccountStatus.ENABLED
    formal_member: bool = True
    granted_capabilities: frozenset[str] = frozenset(CAPABILITIES)
    authorized_repository_ids: frozenset[str] = frozenset({REPOSITORY_ID})
    raise_at: str | None = None


class ReadOnlyEngine:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    @contextmanager
    def connect(self) -> Iterator[object]:
        self.calls.append(self.name)
        yield object()


class GitLabMustNotBeCalled:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"GitLab must not be used by Assignment Guard: {name}")


def _guard(
    monkeypatch: pytest.MonkeyPatch,
    state: FacadeState,
) -> tuple[ComposedAutomaticAssignmentGuard, list[str]]:
    calls: list[str] = []

    def fail_if(stage: str) -> None:
        if state.raise_at == stage:
            raise RuntimeError(f"{stage} unavailable")

    def get_account(_db: object, **kwargs: Any) -> object:
        assert kwargs["account_id"] == ACTOR_ID
        fail_if("identity")
        return SimpleNamespace(status=state.account_status)

    def is_formal_member(_db: object, **kwargs: Any) -> bool:
        assert kwargs["workspace_id"] == WORKSPACE_ID
        assert kwargs["account_id"] == ACTOR_ID
        fail_if("workspace")
        return state.formal_member

    def effective_grants(_db: object, **kwargs: Any) -> list[object]:
        assert kwargs["principal_id"] == ACTOR_ID
        assert kwargs["scope"] == Scope.workspace(WORKSPACE_ID)
        fail_if("authorization")
        return [object()] if kwargs["capability"] in state.granted_capabilities else []

    def list_authorized_repositories(_db: object, **kwargs: Any) -> tuple[object, ...]:
        assert kwargs["workspace_id"] == WORKSPACE_ID
        fail_if("source_control")
        return tuple(
            SimpleNamespace(repository_id=repository_id)
            for repository_id in state.authorized_repository_ids
        )

    monkeypatch.setattr(assignment_module, "get_account", get_account)
    monkeypatch.setattr(assignment_module, "is_formal_member", is_formal_member)
    monkeypatch.setattr(assignment_module, "effective_grants", effective_grants)
    monkeypatch.setattr(
        assignment_module,
        "list_authorized_repositories",
        list_authorized_repositories,
    )

    guard = ComposedAutomaticAssignmentGuard(
        identity_engine=ReadOnlyEngine("identity", calls),  # type: ignore[arg-type]
        identity_dependencies=object(),  # type: ignore[arg-type]
        workspace_engine=ReadOnlyEngine("workspace", calls),  # type: ignore[arg-type]
        workspace_dependencies=object(),  # type: ignore[arg-type]
        authorization_engine=ReadOnlyEngine("authorization", calls),  # type: ignore[arg-type]
        authorization_dependencies=object(),  # type: ignore[arg-type]
        source_control_engine=ReadOnlyEngine("source_control", calls),  # type: ignore[arg-type]
        source_control_dependencies=SimpleNamespace(gitlab=GitLabMustNotBeCalled()),  # type: ignore[arg-type]
    )
    return guard, calls


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (FacadeState(), True),
        (FacadeState(account_status=AccountStatus.DISABLED), False),
        (FacadeState(formal_member=False), False),
        (FacadeState(granted_capabilities=frozenset({"code.change"})), False),
        (FacadeState(authorized_repository_ids=frozenset()), False),
        (FacadeState(authorized_repository_ids=frozenset({"another-repository"})), False),
    ],
)
def test_composed_assignment_guard_enforces_the_exact_eligibility_matrix(
    monkeypatch: pytest.MonkeyPatch,
    state: FacadeState,
    expected: bool,
) -> None:
    guard, calls = _guard(monkeypatch, state)

    allowed = guard.can_auto_assign(
        actor_id=ACTOR_ID,
        workspace_id=WORKSPACE_ID,
        repository_id=REPOSITORY_ID,
        required_capabilities=CAPABILITIES,
    )

    assert allowed is expected
    if expected:
        assert calls == ["identity", "workspace", "authorization", "source_control"]


@pytest.mark.parametrize(
    "stage",
    ["identity", "workspace", "authorization", "source_control"],
)
def test_composed_assignment_guard_fails_closed_on_every_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    guard, _calls = _guard(monkeypatch, FacadeState(raise_at=stage))

    assert not guard.can_auto_assign(
        actor_id=ACTOR_ID,
        workspace_id=WORKSPACE_ID,
        repository_id=REPOSITORY_ID,
        required_capabilities=CAPABILITIES,
    )
