from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text

from control_plane.app.modules.audit.adapters.sqlalchemy_repository import (
    SqlAlchemyAuditEventRepository,
)
from control_plane.app.modules.requirement import (
    ArtifactSnapshot,
    ArtifactState,
    ArtifactTrust,
    ArtifactUnavailable,
    CreateRequirementResult,
    DecisionOutcome,
    GateAlreadyDecided,
    GatePolicySnapshot,
    GateReviewerIneligible,
    GateReviewerMismatch,
    RequirementDependencies,
    RequirementDependencyUnavailable,
    RequirementDto,
    RequirementState,
    StaleBaselineSubject,
    StaleGateRevision,
    StaleRequirementRevision,
    WorkItemState,
    decide_baseline,
    get_requirement,
    reassign_baseline_gate,
    record_repository_binding,
    register_sdd_baseline,
    start_requirement_preparation,
    submit_baseline_confirmation,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_commands import (
    Actor,
    DurableDenialAudit,
    FailingAudit,
    _create,
    _dependencies,
)


@dataclass(frozen=True, slots=True)
class StaticArtifacts:
    available: bool = True
    trusted: bool = True
    sha256: str = "sha256:sdd-1"

    def get_snapshot(self, artifact_id: str, artifact_version: str) -> ArtifactSnapshot:
        return ArtifactSnapshot(
            id=artifact_id,
            version=artifact_version,
            sha256=self.sha256,
            state=(ArtifactState.AVAILABLE if self.available else ArtifactState.UNAVAILABLE),
            media_type="text/markdown",
            trust=(ArtifactTrust.TRUSTED_PLAIN_TEXT if self.trusted else ArtifactTrust.UNTRUSTED),
        )


@dataclass(frozen=True, slots=True)
class StaticGatePolicies:
    version: int = 7
    default_reviewer_id: str = "reviewer-1"
    policy_code: str = "REQUIREMENT_BASELINE_WORKSPACE_OWNER"
    snapshot_hash: str = "sha256:bdfadcc2d2c32fdb9fdf327d45a231cd2e5cb9bf3028f4e09d527fdb50dd8ea2"

    def requirement_baseline(self, *, workspace_id: str) -> GatePolicySnapshot:
        del workspace_id
        return GatePolicySnapshot(
            version=self.version,
            default_reviewer_id=self.default_reviewer_id,
            policy_code=self.policy_code,
            snapshot_hash=self.snapshot_hash,
        )


class FailingArtifacts:
    def get_snapshot(self, artifact_id: str, artifact_version: str) -> ArtifactSnapshot:
        del artifact_id, artifact_version
        raise RuntimeError("artifact token and internal endpoint must not escape")


@dataclass(frozen=True, slots=True)
class StaticReviewerGuard:
    allowed: bool = True

    def can_decide(self, *, actor_id: str, workspace_id: str) -> bool:
        del actor_id, workspace_id
        return self.allowed


def _gate_dependencies(
    *,
    artifact_available: bool = True,
    artifact_trusted: bool = True,
    artifact_hash: str = "sha256:sdd-1",
    reviewer_allowed: bool = True,
    denial_audit: DurableDenialAudit | None = None,
) -> RequirementDependencies:
    return replace(
        _dependencies(denial_audit=denial_audit),
        artifacts=StaticArtifacts(artifact_available, artifact_trusted, artifact_hash),
        gate_policies=StaticGatePolicies(),
        reviewer_guard=StaticReviewerGuard(reviewer_allowed),
    )


def _prepare(
    isolated: IsolatedRequirementDatabase,
) -> tuple[CreateRequirementResult, RequirementDto]:
    created = _create(isolated, idempotency_key="baseline-create")
    with isolated.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=created.requirement.id,
            expected_revision=1,
            actor=Actor("employee-1"),
            idempotency_key="baseline-start",
            dependencies=_gate_dependencies(),
        )
    return created, prepared


def test_approved_sdd_gate_advances_requirement_to_ready_without_readying_work_item(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="baseline-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="baseline-submit",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        decided = decide_baseline(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="The SDD is executable.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("reviewer-1"),
            idempotency_key="baseline-decide",
            dependencies=dependencies,
        )

    with isolated_requirement_database.owner.connect() as db:
        gate = db.execute(
            text(
                "SELECT requirement_version, artifact_id, artifact_version, artifact_hash, "
                "route_snapshot_version, route_snapshot_hash, policy_version, state "
                "FROM requirement.gate_instance WHERE id=:id"
            ),
            {"id": confirmation.gate.id},
        ).one()
        work_item = db.execute(
            text("SELECT state, repository_state FROM requirement.work_item WHERE id=:id"),
            {"id": created.work_item.id},
        ).one()

    assert confirmation.requirement.state is RequirementState.AWAITING_CONFIRMATION
    assert decided.requirement.state is RequirementState.READY
    assert gate == (
        1,
        "sdd-1",
        "version-1",
        "sha256:sdd-1",
        1,
        "sha256:route-1",
        7,
        "DECIDED",
    )
    assert work_item == ("DRAFT", "WAITING_REPOSITORY")


@pytest.mark.parametrize(
    "gate_policies",
    [
        StaticGatePolicies(policy_code=" "),
        StaticGatePolicies(snapshot_hash="sha256:not-a-canonical-digest"),
    ],
)
def test_invalid_gate_policy_snapshot_fails_closed_before_gate_is_saved(
    isolated_requirement_database: IsolatedRequirementDatabase,
    gate_policies: StaticGatePolicies,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = replace(_gate_dependencies(), gate_policies=gate_policies)
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="invalid-policy-register",
            dependencies=dependencies,
        )

    with pytest.raises(RequirementDependencyUnavailable):
        with isolated_requirement_database.runtime.begin() as db:
            submit_baseline_confirmation(
                db,
                requirement_id=created.requirement.id,
                sdd_baseline_id=baseline.baseline.id,
                expected_revision=baseline.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key=f"invalid-policy-{gate_policies.policy_code}",
                dependencies=dependencies,
            )

    with isolated_requirement_database.owner.connect() as db:
        gate_count = db.execute(text("SELECT count(*) FROM requirement.gate_instance")).scalar_one()
    assert gate_count == 0


def test_approved_sdd_gate_promotes_only_assigned_and_bound_work_item(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        bound = record_repository_binding(
            db,
            work_item_id=created.work_item.id,
            repository_id="repository-1",
            base_commit_sha="a" * 40,
            task_branch="work-items/gated-ready",
            expected_revision=created.work_item.revision,
            actor=Actor("SYSTEM"),
            idempotency_key="gated-ready-binding",
            correlation_id="source-control:effect:gated-ready-binding",
            dependencies=dependencies,
        )
    assert bound.state is WorkItemState.DRAFT
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="gated-ready-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="gated-ready-submit",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        decide_baseline(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="Bound plan is executable.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("reviewer-1"),
            idempotency_key="gated-ready-decide",
            dependencies=dependencies,
        )
    with isolated_requirement_database.owner.connect() as db:
        work_item = db.execute(
            text("SELECT state, revision FROM requirement.work_item WHERE id=:id"),
            {"id": created.work_item.id},
        ).one()
    assert work_item == ("READY", bound.revision + 1)


def test_gate_reassignment_supersedes_history_and_only_current_reviewer_can_decide(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="gate-reassign-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="gate-reassign-submit",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        reassigned = reassign_baseline_gate(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            reviewer_id="reviewer-2",
            reason="Current owner delegated this review.",
            expected_gate_revision=confirmation.gate.revision,
            actor=Actor("reviewer-1"),
            idempotency_key="gate-reassign-reviewer-2",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replay = reassign_baseline_gate(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            reviewer_id="reviewer-2",
            reason="Current owner delegated this review.",
            expected_gate_revision=confirmation.gate.revision,
            actor=Actor("reviewer-1"),
            idempotency_key="gate-reassign-reviewer-2",
            dependencies=dependencies,
        )
    assert replay == reassigned
    assert reassigned.gate.revision == confirmation.gate.revision + 1
    assert reassigned.assignment.default_reviewer_id == "reviewer-1"
    assert reassigned.assignment.current_reviewer_id == "reviewer-2"
    assert reassigned.assignment.revision == 2

    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(StaleGateRevision):
            reassign_baseline_gate(
                db,
                requirement_id=created.requirement.id,
                gate_id=confirmation.gate.id,
                reviewer_id="reviewer-3",
                reason="Stale reassignment.",
                expected_gate_revision=confirmation.gate.revision,
                actor=Actor("reviewer-1"),
                idempotency_key="gate-reassign-stale",
                dependencies=dependencies,
            )
    with isolated_requirement_database.runtime.begin() as db:
        with pytest.raises(GateReviewerMismatch):
            decide_baseline(
                db,
                requirement_id=created.requirement.id,
                gate_id=confirmation.gate.id,
                outcome=DecisionOutcome.APPROVED,
                reason="Old assignee must no longer decide.",
                expected_revision=confirmation.requirement.revision,
                actor=Actor("reviewer-1"),
                idempotency_key="gate-reassign-old-reviewer",
                dependencies=dependencies,
            )
    with isolated_requirement_database.runtime.begin() as db:
        decided = decide_baseline(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="Current assignee approved.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("reviewer-2"),
            idempotency_key="gate-reassign-current-reviewer",
            dependencies=dependencies,
        )
    with isolated_requirement_database.owner.connect() as db:
        history = db.execute(
            text(
                "SELECT current_reviewer_id, revision, superseded_at IS NULL "
                "FROM requirement.gate_assignment "
                "WHERE gate_instance_id=:id ORDER BY revision"
            ),
            {"id": confirmation.gate.id},
        ).all()
    with isolated_requirement_database.runtime.connect() as db:
        details = get_requirement(
            db,
            requirement_id=created.requirement.id,
            dependencies=dependencies,
        )
    assert decided.requirement.state is RequirementState.READY
    assert [tuple(row) for row in history] == [
        ("reviewer-1", 1, False),
        ("reviewer-2", 2, True),
    ]
    assert details.current_sdd_baseline is not None
    assert details.current_sdd_baseline.id == baseline.baseline.id
    assert details.current_gate is not None
    assert details.current_gate.id == confirmation.gate.id
    assert details.current_gate_assignment is not None
    assert details.current_gate_assignment.current_reviewer_id == "reviewer-2"
    assert details.current_decision is not None
    assert details.current_decision.reviewer_id == "reviewer-2"
    assert [item.assignee_id for item in details.work_item_assignments] == ["employee-1"]


@pytest.mark.parametrize(
    ("available", "trusted"),
    [(False, True), (True, False)],
)
def test_unavailable_or_untrusted_artifact_fails_closed_before_baseline_is_saved(
    isolated_requirement_database: IsolatedRequirementDatabase,
    available: bool,
    trusted: bool,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    denial_audit = DurableDenialAudit()
    with pytest.raises(ArtifactUnavailable):
        with isolated_requirement_database.runtime.begin() as db:
            register_sdd_baseline(
                db,
                requirement_id=created.requirement.id,
                artifact_id="sdd-1",
                artifact_version="version-1",
                expected_revision=prepared.revision,
                actor=Actor("employee-1"),
                idempotency_key="baseline-unavailable",
                dependencies=_gate_dependencies(
                    artifact_available=available,
                    artifact_trusted=trusted,
                    denial_audit=denial_audit,
                ),
            )
    with isolated_requirement_database.owner.connect() as db:
        count = db.execute(text("SELECT count(*) FROM requirement.sdd_baseline")).scalar_one()
    assert count == 0
    assert [event.action for event in denial_audit.events] == [
        "requirement.sdd_baseline.register_denied"
    ]
    assert denial_audit.events[0].result == "DENIED"


def test_stale_requirement_revision_cannot_register_a_baseline(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    with pytest.raises(StaleRequirementRevision):
        with isolated_requirement_database.runtime.begin() as db:
            register_sdd_baseline(
                db,
                requirement_id=created.requirement.id,
                artifact_id="sdd-1",
                artifact_version="version-1",
                expected_revision=prepared.revision - 1,
                actor=Actor("employee-1"),
                idempotency_key="baseline-stale",
                dependencies=_gate_dependencies(),
            )


def test_artifact_port_exception_fails_closed_with_safe_durable_audit(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    denial_audit = DurableDenialAudit()
    dependencies = replace(
        _gate_dependencies(denial_audit=denial_audit),
        artifacts=FailingArtifacts(),
    )
    with pytest.raises(RequirementDependencyUnavailable):
        with isolated_requirement_database.runtime.begin() as db:
            register_sdd_baseline(
                db,
                requirement_id=created.requirement.id,
                artifact_id="sdd-1",
                artifact_version="version-1",
                expected_revision=prepared.revision,
                actor=Actor("employee-1"),
                idempotency_key="artifact-port-failure",
                dependencies=dependencies,
            )

    assert len(denial_audit.events) == 1
    assert denial_audit.events[0].reason == "reasonCode=REQUIREMENTDEPENDENCYUNAVAILABLE"
    assert "token" not in (denial_audit.events[0].reason or "")


def test_postgresql_denial_audit_commits_while_requirement_transaction_rolls_back(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = replace(
        _gate_dependencies(artifact_available=False),
        denial_audit=SqlAlchemyAuditEventRepository(isolated_requirement_database.owner),
    )
    with pytest.raises(ArtifactUnavailable):
        with isolated_requirement_database.runtime.begin() as db:
            register_sdd_baseline(
                db,
                requirement_id=created.requirement.id,
                artifact_id="sdd-1",
                artifact_version="version-1",
                expected_revision=prepared.revision,
                actor=Actor("employee-1"),
                idempotency_key="postgres-denial-audit",
                dependencies=dependencies,
            )

    with isolated_requirement_database.owner.connect() as db:
        baseline_count = db.execute(
            text("SELECT count(*) FROM requirement.sdd_baseline")
        ).scalar_one()
        denied = db.execute(
            text(
                "SELECT action, result, reason FROM audit.audit_event "
                "WHERE target_id=:target_id "
                "AND action='requirement.sdd_baseline.register_denied'"
            ),
            {"target_id": created.requirement.id},
        ).one()

    assert baseline_count == 0
    assert denied == (
        "requirement.sdd_baseline.register_denied",
        "DENIED",
        "reasonCode=ARTIFACTUNAVAILABLE",
    )


def test_only_latest_registered_sdd_baseline_can_be_submitted(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    first_dependencies = _gate_dependencies(artifact_hash="sha256:sdd-1")
    with isolated_requirement_database.runtime.begin() as db:
        first = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="current-register-1",
            dependencies=first_dependencies,
        )
    second_dependencies = _gate_dependencies(artifact_hash="sha256:sdd-2")
    with isolated_requirement_database.runtime.begin() as db:
        second = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-2",
            expected_revision=first.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="current-register-2",
            dependencies=second_dependencies,
        )

    with pytest.raises(StaleBaselineSubject):
        with isolated_requirement_database.runtime.begin() as db:
            submit_baseline_confirmation(
                db,
                requirement_id=created.requirement.id,
                sdd_baseline_id=first.baseline.id,
                expected_revision=second.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="current-submit-stale",
                dependencies=second_dependencies,
            )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=second.baseline.id,
            expected_revision=second.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="current-submit-latest",
            dependencies=second_dependencies,
        )

    assert confirmation.gate.sdd_baseline_id == second.baseline.id


def test_current_reviewer_cannot_decide_after_realtime_eligibility_is_lost(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="eligibility-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="eligibility-submit",
            dependencies=dependencies,
        )
    with pytest.raises(GateReviewerIneligible):
        with isolated_requirement_database.runtime.begin() as db:
            decide_baseline(
                db,
                requirement_id=created.requirement.id,
                gate_id=confirmation.gate.id,
                outcome=DecisionOutcome.APPROVED,
                reason="No longer eligible.",
                expected_revision=confirmation.requirement.revision,
                actor=Actor("reviewer-1"),
                idempotency_key="eligibility-decide",
                dependencies=_gate_dependencies(reviewer_allowed=False),
            )


def test_non_current_reviewer_cannot_decide(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="reviewer-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="reviewer-submit",
            dependencies=dependencies,
        )
    with pytest.raises(GateReviewerMismatch):
        with isolated_requirement_database.runtime.begin() as db:
            decide_baseline(
                db,
                requirement_id=created.requirement.id,
                gate_id=confirmation.gate.id,
                outcome=DecisionOutcome.APPROVED,
                reason="I am not assigned.",
                expected_revision=confirmation.requirement.revision,
                actor=Actor("reviewer-2"),
                idempotency_key="reviewer-decide",
                dependencies=dependencies,
            )


def test_reassignment_that_locks_gate_then_assignment_prevents_stale_reviewer_decision(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="concurrent-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="concurrent-submit",
            dependencies=dependencies,
        )

    assignment_changed = Event()
    allow_reassignment_commit = Event()

    def reassign() -> None:
        with isolated_requirement_database.runtime.begin() as db:
            db.execute(
                text("SELECT id FROM requirement.gate_instance WHERE id=:id FOR UPDATE"),
                {"id": confirmation.gate.id},
            ).one()
            current = (
                db.execute(
                    text(
                        "SELECT * FROM requirement.gate_assignment "
                        "WHERE gate_instance_id=:id AND superseded_at IS NULL FOR UPDATE"
                    ),
                    {"id": confirmation.gate.id},
                )
                .mappings()
                .one()
            )
            db.execute(
                text("UPDATE requirement.gate_assignment SET superseded_at=now() WHERE id=:id"),
                {"id": current["id"]},
            )
            db.execute(
                text(
                    "INSERT INTO requirement.gate_assignment "
                    "(id, gate_instance_id, default_reviewer_id, current_reviewer_id, "
                    "revision) VALUES (:id, :gate_id, 'reviewer-1', 'reviewer-2', 2)"
                ),
                {"id": str(uuid4()), "gate_id": confirmation.gate.id},
            )
            assignment_changed.set()
            assert allow_reassignment_commit.wait(timeout=5)

    def decide_as_stale_reviewer() -> None:
        with isolated_requirement_database.runtime.begin() as db:
            decide_baseline(
                db,
                requirement_id=created.requirement.id,
                gate_id=confirmation.gate.id,
                outcome=DecisionOutcome.APPROVED,
                reason="The old reviewer must not sign.",
                expected_revision=confirmation.requirement.revision,
                actor=Actor("reviewer-1"),
                idempotency_key="concurrent-stale-decision",
                dependencies=dependencies,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        reassignment = pool.submit(reassign)
        assert assignment_changed.wait(timeout=5)
        stale_decision = pool.submit(decide_as_stale_reviewer)
        allow_reassignment_commit.set()
        reassignment.result(timeout=5)
        with pytest.raises(GateReviewerMismatch):
            stale_decision.result(timeout=5)

    with isolated_requirement_database.owner.connect() as db:
        decision_count = db.execute(text("SELECT count(*) FROM requirement.decision")).scalar_one()
    assert decision_count == 0


def test_decided_gate_rejects_a_second_decision(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="repeat-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="repeat-submit",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        decided = decide_baseline(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="Approved once.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("reviewer-1"),
            idempotency_key="repeat-first-decision",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        replayed = decide_baseline(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="Approved once.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("reviewer-1"),
            idempotency_key="repeat-first-decision",
            dependencies=dependencies,
        )
    assert replayed == decided
    with pytest.raises(GateAlreadyDecided):
        with isolated_requirement_database.runtime.begin() as db:
            decide_baseline(
                db,
                requirement_id=created.requirement.id,
                gate_id=confirmation.gate.id,
                outcome=DecisionOutcome.APPROVED,
                reason="A duplicate decision.",
                expected_revision=decided.requirement.revision,
                actor=Actor("reviewer-1"),
                idempotency_key="repeat-second-decision",
                dependencies=dependencies,
            )

    with isolated_requirement_database.owner.connect() as db:
        count = db.execute(text("SELECT count(*) FROM requirement.decision")).scalar_one()
    assert count == 1


def test_decision_gate_and_requirement_roll_back_together_when_audit_fails(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="rollback-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="rollback-submit",
            dependencies=dependencies,
        )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        with isolated_requirement_database.runtime.begin() as db:
            decide_baseline(
                db,
                requirement_id=created.requirement.id,
                gate_id=confirmation.gate.id,
                outcome=DecisionOutcome.APPROVED,
                reason="This transaction must roll back.",
                expected_revision=confirmation.requirement.revision,
                actor=Actor("reviewer-1"),
                idempotency_key="rollback-decision",
                dependencies=replace(dependencies, audit=FailingAudit()),
            )

    with isolated_requirement_database.owner.connect() as db:
        decision_count = db.execute(text("SELECT count(*) FROM requirement.decision")).scalar_one()
        gate_state = db.execute(
            text("SELECT state, revision FROM requirement.gate_instance WHERE id=:id"),
            {"id": confirmation.gate.id},
        ).one()
        requirement_state = db.execute(
            text("SELECT state, revision FROM requirement.requirement WHERE id=:id"),
            {"id": created.requirement.id},
        ).one()
        idempotency_count = db.execute(
            text(
                "SELECT count(*) FROM requirement.idempotency_record "
                "WHERE operation='requirement_decide_baseline' "
                "AND idempotency_key='rollback-decision'"
            )
        ).scalar_one()

    assert decision_count == 0
    assert gate_state == ("OPEN", 1)
    assert requirement_state == (
        RequirementState.AWAITING_CONFIRMATION.value,
        confirmation.requirement.revision,
    )
    assert idempotency_count == 0


def test_changed_artifact_hash_is_rejected_and_new_version_creates_a_new_gate(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    original = _gate_dependencies(artifact_hash="sha256:sdd-1")
    with isolated_requirement_database.runtime.begin() as db:
        baseline_1 = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key="changed-register-1",
            dependencies=original,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation_1 = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline_1.baseline.id,
            expected_revision=baseline_1.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="changed-submit-1",
            dependencies=original,
        )
    with isolated_requirement_database.runtime.begin() as db:
        changes_requested = decide_baseline(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation_1.gate.id,
            outcome=DecisionOutcome.CHANGES_REQUESTED,
            reason="Update the design.",
            expected_revision=confirmation_1.requirement.revision,
            actor=Actor("reviewer-1"),
            idempotency_key="changed-decision-1",
            dependencies=original,
        )

    with pytest.raises(StaleBaselineSubject):
        with isolated_requirement_database.runtime.begin() as db:
            submit_baseline_confirmation(
                db,
                requirement_id=created.requirement.id,
                sdd_baseline_id=baseline_1.baseline.id,
                expected_revision=changes_requested.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="changed-resubmit-old-baseline",
                dependencies=original,
            )

    changed_hash = _gate_dependencies(artifact_hash="sha256:mutated")
    with pytest.raises(ArtifactUnavailable):
        with isolated_requirement_database.runtime.begin() as db:
            register_sdd_baseline(
                db,
                requirement_id=created.requirement.id,
                artifact_id="sdd-1",
                artifact_version="version-1",
                expected_revision=changes_requested.requirement.revision,
                actor=Actor("employee-1"),
                idempotency_key="changed-register-mutated",
                dependencies=changed_hash,
            )

    changed_version = _gate_dependencies(artifact_hash="sha256:sdd-2")
    with isolated_requirement_database.runtime.begin() as db:
        baseline_2 = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-2",
            expected_revision=changes_requested.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="changed-register-2",
            dependencies=changed_version,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation_2 = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline_2.baseline.id,
            expected_revision=baseline_2.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="changed-submit-2",
            dependencies=changed_version,
        )

    assert baseline_2.baseline.id != baseline_1.baseline.id
    assert baseline_2.baseline.artifact_version == "version-2"
    assert baseline_2.baseline.artifact_hash == "sha256:sdd-2"
    assert confirmation_2.gate.id != confirmation_1.gate.id
    assert confirmation_2.gate.artifact_hash == "sha256:sdd-2"


@pytest.mark.parametrize(
    ("outcome", "expected", "expected_work_item_state"),
    [
        (DecisionOutcome.CHANGES_REQUESTED, RequirementState.PREPARING, "DRAFT"),
        (DecisionOutcome.REJECTED, RequirementState.CANCELED, "CANCELED"),
    ],
)
def test_non_approval_decisions_follow_the_first_batch_state_machine(
    isolated_requirement_database: IsolatedRequirementDatabase,
    outcome: DecisionOutcome,
    expected: RequirementState,
    expected_work_item_state: str,
) -> None:
    created, prepared = _prepare(isolated_requirement_database)
    dependencies = _gate_dependencies()
    with isolated_requirement_database.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=created.requirement.id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=prepared.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"{outcome.value}-register",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=created.requirement.id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"{outcome.value}-submit",
            dependencies=dependencies,
        )
    with isolated_requirement_database.runtime.begin() as db:
        result = decide_baseline(
            db,
            requirement_id=created.requirement.id,
            gate_id=confirmation.gate.id,
            outcome=outcome,
            reason="Human decision.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("reviewer-1"),
            idempotency_key=f"{outcome.value}-decide",
            dependencies=dependencies,
        )

    with isolated_requirement_database.owner.connect() as db:
        work_item_state = db.execute(
            text("SELECT state FROM requirement.work_item WHERE id=:id"),
            {"id": created.work_item.id},
        ).scalar_one()

    assert result.requirement.state is expected
    assert work_item_state == expected_work_item_state
