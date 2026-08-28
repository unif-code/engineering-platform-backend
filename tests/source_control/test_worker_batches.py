import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from control_plane.app.bootstrap.source_control_runtime import SourceControlRuntime
from control_plane.app.modules.source_control import (
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    process_due_source_control_inboxes,
    reconcile_due_source_control_effects,
    relay_due_source_control_requests,
)
from control_plane.app.modules.source_control.application import (
    _integration_reconcile_callbacks as integration_callback_module,
)
from control_plane.app.modules.source_control.application import batches
from control_plane.app.modules.source_control.application import (
    integration_reconciliation as integration_reconciliation_module,
)
from control_plane.app.modules.source_control.application import (
    reconciliation as branch_reconciliation_module,
)
from control_plane.app.modules.source_control.application._batch_claim import InboxClaimLost
from control_plane.app.modules.source_control.domain import EffectOperation, EffectState
from control_plane.tools import source_control_worker


def _dependencies() -> SourceControlDependencies:
    return cast(SourceControlDependencies, object())


def test_worker_default_path_owns_the_shared_runtime_context(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lifecycle: list[str] = []
    dependencies = _dependencies()

    @contextmanager
    def runtime_context() -> Iterator[SourceControlRuntime]:
        lifecycle.append("entered")
        try:
            yield cast(
                SourceControlRuntime,
                SimpleNamespace(dependencies=dependencies),
            )
        finally:
            lifecycle.append("closed")

    monkeypatch.setattr(
        source_control_worker,
        "process_due_source_control_inboxes",
        lambda *, limit, dependencies: SimpleNamespace(
            claimed=limit,
            processed=limit,
            released=0,
            effect_ids=(),
            error_codes=(),
        ),
    )

    exit_code = source_control_worker.main(
        ["process", "--limit", "3"],
        runtime_context_provider=runtime_context,
    )

    assert exit_code == 0
    assert lifecycle == ["entered", "closed"]
    assert json.loads(capsys.readouterr().out) == {
        "command": "process",
        "claimed": 3,
        "processed": 3,
        "released": 0,
        "effect_ids": [],
        "error_codes": [],
    }


def test_worker_incomplete_runtime_is_nonzero_and_sanitized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    @contextmanager
    def unavailable_runtime() -> Iterator[SourceControlRuntime]:
        raise SourceControlDependencyUnavailable("private-runtime-detail")
        yield  # pragma: no cover

    exit_code = source_control_worker.main(
        ["relay", "--limit", "2"],
        runtime_context_provider=unavailable_runtime,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert json.loads(output) == {
        "command": "relay",
        "errorCodes": ["DEPENDENCY_UNAVAILABLE"],
    }
    assert "private-runtime-detail" not in output


@pytest.mark.parametrize(
    ("facade", "minimum"),
    [
        (relay_due_source_control_requests, 2),
        (process_due_source_control_inboxes, 3),
        (reconcile_due_source_control_effects, 2),
    ],
)
def test_batch_facades_reject_limits_below_their_lane_minimum(
    facade: object,
    minimum: int,
) -> None:
    with pytest.raises(ValueError):
        facade(limit=minimum - 1, dependencies=_dependencies())  # type: ignore[operator]


def test_relay_batch_reserves_both_lanes_with_one_hard_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def binding(*, limit: int, dependencies: SourceControlDependencies) -> object:
        calls.append(("binding", limit))
        return SimpleNamespace(claimed=limit, accepted=limit, released=0)

    def delivery(*, limit: int, dependencies: SourceControlDependencies) -> object:
        calls.append(("delivery", limit))
        return SimpleNamespace(claimed=limit, accepted=limit - 1, released=1)

    monkeypatch.setattr(batches, "relay_binding_requests", binding)
    monkeypatch.setattr(batches, "relay_integration_delivery_requests", delivery)

    result = relay_due_source_control_requests(limit=5, dependencies=_dependencies())

    assert calls == [("binding", 3), ("delivery", 2)]
    assert (result.claimed, result.processed, result.released) == (5, 4, 1)
    assert result.claimed <= 5


def test_reconcile_batch_reserves_both_lanes_and_never_truncates_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def branch(*, limit: int, dependencies: SourceControlDependencies) -> object:
        calls.append(("branch", limit))
        return SimpleNamespace(
            effects=tuple(
                SimpleNamespace(id=f"branch-{index}", last_error_code=None)
                for index in range(limit)
            )
        )

    def integration(*, limit: int, dependencies: SourceControlDependencies) -> object:
        calls.append(("integration", limit))
        return SimpleNamespace(
            effects=tuple(
                SimpleNamespace(id=f"integration-{index}", last_error_code="MR_CLOSED")
                for index in range(limit)
            )
        )

    monkeypatch.setattr(batches, "reconcile_due_effects", branch)
    monkeypatch.setattr(batches, "reconcile_due_integration_effects", integration)

    result = reconcile_due_source_control_effects(limit=5, dependencies=_dependencies())

    assert calls == [("branch", 3), ("integration", 2)]
    assert result.claimed == result.processed == 5
    assert result.effect_ids == (
        "branch-0",
        "branch-1",
        "branch-2",
        "integration-0",
        "integration-1",
    )
    assert result.error_codes == ("MR_CLOSED", "MR_CLOSED")


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(2, [("branch", 1), ("integration", 1)]), (50, [("branch", 25), ("integration", 25)])],
)
def test_reconcile_batch_never_starves_either_lane_at_supported_limits(
    limit: int,
    expected: list[tuple[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def branch(*, limit: int, dependencies: SourceControlDependencies) -> object:
        calls.append(("branch", limit))
        return SimpleNamespace(effects=())

    def integration(*, limit: int, dependencies: SourceControlDependencies) -> object:
        calls.append(("integration", limit))
        return SimpleNamespace(effects=())

    monkeypatch.setattr(batches, "reconcile_due_effects", branch)
    monkeypatch.setattr(batches, "reconcile_due_integration_effects", integration)

    reconcile_due_source_control_effects(limit=limit, dependencies=_dependencies())

    assert calls == expected


def test_branch_reconciliation_callbacks_only_use_budget_left_after_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_limits: list[int] = []
    claimed = [{"id": "claimed", "work_item_id": "work-claimed"}]
    callbacks = [
        {"id": "claimed", "work_item_id": "work-claimed"},
        {"id": "callback-1", "work_item_id": "work-callback-1"},
        {"id": "callback-2", "work_item_id": "work-callback-2"},
    ]

    def pending_callback_effects(*, limit: int) -> list[dict[str, str]]:
        callback_limits.append(limit)
        return callbacks[:limit]

    repository = SimpleNamespace(
        claim_unknown_effects=lambda *, limit, now, lease_until: claimed[:limit],
        pending_callback_effects=pending_callback_effects,
        binding_by_work_item=lambda work_item_id: None,
    )
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=SimpleNamespace(begin=_ConnectionContext, connect=_ConnectionContext),
            clock=SimpleNamespace(now=lambda: datetime(2026, 8, 26, tzinfo=UTC)),
            repository_factory=lambda db: repository,
            requirement=SimpleNamespace(binding_context=lambda work_item_id: object()),
        ),
    )
    monkeypatch.setattr(
        branch_reconciliation_module, "append_lifecycle_audit", lambda *a, **k: None
    )
    monkeypatch.setattr(
        branch_reconciliation_module,
        "_effect_dto",
        lambda row: SimpleNamespace(id=row["id"], work_item_id=row["work_item_id"]),
    )
    monkeypatch.setattr(
        branch_reconciliation_module,
        "_reconcile_effect",
        lambda effect, *, dependencies: effect,
    )
    monkeypatch.setattr(
        branch_reconciliation_module,
        "_deliver_terminal_callback",
        lambda effect, context, *, binding, dependencies: effect,
    )
    monkeypatch.setattr(
        branch_reconciliation_module,
        "ReconcileDueEffectsResult",
        lambda *, effects: SimpleNamespace(effects=tuple(effects)),
    )

    result = branch_reconciliation_module.reconcile_due_effects(
        limit=2,
        dependencies=dependencies,
    )

    assert callback_limits == [2]
    assert [effect.id for effect in result.effects] == ["claimed", "callback-1"]


def test_integration_reconciliation_callbacks_only_use_budget_left_after_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_limits: list[int] = []
    repository = SimpleNamespace(
        claim_effects=lambda *, limit, now, lease_until: [
            {"id": "claimed", "operation": EffectOperation.CREATE_INTEGRATION_MR}
        ][:limit]
    )
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=SimpleNamespace(begin=_ConnectionContext),
            clock=SimpleNamespace(now=lambda: datetime(2026, 8, 26, tzinfo=UTC)),
            delivery_repository_factory=lambda db: repository,
        ),
    )
    monkeypatch.setattr(
        integration_reconciliation_module,
        "_effect_dto",
        lambda row: SimpleNamespace(id=row["id"], operation=row["operation"]),
    )
    monkeypatch.setattr(
        integration_reconciliation_module,
        "reconcile_create_effect",
        lambda effect, *, dependencies: effect,
    )

    def replay(
        *,
        limit: int,
        excluded_effect_ids: frozenset[str],
        dependencies: object,
    ) -> tuple[object, ...]:
        callback_limits.append(limit)
        assert excluded_effect_ids == frozenset({"claimed"})
        return tuple(SimpleNamespace(id=f"callback-{index}") for index in range(limit))

    monkeypatch.setattr(
        integration_reconciliation_module,
        "replay_pending_integration_callbacks",
        replay,
    )
    monkeypatch.setattr(
        integration_reconciliation_module,
        "ReconcileDueIntegrationEffectsResult",
        lambda *, effects: SimpleNamespace(effects=tuple(effects)),
    )

    result = integration_reconciliation_module.reconcile_due_integration_effects(
        limit=2,
        dependencies=dependencies,
    )

    assert callback_limits == [1]
    assert [effect.id for effect in result.effects] == ["claimed", "callback-0"]


def test_integration_callback_replay_overfetches_excluded_rows_and_stops_at_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_limits: list[int] = []
    recorded: list[str] = []
    effects: list[object] = [
        SimpleNamespace(
            id=effect_id,
            operation=EffectOperation.CREATE_INTEGRATION_MR,
            state=EffectState.SUCCEEDED,
            work_item_id=f"work-{effect_id}",
            requirement_id=f"requirement-{effect_id}",
            repository_id=f"repository-{effect_id}",
        )
        for effect_id in ("claimed", "callback-1", "callback-2")
    ]

    def pending_callback_effects(*, limit: int) -> list[object]:
        scan_limits.append(limit)
        return effects

    repository = SimpleNamespace(
        pending_callback_effects=pending_callback_effects,
        merge_request_binding_by_work_item=lambda work_item_id: {"id": f"binding-{work_item_id}"},
    )
    requirement = SimpleNamespace(
        delivery_context=lambda work_item_id: SimpleNamespace(
            work_item_id=work_item_id,
            work_item_revision=1,
            requirement_id=f"requirement-{work_item_id.removeprefix('work-')}",
            repository_id=f"repository-{work_item_id.removeprefix('work-')}",
        )
    )
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=SimpleNamespace(connect=_ConnectionContext),
            delivery_repository_factory=lambda db: repository,
            requirement_delivery=requirement,
        ),
    )
    monkeypatch.setattr(integration_callback_module, "effect_dto", lambda row: row)
    monkeypatch.setattr(
        integration_callback_module,
        "binding_dto",
        lambda row: SimpleNamespace(id=row["id"]),
    )

    def record_callback(subject: object, effect: object, **kwargs: object) -> object:
        recorded.append(effect.id)  # type: ignore[attr-defined]
        return effect

    monkeypatch.setattr(integration_callback_module, "_record_effect_callback", record_callback)

    result = integration_callback_module.replay_pending_integration_callbacks(
        limit=1,
        excluded_effect_ids=frozenset({"claimed"}),
        dependencies=dependencies,
    )

    assert scan_limits == [2]
    assert recorded == ["callback-1"]
    assert [effect.id for effect in result] == ["callback-1"]


@pytest.mark.parametrize(
    "module",
    [branch_reconciliation_module, integration_reconciliation_module],
)
def test_reconciliation_does_not_scan_callbacks_when_effects_fill_budget(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": "claimed-1",
            "work_item_id": "work-1",
            "operation": EffectOperation.CREATE_INTEGRATION_MR,
        },
        {
            "id": "claimed-2",
            "work_item_id": "work-2",
            "operation": EffectOperation.CREATE_INTEGRATION_MR,
        },
    ]
    repository = SimpleNamespace(
        claim_unknown_effects=lambda *, limit, now, lease_until: rows[:limit],
        claim_effects=lambda *, limit, now, lease_until: rows[:limit],
        pending_callback_effects=lambda *, limit: pytest.fail("callback scan exceeded budget"),
    )
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=SimpleNamespace(begin=_ConnectionContext, connect=_ConnectionContext),
            clock=SimpleNamespace(now=lambda: datetime(2026, 8, 26, tzinfo=UTC)),
            repository_factory=lambda db: repository,
            delivery_repository_factory=lambda db: repository,
        ),
    )
    if module is branch_reconciliation_module:
        monkeypatch.setattr(module, "append_lifecycle_audit", lambda *a, **k: None)
        monkeypatch.setattr(
            module,
            "_effect_dto",
            lambda row: SimpleNamespace(id=row["id"], work_item_id=row["work_item_id"]),
        )
        monkeypatch.setattr(module, "_reconcile_effect", lambda effect, *, dependencies: effect)
        monkeypatch.setattr(
            module,
            "ReconcileDueEffectsResult",
            lambda *, effects: SimpleNamespace(effects=tuple(effects)),
        )
        result = module.reconcile_due_effects(limit=2, dependencies=dependencies)
    else:
        monkeypatch.setattr(
            module,
            "_effect_dto",
            lambda row: SimpleNamespace(id=row["id"], operation=row["operation"]),
        )
        monkeypatch.setattr(
            module, "reconcile_create_effect", lambda effect, *, dependencies: effect
        )
        monkeypatch.setattr(
            module,
            "replay_pending_integration_callbacks",
            lambda **kwargs: pytest.fail("callback replay exceeded budget"),
        )
        monkeypatch.setattr(
            module,
            "ReconcileDueIntegrationEffectsResult",
            lambda *, effects: SimpleNamespace(effects=tuple(effects)),
        )
        result = module.reconcile_due_integration_effects(limit=2, dependencies=dependencies)

    assert len(result.effects) == 2


class _ConnectionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


class _Engine:
    def connect(self) -> _ConnectionContext:
        return _ConnectionContext()

    def begin(self) -> _ConnectionContext:
        return _ConnectionContext()


class _ForbiddenCollaborator:
    def __init__(self, label: str, calls: list[str]) -> None:
        self._label = label
        self._calls = calls

    def __getattr__(self, name: str) -> object:
        self._calls.append(f"{self._label}.{name}")
        raise AssertionError(f"strict claim loser reached {self._label}.{name}")


class _StrictClaimRaceRepository:
    def __init__(self, *, lane: str, state: str) -> None:
        self.lane = lane
        self.state = state
        self.forbidden_calls: list[str] = []

    def pending_binding_request_ids(self, *, limit: int, now: datetime) -> list[str]:
        return ["lost-candidate"] if self.lane == "binding" else []

    def pending_delivery_request_candidates(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> list[dict[str, str]]:
        if not self.lane.startswith("delivery_"):
            return []
        return [
            {
                "message_id": "lost-candidate",
                "topic": self._delivery_topic(),
            }
        ]

    def pending_webhook_ids(self, *, limit: int) -> list[str]:
        return ["lost-candidate"] if self.lane == "webhook" else []

    def claim_binding_request(self, *_args: object, **_kwargs: object) -> None:
        return None

    def binding_request(self, _message_id: str) -> dict[str, str]:
        return {"state": self.state}

    def claim_delivery_request(self, *_args: object, **_kwargs: object) -> None:
        return None

    def delivery_request(self, _message_id: str) -> dict[str, str]:
        return {
            "state": self.state,
            "topic": self._delivery_topic(),
        }

    def _delivery_topic(self) -> str:
        if self.lane == "delivery_merge":
            return "requirement.integration-merge.requested"
        return "requirement.integration-merge-request.requested"

    def webhook_by_id(
        self,
        _inbox_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, str]:
        return {"state": self.state}

    def __getattr__(self, name: str) -> object:
        self.forbidden_calls.append(name)
        raise AssertionError(f"strict claim loser performed repository effect {name}")


def test_process_batch_round_robins_three_read_only_candidate_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_repository = SimpleNamespace(
        pending_binding_request_ids=lambda *, limit, now: ["branch-1", "branch-2"],
        pending_webhook_ids=lambda *, limit: ["webhook-1", "webhook-2"],
    )
    delivery_repository = SimpleNamespace(
        pending_delivery_request_candidates=lambda *, limit, now: [
            {
                "message_id": "create-1",
                "topic": "requirement.integration-merge-request.requested",
            },
            {
                "message_id": "merge-1",
                "topic": "requirement.integration-merge.requested",
            },
        ]
    )
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=_Engine(),
            clock=SimpleNamespace(now=lambda: object()),
            repository_factory=lambda db: primary_repository,
            delivery_repository_factory=lambda db: delivery_repository,
        ),
    )
    calls: list[tuple[str, str]] = []

    def branch(*, message_id: str, dependencies: SourceControlDependencies) -> object:
        calls.append(("branch", message_id))
        return SimpleNamespace(
            effect=SimpleNamespace(id=f"effect-{message_id}"),
            blocked_reason=None,
        )

    def create(*, message_id: str, dependencies: SourceControlDependencies) -> object:
        calls.append(("create", message_id))
        return SimpleNamespace(
            effect=SimpleNamespace(id=f"effect-{message_id}"),
            blocked_reason=None,
        )

    def merge(*, message_id: str, dependencies: SourceControlDependencies) -> object:
        calls.append(("merge", message_id))
        return SimpleNamespace(
            effect=SimpleNamespace(id=f"effect-{message_id}"),
            blocked_reason="MR_CLOSED",
        )

    def webhook(inbox_id: str, *, dependencies: SourceControlDependencies) -> int:
        calls.append(("webhook", inbox_id))
        return 1

    monkeypatch.setattr(batches, "process_binding_candidate", branch)
    monkeypatch.setattr(batches, "process_integration_mr_candidate", create)
    monkeypatch.setattr(batches, "process_integration_merge_candidate", merge)
    monkeypatch.setattr(batches, "process_webhook_candidate", webhook)

    result = process_due_source_control_inboxes(limit=5, dependencies=dependencies)

    assert calls == [
        ("branch", "branch-1"),
        ("create", "create-1"),
        ("webhook", "webhook-1"),
        ("branch", "branch-2"),
        ("merge", "merge-1"),
    ]
    assert result.claimed == result.processed == 5
    assert result.effect_ids == (
        "effect-branch-1",
        "effect-create-1",
        "effect-branch-2",
        "effect-merge-1",
    )
    assert result.error_codes == ("MR_CLOSED",)


def test_process_batch_treats_typed_concurrent_claim_loss_as_benign_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_repository = SimpleNamespace(
        pending_binding_request_ids=lambda *, limit, now: [],
        pending_webhook_ids=lambda *, limit: [],
    )
    delivery_repository = SimpleNamespace(
        pending_delivery_request_candidates=lambda *, limit, now: [
            {
                "message_id": "create-lost",
                "topic": "requirement.integration-merge-request.requested",
            }
        ]
    )
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=_Engine(),
            clock=SimpleNamespace(now=lambda: object()),
            repository_factory=lambda db: primary_repository,
            delivery_repository_factory=lambda db: delivery_repository,
        ),
    )

    def lose_claim(*, message_id: str, dependencies: SourceControlDependencies) -> object:
        raise InboxClaimLost(message_id)

    monkeypatch.setattr(batches, "process_integration_mr_candidate", lose_claim)

    result = process_due_source_control_inboxes(limit=3, dependencies=dependencies)

    assert (result.claimed, result.processed) == (0, 0)
    assert result.effect_ids == result.error_codes == ()


@pytest.mark.parametrize(
    ("lane", "state"),
    [
        ("binding", "PROCESSING"),
        ("binding", "PROCESSED"),
        ("delivery_create", "PROCESSING"),
        ("delivery_create", "PROCESSED"),
        ("delivery_merge", "PROCESSING"),
        ("delivery_merge", "PROCESSED"),
        ("webhook", "PROCESSED"),
    ],
)
def test_process_batch_exact_claim_loser_is_a_side_effect_free_noop(
    lane: str,
    state: str,
) -> None:
    repository = _StrictClaimRaceRepository(lane=lane, state=state)
    provider_calls: list[str] = []
    callback_calls: list[str] = []
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=_Engine(),
            clock=SimpleNamespace(now=lambda: datetime(2026, 8, 28, tzinfo=UTC)),
            repository_factory=lambda db: repository,
            delivery_repository_factory=lambda db: repository,
            requirement=_ForbiddenCollaborator("requirement", callback_calls),
            requirement_delivery=_ForbiddenCollaborator("requirement_delivery", callback_calls),
            eligibility=_ForbiddenCollaborator("eligibility", provider_calls),
            gitlab=_ForbiddenCollaborator("gitlab", provider_calls),
            gitlab_merge_requests=_ForbiddenCollaborator("gitlab_merge_requests", provider_calls),
            policy=_ForbiddenCollaborator("policy", provider_calls),
        ),
    )

    result = process_due_source_control_inboxes(limit=3, dependencies=dependencies)

    assert (result.claimed, result.processed) == (0, 0)
    assert result.effect_ids == result.error_codes == ()
    assert repository.forbidden_calls == []
    assert provider_calls == []
    assert callback_calls == []


def test_process_batch_does_not_reassign_unused_active_lane_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_repository = SimpleNamespace(
        pending_binding_request_ids=lambda *, limit, now: [
            "branch-1",
            "branch-2",
            "branch-3",
            "branch-4",
            "branch-5",
        ],
        pending_webhook_ids=lambda *, limit: ["webhook-1"],
    )
    delivery_repository = SimpleNamespace(
        pending_delivery_request_candidates=lambda *, limit, now: [
            {
                "message_id": "create-1",
                "topic": "requirement.integration-merge-request.requested",
            }
        ]
    )
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=_Engine(),
            clock=SimpleNamespace(now=lambda: object()),
            repository_factory=lambda db: primary_repository,
            delivery_repository_factory=lambda db: delivery_repository,
        ),
    )
    calls: list[str] = []

    def processed(identifier: str) -> object:
        calls.append(identifier)
        return SimpleNamespace(effect=None, blocked_reason=None)

    monkeypatch.setattr(
        batches,
        "process_binding_candidate",
        lambda *, message_id, dependencies: processed(message_id),
    )
    monkeypatch.setattr(
        batches,
        "process_integration_mr_candidate",
        lambda *, message_id, dependencies: processed(message_id),
    )
    monkeypatch.setattr(
        batches,
        "process_webhook_candidate",
        lambda inbox_id, *, dependencies: (processed(inbox_id), 1)[1],
    )

    result = process_due_source_control_inboxes(limit=6, dependencies=dependencies)

    assert calls == ["branch-1", "create-1", "webhook-1", "branch-2"]
    assert result.claimed == result.processed == 4


def test_process_batch_discards_non_allowlisted_provider_reason_from_worker_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unsafe_reason = "GitLab 500 token=" + "".join(("gl", "pat", "-secret"))
    primary_repository = SimpleNamespace(
        pending_binding_request_ids=lambda *, limit, now: ["branch-safe", "branch-unsafe"],
        pending_webhook_ids=lambda *, limit: [],
    )
    delivery_repository = SimpleNamespace(
        pending_delivery_request_candidates=lambda *, limit, now: []
    )
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(
            engine=_Engine(),
            clock=SimpleNamespace(now=lambda: object()),
            repository_factory=lambda db: primary_repository,
            delivery_repository_factory=lambda db: delivery_repository,
        ),
    )
    monkeypatch.setattr(
        batches,
        "process_binding_candidate",
        lambda *, message_id, dependencies: SimpleNamespace(
            effect=None,
            blocked_reason="MR_CLOSED" if message_id == "branch-safe" else unsafe_reason,
        ),
    )

    result = process_due_source_control_inboxes(limit=6, dependencies=dependencies)
    monkeypatch.setattr(
        source_control_worker,
        "process_due_source_control_inboxes",
        lambda *, limit, dependencies: result,
    )
    exit_code = source_control_worker.main(
        ["process", "--limit", "6"],
        dependencies_provider=lambda: dependencies,
    )
    output = capsys.readouterr().out

    assert result.error_codes == ("MR_CLOSED",)
    assert exit_code == 0
    assert json.loads(output) == {
        "command": "process",
        "claimed": 2,
        "processed": 2,
        "released": 0,
        "effect_ids": [],
        "error_codes": ["MR_CLOSED"],
    }
    assert unsafe_reason not in output
    assert "glpat-secret" not in output


@pytest.mark.parametrize(
    ("command", "minimum"),
    [("relay", 2), ("process", 3), ("reconcile", 2)],
)
def test_worker_reports_invalid_argument_for_command_specific_minimum(
    command: str,
    minimum: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = source_control_worker.main(
        [command, "--limit", str(minimum - 1)],
        dependencies_provider=_dependencies,
    )

    assert exit_code == 2
    assert capsys.readouterr().out == (
        f'{{"command": "{command}", "errorCodes": ["INVALID_ARGUMENT"]}}\n'
    )


@pytest.mark.parametrize(
    ("command", "minimum"),
    [("relay", 2), ("process", 3), ("reconcile", 2)],
)
def test_worker_rejects_invalid_limit_before_resolving_dependencies(
    command: str,
    minimum: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_called = False

    def dependencies_provider() -> SourceControlDependencies:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("invalid limits must not resolve dependencies")

    exit_code = source_control_worker.main(
        [command, "--limit", str(minimum - 1)],
        dependencies_provider=dependencies_provider,
    )

    assert exit_code == 2
    assert provider_called is False
    assert capsys.readouterr().out == (
        f'{{"command": "{command}", "errorCodes": ["INVALID_ARGUMENT"]}}\n'
    )


def test_worker_dispatches_only_to_the_three_package_batch_facades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def result(command: str, limit: int) -> object:
        calls.append((command, limit))
        return SimpleNamespace(
            claimed=limit,
            processed=limit - 1,
            released=1,
            effect_ids=(f"{command}-effect",),
            error_codes=("MR_CLOSED",),
        )

    monkeypatch.setattr(
        source_control_worker,
        "relay_due_source_control_requests",
        lambda *, limit, dependencies: result("relay", limit),
    )
    monkeypatch.setattr(
        source_control_worker,
        "process_due_source_control_inboxes",
        lambda *, limit, dependencies: result("process", limit),
    )
    monkeypatch.setattr(
        source_control_worker,
        "reconcile_due_source_control_effects",
        lambda *, limit, dependencies: result("reconcile", limit),
    )

    reports = tuple(
        source_control_worker.run_worker_once(
            command,
            limit=5,
            dependencies=_dependencies(),
        )
        for command in ("relay", "process", "reconcile")
    )

    assert calls == [("relay", 5), ("process", 5), ("reconcile", 5)]
    assert tuple(report.effect_ids for report in reports) == (
        ("relay-effect",),
        ("process-effect",),
        ("reconcile-effect",),
    )
    assert all(report.error_codes == ("MR_CLOSED",) for report in reports)
