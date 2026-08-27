import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

import control_plane.app.modules.authorization as authorization_module
import control_plane.app.modules.identity as identity_module
import control_plane.app.modules.requirement as requirement_module
import control_plane.app.modules.workspace as workspace_module
from control_plane.app.modules.identity import AccountStatus
from control_plane.app.modules.requirement import (
    AssignmentState,
    IntegrationDeliveryBlockedReason,
    IntegrationDeliveryContext,
    IntegrationDeliveryRequestKind,
    IntegrationDeliveryRequestMessage,
    IntegrationDeliveryState,
    RepositoryBindingRequestMessage,
    RepositoryState,
    RequirementState,
    RequirementType,
    WorkItemState,
)
from control_plane.app.modules.source_control import RequirementCallbackUnavailable
from control_plane.app.modules.source_control.adapters import (
    CurrentOwnerEligibilityAdapter,
    RequirementFacadeBindingAdapter,
    RequirementFacadeDeliveryAdapter,
)
from control_plane.app.modules.source_control.domain import DeliveryRequestKind
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import (
    BindingBlockedResult,
    BindingReadyResult,
    ExternalMergeDriftResult,
    IntegrationDeliveryBlockedResult,
    IntegrationMergedResult,
    IntegrationMrReadyResult,
    IntegrationReconciliationPendingResult,
    RequirementBindingContext,
)

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


def test_requirement_binding_adapter_forwards_stable_callback_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    calls: list[tuple[str, dict[str, object]]] = []

    def capture(name: str) -> Callable[..., None]:
        def callback(*_args: object, **kwargs: object) -> None:
            calls.append((name, kwargs))

        return callback

    monkeypatch.setattr(requirement_module, "record_repository_binding", capture("ready"))
    monkeypatch.setattr(
        requirement_module,
        "record_repository_binding_blocked",
        capture("blocked"),
    )
    adapter = RequirementFacadeBindingAdapter(
        engine=engine,
        dependencies=object(),
        clock=FixedClock(),
    )
    work_item_id = "50000000-0000-0000-0000-000000000525"
    ready_correlation = "source-control:effect:70000000-0000-0000-0000-000000000525"
    blocked_correlation = f"source-control:work-item:{work_item_id}"

    adapter.record_ready(
        BindingReadyResult(
            work_item_id=work_item_id,
            repository_id="gitlab-project-525",
            base_commit_sha="a" * 40,
            task_branch="task/525",
            expected_revision=5,
            idempotency_key="source-control:binding-ready:525",
            correlation_id=ready_correlation,
        )
    )
    adapter.record_blocked(
        BindingBlockedResult(
            work_item_id=work_item_id,
            repository_id="gitlab-project-525",
            reason_code=SourceControlReason.OWNER_INELIGIBLE,
            expected_revision=5,
            idempotency_key="source-control:binding-blocked:525",
            correlation_id=blocked_correlation,
        )
    )

    assert [(name, kwargs["correlation_id"]) for name, kwargs in calls] == [
        ("ready", ready_correlation),
        ("blocked", blocked_correlation),
    ]
    engine.dispose()


def test_requirement_delivery_adapter_uses_package_root_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    dependencies = SimpleNamespace(clock=FixedClock())
    monkeypatch.setattr(
        requirement_module,
        "claim_integration_delivery_requests",
        lambda *_args, **_kwargs: (
            IntegrationDeliveryRequestMessage(
                message_id="30000000-0000-0000-0000-000000000522",
                payload_hash="sha256:delivery-request",
                requirement_id="40000000-0000-0000-0000-000000000522",
                requirement_revision=3,
                work_item_id="50000000-0000-0000-0000-000000000522",
                work_item_revision=5,
                repository_id="gitlab-project-522",
                actor_id="employee-1",
                kind=IntegrationDeliveryRequestKind.CREATE_MR,
                integration_merge_request_binding_id=None,
                attempts=1,
            ),
        ),
    )
    adapter = RequirementFacadeDeliveryAdapter(
        engine=engine,
        dependencies=dependencies,
    )

    messages = adapter.claim_requests(limit=10, lease_until=NOW + timedelta(minutes=1))

    assert messages[0].kind is DeliveryRequestKind.CREATE_MR
    assert messages[0].actor_id == "employee-1"
    engine.dispose()


@pytest.mark.parametrize("malformed", ["unknown-kind", "schema-drift"])
def test_requirement_delivery_adapter_sanitizes_claim_mapping_failures(
    monkeypatch: pytest.MonkeyPatch,
    malformed: str,
) -> None:
    engine = create_engine("sqlite://")
    sensitive_input = "test-only-sensitive-provider-payload"
    message = SimpleNamespace(
        message_id="30000000-0000-0000-0000-000000000526",
        payload_hash="sha256:delivery-request",
        requirement_id="40000000-0000-0000-0000-000000000526",
        requirement_revision=3,
        work_item_id="50000000-0000-0000-0000-000000000526",
        work_item_revision=5,
        repository_id="gitlab-project-526",
        actor_id="employee-1",
        kind=IntegrationDeliveryRequestKind.CREATE_MR,
        integration_merge_request_binding_id=None,
        attempts=1,
    )
    if malformed == "unknown-kind":
        message.kind = SimpleNamespace(value=sensitive_input)
    else:
        message.requirement_revision = 0
        message.actor_id = sensitive_input
    monkeypatch.setattr(
        requirement_module,
        "claim_integration_delivery_requests",
        lambda *_args, **_kwargs: (message,),
    )
    adapter = RequirementFacadeDeliveryAdapter(
        engine=engine,
        dependencies=SimpleNamespace(clock=FixedClock()),
    )

    with pytest.raises(RequirementCallbackUnavailable) as raised:
        adapter.claim_requests(limit=10, lease_until=NOW + timedelta(minutes=1))

    formatted = "".join(traceback.format_exception(raised.value))
    assert str(raised.value) == "Requirement claim unavailable"
    assert raised.value.__cause__ is None
    assert sensitive_input not in formatted
    engine.dispose()


def test_requirement_delivery_adapter_maps_context_to_source_control_dto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    dependencies = SimpleNamespace(clock=FixedClock())
    monkeypatch.setattr(
        requirement_module,
        "get_integration_delivery_context",
        lambda *_args, **_kwargs: IntegrationDeliveryContext(
            requirement_id="40000000-0000-0000-0000-000000000523",
            requirement_revision=7,
            requirement_state=RequirementState.IN_PROGRESS,
            workspace_id="20000000-0000-0000-0000-000000000523",
            work_item_id="50000000-0000-0000-0000-000000000523",
            work_item_revision=5,
            work_item_state=WorkItemState.IN_PROGRESS,
            repository_id="gitlab-project-523",
            repository_state=RepositoryState.BOUND,
            human_owner_id="employee-1",
            required_capabilities=("code.read", "code.change"),
            base_commit_sha="a" * 40,
            task_branch="feat/523-context",
            integration_delivery_state=IntegrationDeliveryState.MR_PENDING,
            integration_merge_request_binding_id=None,
            request_actor_id="employee-1",
        ),
    )
    adapter = RequirementFacadeDeliveryAdapter(engine=engine, dependencies=dependencies)

    context = adapter.delivery_context("50000000-0000-0000-0000-000000000523")

    assert context.requirement_state == "IN_PROGRESS"
    assert context.requirement_revision == 7
    assert context.repository_state == "BOUND"
    assert context.integration_delivery_state == "MR_PENDING"
    assert context.request_actor_id == "employee-1"
    engine.dispose()


def test_requirement_delivery_adapter_calls_public_callbacks_with_stable_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    dependencies = SimpleNamespace(clock=FixedClock())
    calls: list[tuple[str, dict[str, object]]] = []

    def capture(name: str) -> Callable[..., None]:
        def callback(*_args: object, **kwargs: object) -> None:
            calls.append((name, kwargs))

        return callback

    monkeypatch.setattr(requirement_module, "record_integration_mr_ready", capture("ready"))
    monkeypatch.setattr(
        requirement_module,
        "record_integration_delivery_blocked",
        capture("blocked"),
    )
    monkeypatch.setattr(
        requirement_module,
        "record_integration_reconciliation_pending",
        capture("pending"),
    )
    monkeypatch.setattr(requirement_module, "record_integration_merged", capture("merged"))
    monkeypatch.setattr(
        requirement_module,
        "record_external_merge_drift",
        capture("drift"),
    )
    adapter = RequirementFacadeDeliveryAdapter(engine=engine, dependencies=dependencies)
    work_item_id = "50000000-0000-0000-0000-000000000524"
    expected_revision = 5
    idempotency_key = "effect:524"
    correlation_id = "source-control:effect:70000000-0000-0000-0000-000000000524"

    adapter.record_mr_ready(
        IntegrationMrReadyResult(
            work_item_id=work_item_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            binding_id="70000000-0000-0000-0000-000000000524",
            correlation_id=correlation_id,
        )
    )
    adapter.record_blocked(
        IntegrationDeliveryBlockedResult(
            work_item_id=work_item_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            binding_id=None,
            reason_code=SourceControlReason.MR_CONFLICT,
            correlation_id=correlation_id,
        )
    )
    adapter.record_pending(
        IntegrationReconciliationPendingResult(
            work_item_id=work_item_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            binding_id=None,
            correlation_id=correlation_id,
        )
    )
    adapter.record_merged(
        IntegrationMergedResult(
            work_item_id=work_item_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            binding_id="70000000-0000-0000-0000-000000000524",
            correlation_id=correlation_id,
        )
    )
    adapter.record_external_merge_drift(
        ExternalMergeDriftResult(
            work_item_id=work_item_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            binding_id="70000000-0000-0000-0000-000000000524",
            correlation_id=correlation_id,
        )
    )

    assert [name for name, _kwargs in calls] == [
        "ready",
        "blocked",
        "pending",
        "merged",
        "drift",
    ]
    assert calls[1][1]["reason_code"] is IntegrationDeliveryBlockedReason.MR_CONFLICT
    assert all(
        cast(SimpleNamespace, kwargs["actor"]).account_id == "source-control-worker"
        for _, kwargs in calls
    )
    assert all(kwargs["correlation_id"] == correlation_id for _, kwargs in calls)
    engine.dispose()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: BindingReadyResult(
            work_item_id="work-blank",
            repository_id="repository-blank",
            base_commit_sha="a" * 40,
            task_branch="task/blank",
            expected_revision=1,
            idempotency_key="ready-blank",
            correlation_id=" ",
        ),
        lambda: BindingBlockedResult(
            work_item_id="work-blank",
            repository_id="repository-blank",
            reason_code=SourceControlReason.OWNER_INELIGIBLE,
            expected_revision=1,
            idempotency_key="blocked-blank",
            correlation_id=" ",
        ),
        lambda: IntegrationMrReadyResult(
            work_item_id="work-blank",
            binding_id="70000000-0000-0000-0000-000000000526",
            expected_revision=1,
            idempotency_key="mr-ready-blank",
            correlation_id=" ",
        ),
        lambda: IntegrationDeliveryBlockedResult(
            work_item_id="work-blank",
            binding_id=None,
            reason_code=SourceControlReason.MR_CONFLICT,
            expected_revision=1,
            idempotency_key="delivery-blocked-blank",
            correlation_id=" ",
        ),
        lambda: IntegrationReconciliationPendingResult(
            work_item_id="work-blank",
            binding_id=None,
            expected_revision=1,
            idempotency_key="pending-blank",
            correlation_id=" ",
        ),
        lambda: IntegrationMergedResult(
            work_item_id="work-blank",
            binding_id="70000000-0000-0000-0000-000000000526",
            expected_revision=1,
            idempotency_key="merged-blank",
            correlation_id=" ",
        ),
        lambda: ExternalMergeDriftResult(
            work_item_id="work-blank",
            binding_id="70000000-0000-0000-0000-000000000526",
            expected_revision=1,
            idempotency_key="drift-blank",
            correlation_id=" ",
        ),
    ],
)
def test_callback_port_results_reject_blank_correlation(factory: Callable[[], object]) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_requirement_delivery_adapter_sanitizes_public_facade_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    dependencies = SimpleNamespace(clock=FixedClock())
    monkeypatch.setattr(
        requirement_module,
        "acknowledge_integration_delivery_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider-payload=test-only-secret")
        ),
    )
    adapter = RequirementFacadeDeliveryAdapter(engine=engine, dependencies=dependencies)

    with pytest.raises(RequirementCallbackUnavailable) as raised:
        adapter.acknowledge_request("30000000-0000-0000-0000-000000000525")

    assert "provider-payload" not in str(raised.value)
    assert raised.value.__cause__ is None
    engine.dispose()
