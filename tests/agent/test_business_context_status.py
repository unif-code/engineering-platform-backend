from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from control_plane.app.modules.agent.adapters import requirement as requirement_adapter
from control_plane.app.modules.agent.adapters.requirement import RequirementFacadeExecutionContext
from control_plane.app.modules.agent.application import queries
from control_plane.app.modules.agent.application.dependencies import AgentDependencies
from control_plane.app.modules.agent.application.errors import (
    AgentBusinessContextReason,
    InvalidRequirementExecutionContext,
)
from control_plane.app.modules.agent.ports.runtime import (
    RequirementExecutionContext,
    RequirementExecutionRequest,
)
from control_plane.app.modules.requirement import (
    RequirementDependencyUnavailable,
    RequirementNotFound,
)
from tests.agent.test_repository import NOW, RUN
from tests.agent.test_start_run import (
    REQUIREMENT_ID,
    WORK_ITEM_ID,
    WORKSPACE_ID,
    _assignment,
    _requirement_details,
)


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("workspace", "WORKSPACE_CHANGED"),
        ("work-missing", "WORK_ITEM_NOT_IN_REQUIREMENT"),
        ("work-ambiguous", "OWNER_DATA_AMBIGUOUS"),
        ("work-contradiction", "OWNER_DATA_INVALID"),
        ("assignment-missing", "ASSIGNMENT_MISSING"),
        ("assignment-ambiguous", "OWNER_DATA_AMBIGUOUS"),
        ("wrong-requirement", "OWNER_DATA_INVALID"),
        ("invalid-workspace", "OWNER_DATA_INVALID"),
        ("invalid-assignment", "OWNER_DATA_INVALID"),
        ("foreign-assignment", "OWNER_DATA_INVALID"),
        ("missing-work-with-assignment", "OWNER_DATA_INVALID"),
    ],
)
def test_actual_owner_branches_provide_structured_reasons(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    reason: str,
) -> None:
    details = _requirement_details(assignments=(_assignment(),))
    other = "10000000-0000-0000-0000-000000009999"
    if scenario == "workspace":
        details = replace(details, requirement=replace(details.requirement, workspace_id=other))
    if scenario == "work-missing":
        details = replace(details, work_items=(), work_item_assignments=())
    if scenario == "missing-work-with-assignment":
        details = replace(details, work_items=())
    if scenario == "work-ambiguous":
        details = replace(details, work_items=details.work_items * 2)
    if scenario == "work-contradiction":
        details = replace(
            details, work_items=(replace(details.work_items[0], requirement_id=other),)
        )
    if scenario == "assignment-missing":
        details = replace(details, work_item_assignments=())
    if scenario == "assignment-ambiguous":
        details = replace(details, work_item_assignments=details.work_item_assignments * 2)
    if scenario == "wrong-requirement":
        details = replace(details, requirement=replace(details.requirement, id=other))
    if scenario == "invalid-workspace":
        details = replace(
            details, requirement=replace(details.requirement, workspace_id="private-invalid")
        )
    if scenario == "invalid-assignment":
        details = replace(
            details, work_item_assignments=(_assignment(assignment_id="private-invalid"),)
        )
    if scenario == "foreign-assignment":
        details = replace(
            details, work_item_assignments=(replace(_assignment(), work_item_id=other),)
        )
    monkeypatch.setattr(requirement_adapter, "get_requirement", lambda *_args, **_kwargs: details)
    adapter = RequirementFacadeExecutionContext(None, None)  # type: ignore[arg-type]
    with pytest.raises(InvalidRequirementExecutionContext) as error:
        adapter.resolve(
            RequirementExecutionRequest(
                workspace_id=WORKSPACE_ID,
                requirement_id=REQUIREMENT_ID,
                work_item_id=WORK_ITEM_ID,
            )
        )
    assert getattr(error.value, "reason", None) == reason


def inspect_status(run: Any, port: Mock, clock: Any = lambda: NOW) -> Any:
    assert hasattr(queries, "get_business_context_status"), (
        "read-only business-context projection is missing"
    )
    dependencies = cast(AgentDependencies, SimpleNamespace(requirement_context=port, clock=clock))
    return queries.get_business_context_status(run, dependencies=dependencies)


def context() -> RequirementExecutionContext:
    assert RUN.business_context is not None
    return RequirementExecutionContext(
        workspace_id=RUN.workspace_id,
        **RUN.business_context.model_dump(),
        goal_ref="opaque:ignored-current-goal",
    )


def test_legacy_null_is_unverifiable_without_calling_owner_or_parsing_goal() -> None:
    port = Mock()
    port.resolve.side_effect = AssertionError("null source must not read the owner")
    result = inspect_status(RUN.model_copy(update={"business_context": None}), port)
    assert result.currentness == "UNVERIFIABLE"
    assert result.reasons == ("BUSINESS_CONTEXT_NOT_RECORDED",)
    assert (result.run_id, result.workspace_id, result.checked_at) == (
        RUN.id,
        RUN.workspace_id,
        NOW,
    )
    port.resolve.assert_not_called()


def test_matching_current_identities_are_observed_after_the_read_without_writing_facts() -> None:
    port = Mock()
    order: list[str] = []

    def resolve(request: RequirementExecutionRequest) -> RequirementExecutionContext:
        order.append("owner")
        assert RUN.business_context is not None
        assert request.model_dump() == {
            "workspace_id": RUN.workspace_id,
            "requirement_id": RUN.business_context.requirement_id,
            "work_item_id": RUN.business_context.work_item_id,
        }
        return context()

    def clock() -> Any:
        order.append("clock")
        return NOW + timedelta(seconds=1)

    port.resolve.side_effect = resolve
    before = RUN.model_dump()
    result = inspect_status(RUN, port, clock)
    assert result.currentness == "CURRENT" and result.reasons == ()
    assert result.checked_at == NOW + timedelta(seconds=1)
    assert order == ["owner", "clock"] and RUN.model_dump() == before


@pytest.mark.parametrize(
    ("field", "value", "state", "reason"),
    [
        ("workspace_id", "10000000-0000-0000-0000-000000009999", "STALE", "WORKSPACE_CHANGED"),
        ("assignment_id", "10000000-0000-0000-0000-000000009999", "STALE", "ASSIGNMENT_CHANGED"),
        (
            "requirement_id",
            "10000000-0000-0000-0000-000000009999",
            "UNVERIFIABLE",
            "OWNER_DATA_INVALID",
        ),
        (
            "work_item_id",
            "10000000-0000-0000-0000-000000009999",
            "UNVERIFIABLE",
            "OWNER_DATA_INVALID",
        ),
        ("assignment_id", "not-a-uuid", "UNVERIFIABLE", "OWNER_DATA_INVALID"),
        ("workspace_id", "not-a-uuid", "UNVERIFIABLE", "OWNER_DATA_INVALID"),
        ("assignment_id", None, "UNVERIFIABLE", "OWNER_DATA_INVALID"),
    ],
)
def test_port_results_require_valid_requested_target_before_classifying_change(
    field: str,
    value: Any,
    state: str,
    reason: str,
) -> None:
    port = Mock()
    port.resolve.return_value = RequirementExecutionContext.model_construct(
        **{**context().model_dump(), field: value}
    )
    result = inspect_status(RUN, port)
    assert (result.currentness, result.reasons) == (state, (reason,))


def test_another_requirement_with_another_workspace_is_invalid_not_change_evidence() -> None:
    port = Mock()
    port.resolve.return_value = context().model_copy(
        update={
            "requirement_id": "10000000-0000-0000-0000-000000009998",
            "workspace_id": "10000000-0000-0000-0000-000000009999",
        }
    )
    result = inspect_status(RUN, port)
    assert (result.currentness, result.reasons) == ("UNVERIFIABLE", ("OWNER_DATA_INVALID",))


def test_unknown_structured_reason_does_not_infer_change_from_message() -> None:
    port = Mock()
    error = InvalidRequirementExecutionContext(
        "Requirement WorkItem has no current Agent assignment"
    )
    error.reason = cast(Any, "FUTURE_UNKNOWN_REASON")
    port.resolve.side_effect = error
    result = inspect_status(RUN, port)
    assert (result.currentness, result.reasons) == ("UNVERIFIABLE", ("OWNER_DATA_INVALID",))


@pytest.mark.parametrize(
    ("error", "state", "reason"),
    [
        (RequirementNotFound("private-missing-id"), "STALE", "REQUIREMENT_NOT_FOUND"),
        (
            RequirementDependencyUnavailable("private-database-path"),
            "UNVERIFIABLE",
            "OWNER_UNAVAILABLE",
        ),
        (
            InvalidRequirementExecutionContext("workspace does not match"),
            "UNVERIFIABLE",
            "OWNER_DATA_INVALID",
        ),
        (
            InvalidRequirementExecutionContext(
                "private", reason=AgentBusinessContextReason.WORKSPACE_CHANGED
            ),
            "STALE",
            "WORKSPACE_CHANGED",
        ),
        (
            InvalidRequirementExecutionContext(
                "private", reason=AgentBusinessContextReason.WORK_ITEM_NOT_IN_REQUIREMENT
            ),
            "STALE",
            "WORK_ITEM_NOT_IN_REQUIREMENT",
        ),
        (
            InvalidRequirementExecutionContext(
                "private", reason=AgentBusinessContextReason.ASSIGNMENT_MISSING
            ),
            "STALE",
            "ASSIGNMENT_MISSING",
        ),
        (
            InvalidRequirementExecutionContext(
                "private", reason=AgentBusinessContextReason.OWNER_DATA_AMBIGUOUS
            ),
            "UNVERIFIABLE",
            "OWNER_DATA_AMBIGUOUS",
        ),
        (
            InvalidRequirementExecutionContext(
                "private", reason=AgentBusinessContextReason.OWNER_DATA_INVALID
            ),
            "UNVERIFIABLE",
            "OWNER_DATA_INVALID",
        ),
        (
            InvalidRequirementExecutionContext(
                "private", reason=AgentBusinessContextReason.ASSIGNMENT_CHANGED
            ),
            "UNVERIFIABLE",
            "OWNER_DATA_INVALID",
        ),
    ],
)
def test_only_typed_owner_facts_can_prove_change_and_diagnostics_are_not_returned(
    error: Exception,
    state: str,
    reason: str,
) -> None:
    port = Mock()
    port.resolve.side_effect = error
    result = inspect_status(RUN, port)
    assert (result.currentness, result.reasons) == (state, (reason,))
    assert "private" not in result.model_dump_json()
    assert "workspace does not match" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("state", "reasons"),
    [
        ("CURRENT", ("OWNER_UNAVAILABLE",)),
        ("STALE", ()),
        ("UNVERIFIABLE", ()),
    ],
)
def test_projection_cannot_contradict_its_reason_state(
    state: str, reasons: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        queries.AgentRunBusinessContextStatus.model_validate(
            {
                "run_id": RUN.id,
                "workspace_id": RUN.workspace_id,
                "checked_at": NOW,
                "currentness": state,
                "reasons": reasons,
            }
        )
