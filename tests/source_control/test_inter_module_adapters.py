from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

import control_plane.app.modules.authorization as authorization_module
import control_plane.app.modules.identity as identity_module
import control_plane.app.modules.requirement as requirement_module
import control_plane.app.modules.workspace as workspace_module
from control_plane.app.modules.identity import AccountStatus
from control_plane.app.modules.requirement import (
    AssignmentState,
    RepositoryBindingRequestMessage,
    RequirementType,
)
from control_plane.app.modules.source_control import RequirementCallbackUnavailable
from control_plane.app.modules.source_control.adapters import (
    CurrentOwnerEligibilityAdapter,
    RequirementFacadeBindingAdapter,
)
from control_plane.app.modules.source_control.ports import RequirementBindingContext

NOW = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


def _context() -> RequirementBindingContext:
    return RequirementBindingContext(
        requirement_id="40000000-0000-0000-0000-000000000521",
        requirement_type=RequirementType.FEAT.value,
        requirement_title="Source control",
        workspace_id="20000000-0000-0000-0000-000000000521",
        work_item_id="50000000-0000-0000-0000-000000000521",
        work_item_revision=1,
        repository_id="gitlab-project-521",
        assignment_state=AssignmentState.ASSIGNED.value,
        human_owner_id="account-521",
        required_capabilities=("code.read", "code.change"),
    )


def test_current_owner_eligibility_requires_enabled_formal_member_with_every_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engines = [create_engine("sqlite://") for _ in range(3)]
    requested_capabilities: list[str] = []
    monkeypatch.setattr(
        identity_module,
        "get_account",
        lambda *_args, **_kwargs: SimpleNamespace(status=AccountStatus.ENABLED),
    )
    monkeypatch.setattr(
        workspace_module,
        "is_formal_member",
        lambda *_args, **_kwargs: True,
    )

    def effective_grants(
        *_args: object,
        capability: str,
        **_kwargs: object,
    ) -> list[SimpleNamespace]:
        requested_capabilities.append(capability)
        return [SimpleNamespace(id=f"grant:{capability}")]

    monkeypatch.setattr(authorization_module, "effective_grants", effective_grants)
    adapter = CurrentOwnerEligibilityAdapter(
        identity_engine=engines[0],
        identity_dependencies=object(),
        workspace_engine=engines[1],
        workspace_dependencies=object(),
        authorization_engine=engines[2],
        authorization_dependencies=object(),
    )

    result = adapter.evaluate(_context())

    assert result.eligible is True
    assert requested_capabilities == ["code.read", "code.change"]
    for engine in engines:
        engine.dispose()


def test_current_owner_eligibility_fails_closed_when_dependency_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engines = [create_engine("sqlite://") for _ in range(3)]
    monkeypatch.setattr(
        identity_module,
        "get_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    adapter = CurrentOwnerEligibilityAdapter(
        identity_engine=engines[0],
        identity_dependencies=object(),
        workspace_engine=engines[1],
        workspace_dependencies=object(),
        authorization_engine=engines[2],
        authorization_dependencies=object(),
    )

    result = adapter.evaluate(_context())

    assert result.eligible is False
    assert result.reason_code == "OWNER_INELIGIBLE"
    for engine in engines:
        engine.dispose()


def test_requirement_adapter_maps_claimed_messages_through_package_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    monkeypatch.setattr(
        requirement_module,
        "claim_repository_binding_requests",
        lambda *_args, **_kwargs: (
            RepositoryBindingRequestMessage(
                message_id="30000000-0000-0000-0000-000000000521",
                requirement_id="40000000-0000-0000-0000-000000000521",
                requirement_version=1,
                work_item_id="50000000-0000-0000-0000-000000000521",
                repository_id="gitlab-project-521",
                attempts=1,
            ),
        ),
    )
    adapter = RequirementFacadeBindingAdapter(
        engine=engine,
        dependencies=object(),
        clock=FixedClock(),
    )

    messages = adapter.claim_requests(limit=10, lease_until=NOW + timedelta(minutes=1))

    assert messages[0].topic == "requirement.repository-binding.requested"
    assert messages[0].repository_id == "gitlab-project-521"
    engine.dispose()


def test_requirement_adapter_sanitizes_acknowledgement_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    monkeypatch.setattr(
        requirement_module,
        "acknowledge_repository_binding_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider detail")),
    )
    adapter = RequirementFacadeBindingAdapter(
        engine=engine,
        dependencies=object(),
        clock=FixedClock(),
    )

    with pytest.raises(RequirementCallbackUnavailable) as raised:
        adapter.acknowledge_request("30000000-0000-0000-0000-000000000521")

    assert "provider detail" not in str(raised.value)
    engine.dispose()
