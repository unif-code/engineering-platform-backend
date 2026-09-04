from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from control_plane.app.modules.source_control import (
    EvidenceStale,
    FormalDeliveryConflict,
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    accept_external_validation,
    accept_formal_delivery_request,
    accept_integration_baseline_request,
    process_due_source_control_inboxes,
    reconcile_due_source_control_effects,
    relay_due_source_control_requests,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlEvidenceRepository,
    SqlAlchemySourceControlIntegrationRepository,
)
from control_plane.app.modules.source_control.application import batches
from control_plane.app.modules.source_control.application._batch_claim import InboxProcessingFailed
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_v06_evidence_application import (
    _request,
    _seed_merged_integration,
    _validation,
)
from tests.source_control.test_v06_formal_application import (
    FakeFormalGitLab,
    FakeRequirementFormalDelivery,
    _admission,
    _envelope,
)
from tests.source_control.test_v06_formal_application import (
    _dependencies as _formal_dependencies,
)


@pytest.fixture(autouse=True)
def idle_legacy_relay_and_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests target evidence/formal persistence; old lane behavior has its own tests.
    for name in ("relay_binding_requests", "relay_integration_delivery_requests"):
        monkeypatch.setattr(
            batches, name, lambda **kw: SimpleNamespace(claimed=0, accepted=0, released=0)
        )
    for name in ("reconcile_due_effects", "reconcile_due_integration_effects"):
        monkeypatch.setattr(batches, name, lambda **kw: SimpleNamespace(effects=()))


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


class _EvidenceRequestRepository:
    def evidence_request(self, *args: Any, **kwargs: Any) -> Any:
        return {"attempts": 0}


class _FixedClock:
    def now(self) -> datetime:
        return NOW


class _ConnectionContext:
    def __enter__(self) -> "_FakeConnection":
        return _FakeConnection()

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeConnection:
    def begin_nested(self) -> object:
        return nullcontext()


class _FakeEngine:
    def __init__(self) -> None:
        self.opens = 0

    def connect(self) -> _ConnectionContext:
        self.opens += 1
        return _ConnectionContext()

    def begin(self) -> _ConnectionContext:
        self.opens += 1
        return _ConnectionContext()


def _unit_dependencies(**overrides: object) -> SourceControlDependencies:
    values: dict[str, object] = {
        "engine": _FakeEngine(),
        "repository_factory": lambda _db: SimpleNamespace(
            pending_binding_request_ids=lambda **kw: [], pending_webhook_ids=lambda **kw: []
        ),
        "delivery_repository_factory": lambda _db: SimpleNamespace(
            pending_delivery_request_candidates=lambda **kw: []
        ),
        "clock": _FixedClock(),
        "requirement_evidence": object(),
        "evidence_repository_factory": object(),
        "requirement_formal_delivery": object(),
        "formal_repository_factory": object(),
        "gitlab_formal_merge_requests": object(),
        "formal_review_routing": object(),
    }
    values.update(overrides)
    return cast(SourceControlDependencies, SimpleNamespace(**values))


@pytest.mark.parametrize(
    "missing",
    [
        "requirement_evidence",
        "evidence_repository_factory",
        "requirement_formal_delivery",
        "formal_repository_factory",
    ],
)
def test_v06_relay_fails_closed_before_either_lane_when_a_dependency_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    called: list[str] = []

    def unexpected_lane(**_kwargs: object) -> object:
        called.append("lane")
        return SimpleNamespace(claimed=0, accepted=0, released=0)

    monkeypatch.setattr(batches, "relay_requirement_evidence_requests", unexpected_lane)
    monkeypatch.setattr(
        batches,
        "relay_requirement_formal_delivery_requests",
        unexpected_lane,
    )

    with pytest.raises(SourceControlDependencyUnavailable):
        relay_due_source_control_requests(
            limit=5,
            dependencies=_unit_dependencies(**{missing: None}),
        )

    assert called == []


@pytest.mark.parametrize(
    "missing",
    [
        "evidence_repository_factory",
        "formal_repository_factory",
        "requirement_formal_delivery",
        "gitlab_formal_merge_requests",
        "formal_review_routing",
    ],
)
def test_v06_process_fails_closed_before_scanning_when_a_dependency_is_missing(
    missing: str,
) -> None:
    engine = _FakeEngine()

    with pytest.raises(SourceControlDependencyUnavailable):
        process_due_source_control_inboxes(
            limit=5,
            dependencies=_unit_dependencies(
                engine=engine,
                **{missing: None},
            ),
        )

    assert engine.opens == 0


def test_v06_relay_reserves_a_fair_budget_for_evidence_and_formal_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def evidence(*, limit: int, dependencies: SourceControlDependencies) -> object:
        calls.append(("evidence", limit))
        return SimpleNamespace(claimed=limit, accepted=limit - 1, released=1)

    def formal(*, limit: int, dependencies: SourceControlDependencies) -> object:
        calls.append(("formal", limit))
        return SimpleNamespace(claimed=limit, accepted=limit, released=0)

    monkeypatch.setattr(batches, "relay_requirement_evidence_requests", evidence)
    monkeypatch.setattr(
        batches,
        "relay_requirement_formal_delivery_requests",
        formal,
    )

    result = relay_due_source_control_requests(limit=10, dependencies=_unit_dependencies())

    assert calls == [("evidence", 2), ("formal", 2)]
    assert (result.claimed, result.processed, result.released) == (4, 3, 1)


def test_v06_process_round_robins_repository_candidates_within_lane_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans: list[tuple[str, int]] = []
    processed: list[str] = []

    class EvidenceRepository(_EvidenceRequestRepository):
        def pending_evidence_request_ids(
            self,
            *,
            limit: int,
            now: datetime,
        ) -> list[str]:
            assert now == NOW
            scans.append(("evidence", limit))
            return ["evidence-1", "evidence-2", "evidence-3", "evidence-extra"]

    class FormalRepository:
        def pending_formal_request_candidates(
            self,
            *,
            limit: int,
            now: datetime,
        ) -> list[dict[str, str]]:
            assert now == NOW
            scans.append(("formal", limit))
            return [
                {"message_id": "formal-1", "topic": "create"},
                {"message_id": "formal-2", "topic": "merge"},
                {"message_id": "formal-extra", "topic": "create"},
            ]

    def process_evidence(
        repository: object,
        *,
        message_id: str,
        generated_by: str,
        dependencies: SourceControlDependencies,
    ) -> object:
        assert isinstance(repository, EvidenceRepository)
        assert generated_by == "SYSTEM:SOURCE_CONTROL"
        processed.append(message_id)
        return object()

    def process_formal(
        *,
        message_id: str,
        dependencies: SourceControlDependencies,
    ) -> object:
        processed.append(message_id)
        return SimpleNamespace(
            effect=SimpleNamespace(id=f"effect-{message_id}", last_error_code=None)
        )

    monkeypatch.setattr(batches, "process_integration_baseline_candidate", process_evidence)
    monkeypatch.setattr(batches, "process_formal_delivery_candidate", process_formal)
    dependencies = _unit_dependencies(
        evidence_repository_factory=lambda _db: EvidenceRepository(),
        formal_repository_factory=lambda _db: FormalRepository(),
    )

    result = process_due_source_control_inboxes(limit=15, dependencies=dependencies)

    assert scans == [("evidence", 3), ("formal", 3)]
    assert processed == [
        "evidence-1",
        "formal-1",
        "evidence-2",
        "formal-2",
        "evidence-3",
        "formal-extra",
    ]
    assert (result.claimed, result.processed, result.released) == (6, 6, 0)
    assert result.effect_ids == ("effect-formal-1", "effect-formal-2", "effect-formal-extra")


def test_v06_process_isolates_a_poison_candidate_and_continues_both_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed: list[str] = []
    failures: list[tuple[str, str, datetime]] = []

    class EvidenceRepository(_EvidenceRequestRepository):
        def pending_evidence_request_ids(
            self,
            *,
            limit: int,
            now: datetime,
        ) -> list[str]:
            return ["evidence-poison", "evidence-good"][:limit]

        def fail_evidence_request(
            self,
            message_id: str,
            *,
            expected_attempts: int,
            now: datetime,
            retry_at: datetime,
            error_code: str,
        ) -> object:
            assert expected_attempts == 0
            failures.append((message_id, error_code, retry_at))
            return object()

        def evidence_request(
            self,
            message_id: str,
            *,
            for_update: bool = False,
        ) -> dict[str, int]:
            assert for_update is True
            return {"attempts": 0}

    class FormalRepository:
        def pending_formal_request_candidates(
            self,
            *,
            limit: int,
            now: datetime,
        ) -> list[dict[str, str]]:
            return [{"message_id": "formal-good", "topic": "create"}][:limit]

    def process_evidence(
        repository: object,
        *,
        message_id: str,
        generated_by: str,
        dependencies: SourceControlDependencies,
    ) -> object:
        processed.append(message_id)
        if message_id == "evidence-poison":
            raise InboxProcessingFailed(
                EvidenceStale("poison details must not be persisted"), expected_attempts=1
            )
        return object()

    def process_formal(
        *,
        message_id: str,
        dependencies: SourceControlDependencies,
    ) -> object:
        processed.append(message_id)
        return SimpleNamespace(
            effect=SimpleNamespace(id=f"effect-{message_id}", last_error_code=None)
        )

    monkeypatch.setattr(batches, "process_integration_baseline_candidate", process_evidence)
    monkeypatch.setattr(batches, "process_formal_delivery_candidate", process_formal)
    dependencies = _unit_dependencies(
        evidence_repository_factory=lambda _db: EvidenceRepository(),
        formal_repository_factory=lambda _db: FormalRepository(),
    )

    result = process_due_source_control_inboxes(limit=10, dependencies=dependencies)

    assert processed == ["evidence-poison", "formal-good", "evidence-good"]
    assert failures == [("evidence-poison", "EVIDENCE_STALE", NOW + timedelta(seconds=5))]
    assert (result.claimed, result.processed, result.released) == (3, 2, 1)
    assert result.effect_ids == ("effect-formal-good",)
    assert result.error_codes == ("EVIDENCE_STALE",)


def test_v06_process_persists_a_formal_failure_and_continues_the_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed: list[str] = []
    failures: list[tuple[str, str, datetime]] = []

    class EvidenceRepository(_EvidenceRequestRepository):
        def pending_evidence_request_ids(
            self,
            *,
            limit: int,
            now: datetime,
        ) -> list[str]:
            return ["evidence-good"][:limit]

    class FormalRepository:
        def pending_formal_request_candidates(
            self,
            *,
            limit: int,
            now: datetime,
        ) -> list[dict[str, str]]:
            return [
                {"message_id": "formal-poison", "topic": "create"},
                {"message_id": "formal-good", "topic": "create"},
            ][:limit]

        def formal_request(
            self,
            message_id: str,
            *,
            for_update: bool = False,
        ) -> dict[str, int]:
            assert for_update is True
            return {"attempts": 1}

        def fail_formal_request(
            self,
            message_id: str,
            *,
            expected_attempts: int,
            now: datetime,
            retry_at: datetime,
            error_code: str,
        ) -> object:
            assert expected_attempts == 1
            failures.append((message_id, error_code, retry_at))
            return object()

    def process_evidence(
        repository: object,
        *,
        message_id: str,
        generated_by: str,
        dependencies: SourceControlDependencies,
    ) -> object:
        processed.append(message_id)
        return object()

    def process_formal(
        *,
        message_id: str,
        dependencies: SourceControlDependencies,
    ) -> object:
        processed.append(message_id)
        if message_id == "formal-poison":
            raise InboxProcessingFailed(
                FormalDeliveryConflict("private provider details"), expected_attempts=1
            )
        return SimpleNamespace(
            effect=SimpleNamespace(id=f"effect-{message_id}", last_error_code=None)
        )

    monkeypatch.setattr(batches, "process_integration_baseline_candidate", process_evidence)
    monkeypatch.setattr(batches, "process_formal_delivery_candidate", process_formal)
    dependencies = _unit_dependencies(
        evidence_repository_factory=lambda _db: EvidenceRepository(),
        formal_repository_factory=lambda _db: FormalRepository(),
    )

    result = process_due_source_control_inboxes(limit=10, dependencies=dependencies)

    assert processed == ["evidence-good", "formal-poison", "formal-good"]
    assert failures == [("formal-poison", "FORMAL_DELIVERY_CONFLICT", NOW + timedelta(seconds=5))]
    assert (result.claimed, result.processed, result.released) == (3, 2, 1)
    assert result.effect_ids == ("effect-formal-good",)
    assert result.error_codes == ("FORMAL_DELIVERY_CONFLICT",)


def test_v06_process_reports_a_terminal_formal_block_without_an_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EvidenceRepository(_EvidenceRequestRepository):
        def pending_evidence_request_ids(
            self,
            *,
            limit: int,
            now: datetime,
        ) -> list[str]:
            return ["evidence-good"][:limit]

    class FormalRepository:
        def pending_formal_request_candidates(
            self,
            *,
            limit: int,
            now: datetime,
        ) -> list[dict[str, str]]:
            return [{"message_id": "formal-blocked", "topic": "create"}][:limit]

    monkeypatch.setattr(
        batches,
        "process_integration_baseline_candidate",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        batches,
        "process_formal_delivery_candidate",
        lambda **_kwargs: SimpleNamespace(
            effect=None,
            blocked_reason="HEAD_SHA_CHANGED",
        ),
    )
    dependencies = _unit_dependencies(
        evidence_repository_factory=lambda _db: EvidenceRepository(),
        formal_repository_factory=lambda _db: FormalRepository(),
    )

    result = process_due_source_control_inboxes(limit=5, dependencies=dependencies)

    assert (result.claimed, result.processed, result.released) == (2, 2, 0)
    assert result.effect_ids == ()
    assert result.error_codes == ("HEAD_SHA_CHANGED",)


def test_v06_reconciliation_delegates_formal_recovery_and_reports_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def reconcile(*, limit: int, dependencies: SourceControlDependencies) -> tuple[Any, ...]:
        calls.append(limit)
        return (
            SimpleNamespace(id="effect-1", last_error_code=None),
            SimpleNamespace(id="effect-2", last_error_code="EXTERNAL_RESULT_UNKNOWN"),
        )

    monkeypatch.setattr(batches, "reconcile_due_formal_effects", reconcile)

    result = reconcile_due_source_control_effects(
        limit=9,
        dependencies=_unit_dependencies(),
    )

    assert calls == [3]
    assert (result.claimed, result.processed, result.released) == (2, 2, 0)
    assert result.effect_ids == ("effect-1", "effect-2")
    assert result.error_codes == ("EXTERNAL_RESULT_UNKNOWN",)


@pytest.mark.parametrize(
    "missing",
    [
        "formal_repository_factory",
        "requirement_formal_delivery",
        "gitlab_formal_merge_requests",
        "formal_review_routing",
    ],
)
def test_v06_reconciliation_fails_closed_when_a_dependency_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.setattr(
        batches,
        "reconcile_due_formal_effects",
        lambda *, limit, dependencies: (),
    )
    with pytest.raises(SourceControlDependencyUnavailable):
        reconcile_due_source_control_effects(
            limit=3,
            dependencies=_unit_dependencies(**{missing: None}),
        )


@pytest.mark.integration
def test_v06_processes_real_evidence_and_formal_repository_candidates(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    dependencies = replace(
        _formal_dependencies(isolated_source_control_database, requirement, gitlab),
        evidence_repository_factory=SqlAlchemySourceControlEvidenceRepository,
        delivery_repository_factory=SqlAlchemySourceControlIntegrationRepository,
    )
    validation = _validation(message_id="93000000-0000-0000-0000-000000000681")
    evidence_request = _request(message_id="94000000-0000-0000-0000-000000000681")
    formal_request = _envelope(message_id="91000000-0000-0000-0000-000000000681")
    with isolated_source_control_database.runtime.begin() as db:
        accept_external_validation(db, validation, dependencies=dependencies)
    with isolated_source_control_database.runtime.begin() as db:
        accept_integration_baseline_request(db, evidence_request, dependencies=dependencies)
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(db, formal_request, dependencies=dependencies)

    result = process_due_source_control_inboxes(limit=5, dependencies=dependencies)

    with isolated_source_control_database.owner.connect() as db:
        evidence_state = db.execute(
            text(
                "SELECT state FROM source_control.evidence_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": evidence_request.message_id},
        ).scalar_one()
        formal_state = db.execute(
            text(
                "SELECT state FROM source_control.formal_delivery_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": formal_request.message_id},
        ).scalar_one()
        evidence_count = db.execute(
            text("SELECT count(*) FROM source_control.integration_baseline_evidence")
        ).scalar_one()
        formal_binding_count = db.execute(
            text("SELECT count(*) FROM source_control.merge_request_binding WHERE kind='FORMAL'")
        ).scalar_one()

    assert (result.claimed, result.processed) == (2, 2)
    assert len(result.effect_ids) == 1
    assert result.error_codes == ()
    assert (evidence_state, formal_state) == ("PROCESSED", "PROCESSED")
    assert (evidence_count, formal_binding_count) == (1, 1)
    assert gitlab.created == 1
    assert len(requirement.ready) == 1


@pytest.mark.integration
def test_v06_persists_failed_evidence_with_backoff_and_processes_formal(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed_merged_integration(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    dependencies = replace(
        _formal_dependencies(isolated_source_control_database, requirement, gitlab),
        evidence_repository_factory=SqlAlchemySourceControlEvidenceRepository,
        delivery_repository_factory=SqlAlchemySourceControlIntegrationRepository,
    )
    evidence_request = _request(message_id="94000000-0000-0000-0000-000000000682").model_copy(
        update={
            "work_item_ids": (
                "50000000-0000-0000-0000-000000000301",
                "50000000-0000-0000-0000-000000000399",
            )
        }
    )
    formal_request = _envelope(message_id="91000000-0000-0000-0000-000000000682")
    with isolated_source_control_database.runtime.begin() as db:
        accept_integration_baseline_request(db, evidence_request, dependencies=dependencies)
        accept_formal_delivery_request(db, formal_request, dependencies=dependencies)

    result = process_due_source_control_inboxes(limit=5, dependencies=dependencies)

    with isolated_source_control_database.owner.connect() as db:
        evidence_state = db.execute(
            text(
                "SELECT state, attempts, available_at, last_error_code "
                "FROM source_control.evidence_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": evidence_request.message_id},
        ).one()
        formal_state = db.execute(
            text(
                "SELECT state FROM source_control.formal_delivery_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": formal_request.message_id},
        ).scalar_one()

    assert tuple(evidence_state) == (
        "FAILED",
        1,
        dependencies.clock.now() + timedelta(seconds=5),
        "EVIDENCE_UNAVAILABLE",
    )
    assert formal_state == "PROCESSED"
    assert (result.claimed, result.processed, result.released) == (2, 1, 1)
    assert result.error_codes == ("EVIDENCE_UNAVAILABLE",)
    assert gitlab.created == 1


def test_evidence_failure_keeps_row_lock_across_savepoint_rollback(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = isolated_source_control_database
    _seed_merged_integration(source)
    dependencies = replace(
        _formal_dependencies(
            source, FakeRequirementFormalDelivery(_admission()), FakeFormalGitLab()
        ),
        evidence_repository_factory=SqlAlchemySourceControlEvidenceRepository,
        delivery_repository_factory=SqlAlchemySourceControlIntegrationRepository,
    )
    request = _request(message_id="94000000-0000-0000-0000-000000000689")
    with source.runtime.begin() as db:
        accept_integration_baseline_request(db, request, dependencies=dependencies)
    original = SqlAlchemySourceControlEvidenceRepository.fail_evidence_request
    observed = []

    def fail_with_competing_worker(self: Any, message_id: str, **kwargs: Any) -> Any:
        with pytest.raises(OperationalError) as blocked:
            with source.runtime.begin() as competitor:
                competitor.execute(
                    text(
                        "SELECT message_id FROM source_control.evidence_request_inbox "
                        "WHERE message_id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": message_id},
                )
        assert cast(Any, blocked.value.orig).sqlstate == "55P03"
        observed.append(kwargs["expected_attempts"])
        return original(self, message_id, **kwargs)

    monkeypatch.setattr(
        SqlAlchemySourceControlEvidenceRepository,
        "fail_evidence_request",
        fail_with_competing_worker,
    )
    result = process_due_source_control_inboxes(limit=5, dependencies=dependencies)
    assert observed == [0]
    assert (result.claimed, result.processed, result.released) == (1, 0, 1)
    with source.runtime.connect() as db:
        state, attempts = db.execute(
            text(
                "SELECT state, attempts FROM source_control.evidence_request_inbox "
                "WHERE message_id=:id"
            ),
            {"id": request.message_id},
        ).one()
    assert (state, attempts) == ("FAILED", 1)
    assert result.error_codes == ("EVIDENCE_UNAVAILABLE",)
