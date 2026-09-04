from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

import control_plane.app.modules.organization as organization
from control_plane.app.modules.requirement.domain.gate_policy import GatePolicy, ResolvedGatePolicy


@pytest.mark.parametrize("kind,owner", [("MEMBER", "member"), ("LEADER", "leader")])
def test_formal_routing_preserves_owner_organization_and_policy_evidence(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    owner: str,
) -> None:
    import control_plane.app.modules.source_control.adapters as adapters

    adapter_type = getattr(adapters, "GovernedFormalReviewRoutingAdapter", None)
    assert adapter_type is not None, "Formal routing must use Organization owner facts"
    context = organization.ReportingContext(
        account_id=owner,
        kind=kind,
        reviewer_id="leader",
        participants=(),
        facts_hash="sha256:" + "9" * 64,
    )
    monkeypatch.setattr(organization, "reporting_context", lambda *args, **kwargs: context)
    policy = ResolvedGatePolicy(
        "requirement.gate", "PLATFORM", 1, 2, "7" * 64, GatePolicy((), ("code.change",), 30)
    )
    checked = []

    def evaluate(
        actor_id: str, workspace_id: str, required_capabilities: tuple[str, ...]
    ) -> SimpleNamespace:
        checked.append((actor_id, workspace_id, required_capabilities))
        return SimpleNamespace(
            eligible=True,
            actor_id=actor_id,
            workspace_id=workspace_id,
            required_capabilities=required_capabilities,
            model_dump=lambda **_: {"eligible": True},
        )

    engine = create_engine("sqlite://")
    try:
        adapter = adapter_type(
            engine,
            object(),
            SimpleNamespace(resolved_snapshot=lambda: policy),
            SimpleNamespace(evaluate=evaluate),
        )
        result = adapter.resolve(
            workspace_id="w", repository_id="repo", work_item_id="wi", human_owner_id=owner
        )
        assert result.default_reviewer_id == "leader"
        assert result.resolution_snapshot["organization"]["account_id"] == owner
        assert result.resolution_snapshot["policy"]["version"] == 2
        assert checked == [("leader", "w", ("merge_request.review", "code.change"))]
    finally:
        engine.dispose()


def test_formal_routing_fails_closed_when_relevant_organization_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any, cast

    from control_plane.app.modules.source_control.adapters import GovernedFormalReviewRoutingAdapter

    reads = 0

    def context(*_: object, **__: object) -> organization.ReportingContext:
        nonlocal reads
        reads += 1
        return organization.ReportingContext(
            account_id="member",
            kind="MEMBER",
            reviewer_id="leader",
            participants=(),
            facts_hash="sha256:" + str(reads) * 64,
        )

    monkeypatch.setattr(organization, "reporting_context", context)
    policy = ResolvedGatePolicy(
        "requirement.gate", "PLATFORM", 1, 2, "7" * 64, GatePolicy((), (), 30)
    )
    qualification = SimpleNamespace(
        evaluate=lambda *args: SimpleNamespace(
            eligible=True,
            actor_id="leader",
            workspace_id="w",
            required_capabilities=("merge_request.review",),
            model_dump=lambda **_: {},
        )
    )
    engine = create_engine("sqlite://")
    try:
        adapter = GovernedFormalReviewRoutingAdapter(
            engine,
            cast(Any, object()),
            cast(Any, SimpleNamespace(resolved_snapshot=lambda: policy)),
            qualification,
        )
        with pytest.raises(ValueError):
            adapter.resolve(
                workspace_id="w", repository_id="r", work_item_id="wi", human_owner_id="member"
            )
    finally:
        engine.dispose()
