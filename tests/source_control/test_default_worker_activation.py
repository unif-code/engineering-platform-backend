from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from control_plane.app.modules.source_control import FormalDeliveryConflict
from control_plane.app.modules.source_control.application import batches, formal
from control_plane.tools.source_control_worker import main, run_worker_once


@pytest.mark.parametrize(("command", "limit"), [("relay", 3), ("process", 4), ("reconcile", 2)])
def test_worker_rejects_limits_that_starve_a_production_lane(command: str, limit: int) -> None:
    assert (
        main(
            [command, "--limit", str(limit)],
            dependencies_provider=lambda: pytest.fail("resolved dependencies"),
        )
        == 2
    )


def test_worker_unobserved_claim_failure_reports_error_without_inventing_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batches, "_require_processing_dependencies", lambda _: None)
    monkeypatch.setattr(
        batches,
        "_pending_process_candidates",
        lambda **_: (batches._ProcessCandidate("binding", "before-claim"),),
    )

    def unavailable(**_: object) -> Any:
        raise RuntimeError("private dependency error before claim")

    monkeypatch.setattr(batches, "process_binding_candidate", unavailable)
    result = run_worker_once("process", limit=5, dependencies=cast(Any, object()))
    assert (result.claimed, result.processed, result.released) == (0, 0, 0)
    assert result.error_codes == ("CONNECTOR_UNAVAILABLE",)


@pytest.mark.parametrize("lane", ["evidence", "formal"])
@pytest.mark.parametrize("state", ["PROCESSING", "PROCESSED"])
def test_folded_delivery_claim_loser_never_releases_another_workers_inbox(
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    state: str,
) -> None:
    mutations = []

    class Repository:
        def evidence_request(self, *args: Any, **kwargs: Any) -> Any:
            return {
                "state": state,
                "attempts": 7,
                "delivery_snapshot_id": "snapshot",
                "delivery_snapshot_hash": "hash",
            }

        formal_request = evidence_request

        def claim_evidence_request(self, *args: Any, **kwargs: Any) -> None:
            return None

        claim_formal_request = claim_evidence_request

        def integration_baseline_evidence_by_snapshot(self, *args: Any) -> None:
            return None

        def fail_evidence_request(self, *args: Any, **kwargs: Any) -> object:
            mutations.append("released-winner")
            return object()

        fail_formal_request = fail_evidence_request

    repository = Repository()
    db = SimpleNamespace(begin_nested=nullcontext)
    dependencies = SimpleNamespace(
        engine=SimpleNamespace(connect=lambda: nullcontext(db), begin=lambda: nullcontext(db)),
        clock=SimpleNamespace(now=lambda: datetime(2026, 9, 4, tzinfo=UTC)),
        evidence_repository_factory=lambda _: repository,
        formal_repository_factory=lambda _: repository,
        requirement_formal_delivery=object(),
        gitlab_formal_merge_requests=object(),
        formal_review_routing=object(),
    )
    monkeypatch.setattr(
        batches,
        "_pending_process_candidates",
        lambda **_: (batches._ProcessCandidate(cast(Any, lane), "concurrent-candidate"),),
    )
    result = run_worker_once("process", limit=5, dependencies=cast(Any, dependencies))
    assert mutations == []
    assert (result.claimed, result.processed, result.released) == (0, 0, 0)
    assert result.effect_ids == result.error_codes == ()


@pytest.mark.parametrize("failure_at", ["snapshot", "claim"])
@pytest.mark.parametrize("state", ["RECEIVED", "PROCESSING"])
def test_evidence_preclaim_exception_never_mutates_pending_or_winner_lease(
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
    state: str,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    current = {
        "state": state,
        "attempts": 7,
        "available_at": datetime(2026, 9, 5, tzinfo=UTC),
        "delivery_snapshot_id": "snapshot",
        "delivery_snapshot_hash": "hash",
    }
    initial = dict(current)
    trace: list[str] = []

    class Repository:
        def evidence_request(self, *args: Any, **kwargs: Any) -> Any:
            return dict(current)

        def integration_baseline_evidence_by_snapshot(self, *args: Any) -> None:
            trace.append("snapshot")
            if failure_at == "snapshot":
                raise RuntimeError("private snapshot read failure")

        def claim_evidence_request(self, *args: Any, **kwargs: Any) -> None:
            trace.append("claim")
            raise RuntimeError("private claim failure")

        def fail_evidence_request(self, *args: Any, **kwargs: Any) -> Any:
            trace.append("fail")
            current.update(state="FAILED", attempts=8, available_at=kwargs["retry_at"])
            return dict(current)

    db = SimpleNamespace(begin_nested=nullcontext)
    repository = Repository()
    dependencies = SimpleNamespace(
        engine=SimpleNamespace(begin=lambda: nullcontext(db)),
        clock=SimpleNamespace(now=lambda: now),
        evidence_repository_factory=lambda _: repository,
        formal_repository_factory=object(),
        requirement_formal_delivery=object(),
        gitlab_formal_merge_requests=object(),
        formal_review_routing=object(),
    )
    monkeypatch.setattr(
        batches,
        "_pending_process_candidates",
        lambda **_: (batches._ProcessCandidate("evidence", "scanned-before-lease-change"),),
    )
    result = run_worker_once("process", limit=5, dependencies=cast(Any, dependencies))
    assert current == initial
    assert trace == (["snapshot"] if failure_at == "snapshot" else ["snapshot", "claim"])
    assert (result.claimed, result.processed, result.released) == (0, 0, 0)
    assert result.error_codes == ("CONNECTOR_UNAVAILABLE",)


def test_formal_failure_after_expiry_never_adopts_the_reclaimed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {"state": "PROCESSING", "attempts": 7}
    failed_attempts = []

    class Repository:
        def claim_formal_request(self, *args: Any, **kwargs: Any) -> Any:
            return dict(current)

        def formal_request(self, *args: Any, **kwargs: Any) -> Any:
            return dict(current)

        def fail_formal_request(self, *args: Any, expected_attempts: int, **kwargs: Any) -> Any:
            failed_attempts.append(expected_attempts)
            if current["attempts"] == expected_attempts:
                current["state"] = "FAILED"
                return dict(current)
            return None

    repository = Repository()
    dependencies = SimpleNamespace(
        engine=SimpleNamespace(begin=lambda: nullcontext(object())),
        clock=SimpleNamespace(now=lambda: datetime(2026, 9, 4, tzinfo=UTC)),
        evidence_repository_factory=object(),
        formal_repository_factory=lambda _: repository,
        requirement_formal_delivery=object(),
        gitlab_formal_merge_requests=object(),
        formal_review_routing=object(),
    )

    def expired(*args: Any, **kwargs: Any) -> Any:
        current["attempts"] = 8  # The original lease expired and a second worker claimed it.
        raise FormalDeliveryConflict("private provider failure")

    monkeypatch.setattr(formal, "_read_admission", expired)
    monkeypatch.setattr(
        batches,
        "_pending_process_candidates",
        lambda **_: (batches._ProcessCandidate("formal", "reclaimed"),),
    )
    result = run_worker_once("process", limit=5, dependencies=cast(Any, dependencies))
    assert current == {"state": "PROCESSING", "attempts": 8}
    assert failed_attempts == [7]
    assert (result.claimed, result.processed, result.released) == (1, 0, 0)
    assert result.error_codes == ("FORMAL_DELIVERY_CONFLICT",)


def test_real_worker_process_dispatches_all_five_lanes_with_one_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def done(identifier: str) -> Any:
        calls.append(identifier)
        return SimpleNamespace(
            effect=SimpleNamespace(id=identifier, last_error_code=None), blocked_reason=None
        )

    repository = SimpleNamespace(
        evidence_request=lambda *args, **kw: {"attempts": 0},
        pending_binding_request_ids=lambda **_: ["binding-1", "binding-2", "binding-extra"],
        pending_delivery_request_candidates=lambda **_: [
            {"message_id": "integration", "topic": "requirement.integration-merge.requested"}
        ],
        pending_webhook_ids=lambda **_: ["webhook"],
        pending_evidence_request_ids=lambda **_: ["evidence"],
        pending_formal_request_candidates=lambda **_: [{"message_id": "formal", "topic": "create"}],
    )
    db = SimpleNamespace(begin_nested=nullcontext)
    dependencies = SimpleNamespace(
        engine=SimpleNamespace(connect=lambda: nullcontext(db), begin=lambda: nullcontext(db)),
        clock=SimpleNamespace(now=lambda: datetime(2026, 9, 4, tzinfo=UTC)),
        repository_factory=lambda _: repository,
        delivery_repository_factory=lambda _: repository,
        evidence_repository_factory=lambda _: repository,
        formal_repository_factory=lambda _: repository,
        requirement_formal_delivery=object(),
        gitlab_formal_merge_requests=object(),
        formal_review_routing=object(),
    )
    monkeypatch.setattr(
        batches, "process_binding_candidate", lambda *, message_id, **_: done(message_id)
    )
    monkeypatch.setattr(
        batches, "process_integration_merge_candidate", lambda *, message_id, **_: done(message_id)
    )
    monkeypatch.setattr(batches, "process_webhook_candidate", lambda inbox_id, **_: done(inbox_id))
    # raising=False establishes a real RED before the dormant lanes are folded in.
    monkeypatch.setattr(
        batches,
        "process_integration_baseline_candidate",
        lambda _, *, message_id, **kw: done(message_id),
        raising=False,
    )
    monkeypatch.setattr(
        batches,
        "process_formal_delivery_candidate",
        lambda *, message_id, **_: done(message_id),
        raising=False,
    )
    report = run_worker_once("process", limit=6, dependencies=cast(Any, dependencies))
    assert calls == ["binding-1", "integration", "webhook", "evidence", "formal", "binding-2"]
    assert (report.claimed, report.processed, report.released) == (6, 6, 0)
    assert report.effect_ids == ("binding-1", "integration", "formal", "binding-2")


def test_worker_relay_and_reconciliation_reserve_all_lane_quotas_and_isolate_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def relay(name: str, limit: int) -> Any:
        calls.append((name, limit))
        if name == "binding":
            raise RuntimeError("private provider detail")
        return SimpleNamespace(claimed=limit, accepted=limit - 1, released=1)

    for name, function in [
        ("binding", "relay_binding_requests"),
        ("integration", "relay_integration_delivery_requests"),
        ("evidence", "relay_requirement_evidence_requests"),
        ("formal", "relay_requirement_formal_delivery_requests"),
    ]:
        monkeypatch.setattr(
            batches,
            function,
            lambda *, limit, dependencies, name=name: relay(name, limit),
            raising=False,
        )
    dependencies = SimpleNamespace(
        requirement_evidence=object(),
        evidence_repository_factory=object(),
        requirement_formal_delivery=object(),
        formal_repository_factory=object(),
        gitlab_formal_merge_requests=object(),
        formal_review_routing=object(),
    )
    result = run_worker_once("relay", limit=9, dependencies=cast(Any, dependencies))
    assert calls == [("binding", 3), ("integration", 2), ("evidence", 2), ("formal", 2)]
    assert (result.claimed, result.processed, result.released) == (6, 3, 3)
    assert result.error_codes == ("CONNECTOR_UNAVAILABLE",)
    calls.clear()

    def reconcile(name: str, limit: int) -> Any:
        calls.append((name, limit))
        if name == "branch":
            raise RuntimeError("private provider detail")
        effects = tuple(
            SimpleNamespace(id=f"{name}-{i}", last_error_code=None) for i in range(limit)
        )
        return effects if name == "formal" else SimpleNamespace(effects=effects)

    for name, function in [
        ("branch", "reconcile_due_effects"),
        ("integration", "reconcile_due_integration_effects"),
        ("formal", "reconcile_due_formal_effects"),
    ]:
        monkeypatch.setattr(
            batches,
            function,
            lambda *, limit, dependencies, name=name: reconcile(name, limit),
            raising=False,
        )
    result = run_worker_once("reconcile", limit=7, dependencies=cast(Any, dependencies))
    assert calls == [("branch", 3), ("integration", 2), ("formal", 2)]
    assert result.claimed == result.processed == 4
    assert result.error_codes == ("CONNECTOR_UNAVAILABLE",)
