from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest
from sqlalchemy import text

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.source_control import SourceControlDependencies
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlFormalRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.application.formal import (
    accept_formal_delivery_request,
    process_formal_delivery_request,
    reconcile_formal_delivery_effect,
)
from control_plane.app.modules.source_control.domain import (
    EffectState,
    FormalDeliveryRequestEnvelope,
    FormalDeliveryRequestKind,
    FormalReviewRoutingSnapshot,
    MergeRequestCreationOrigin,
    MergeRequestState,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.ports import (
    ActorEligibilityContext,
    BindingEligibility,
    BranchSnapshot,
    FormalDeliveryAdmission,
    FormalDeliveryBlockedCallback,
    FormalMergedCallback,
    FormalMrReadyCallback,
    GitLabMergeRequestLocator,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
    GitLabResultUnknown,
    RequirementBindingContext,
    RequirementBindingPort,
)
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_commands import FixedRandom
from tests.source_control.test_migration import _insert_integration_graph

NOW = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000301"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000301"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000301"
REPOSITORY_ID = "10000000-0000-0000-0000-000000000301"
BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000301"
TASK_BRANCH = "feat/wi-301-source-control"
HEAD_SHA = "b" * 40


class FixedClock:
    def now(self) -> datetime:
        return NOW


class ReconciliationClock:
    def now(self) -> datetime:
        return NOW + timedelta(minutes=3)


@dataclass
class FakeRequirementFormalDelivery:
    admission: FormalDeliveryAdmission

    def __post_init__(self) -> None:
        self.blocked: list[FormalDeliveryBlockedCallback] = []
        self.ready: list[FormalMrReadyCallback] = []
        self.merged: list[FormalMergedCallback] = []

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[FormalDeliveryRequestEnvelope, ...]:
        raise AssertionError(f"claim_requests is not used by these tests: {limit=}, {lease_until=}")

    def acknowledge_request(self, message_id: str) -> None:
        raise AssertionError(f"acknowledge_request is not used by these tests: {message_id=}")

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        raise AssertionError(
            f"release_request is not used by these tests: {message_id=}, {error_code=}, {retry_at=}"
        )

    def delivery_admission(self, work_item_id: str) -> FormalDeliveryAdmission:
        assert work_item_id == WORK_ITEM_ID
        return self.admission

    def record_mr_ready(self, callback: FormalMrReadyCallback) -> None:
        self.ready.append(callback)

    def record_blocked(self, callback: FormalDeliveryBlockedCallback) -> None:
        self.blocked.append(callback)

    def record_merged(self, callback: FormalMergedCallback) -> None:
        self.merged.append(callback)


class StaticRouting:
    def resolve(self, **_: str) -> FormalReviewRoutingSnapshot:
        return FormalReviewRoutingSnapshot(
            default_reviewer_id="reviewer-1",
            policy_code="FORMAL_REVIEW_WORK_ITEM_OWNER",
            policy_version=4,
            policy_snapshot_hash="sha256:" + "a" * 64,
            resolution_snapshot={"rule": "WORK_ITEM_OWNER"},
        )


class FakeRequirementBinding:
    def __init__(
        self,
        formal: FakeRequirementFormalDelivery,
        *,
        human_owner_id: str | None = None,
    ) -> None:
        self.formal = formal
        self.human_owner_id = human_owner_id

    def binding_context(self, work_item_id: str) -> RequirementBindingContext:
        admission = self.formal.admission
        assert work_item_id == admission.work_item_id
        return RequirementBindingContext(
            requirement_id=admission.requirement_id,
            requirement_type="feat",
            requirement_title="Deliver the accepted Requirement",
            workspace_id=admission.workspace_id,
            work_item_id=admission.work_item_id,
            work_item_revision=admission.work_item_revision,
            repository_id=admission.repository_id,
            assignment_state="ASSIGNED",
            human_owner_id=self.human_owner_id or admission.human_owner_id,
            required_capabilities=("code.change",),
        )


class FakeEligibility:
    def __init__(self, *, eligible: bool = True, error: Exception | None = None) -> None:
        self.eligible = eligible
        self.error = error
        self.seen: list[ActorEligibilityContext] = []

    def evaluate(self, context: ActorEligibilityContext) -> BindingEligibility:
        self.seen.append(context)
        if self.error is not None:
            raise self.error
        return BindingEligibility(eligible=self.eligible)


class FakeFormalGitLab:
    def __init__(self) -> None:
        self.candidates: list[GitLabMergeRequestSnapshot] = []
        self.created = 0
        self.merged = 0
        self.create_error: Exception | None = None
        self.branch_head = HEAD_SHA
        self.current = self.snapshot()

    @staticmethod
    def snapshot(
        *,
        state: Literal["opened", "merged", "closed", "locked"] = "opened",
    ) -> GitLabMergeRequestSnapshot:
        merged = state == "merged"
        return GitLabMergeRequestSnapshot(
            project_id="101",
            iid=77,
            source_branch=TASK_BRANCH,
            target_branch="main",
            head_sha=HEAD_SHA,
            state=state,
            detailed_merge_status="mergeable",
            has_conflicts=False,
            blocking_discussions_resolved=True,
            head_pipeline_status="success",
            merge_commit_sha=("d" * 40 if merged else None),
            merge_user_id=("42" if merged else None),
            merged_at=(NOW if merged else None),
        )

    def get_project_delivery_profile(
        self,
        _repository: object,
    ) -> GitLabProjectDeliveryProfile:
        return GitLabProjectDeliveryProfile(
            project_id="101",
            project_path="platform/backend",
            default_branch="main",
            merge_method="merge",
        )

    def get_branch(self, _repository: object, name: str) -> BranchSnapshot:
        assert name == TASK_BRANCH
        return BranchSnapshot(name=name, commit_sha=self.branch_head)

    def list_merge_requests(
        self,
        _repository: object,
        *,
        source_branch: str,
        target_branch: str,
        state: Literal["all"] = "all",
    ) -> list[GitLabMergeRequestSnapshot]:
        assert (source_branch, target_branch, state) == (TASK_BRANCH, "main", "all")
        return self.candidates

    def create_formal_merge_request(
        self,
        _repository: object,
        *,
        source_branch: str,
        expected_head_sha: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestLocator:
        assert source_branch == TASK_BRANCH
        assert expected_head_sha == self.branch_head
        assert "formal" in title.lower()
        assert REQUIREMENT_ID in description
        self.created += 1
        if self.create_error is not None:
            raise self.create_error
        return GitLabMergeRequestLocator(
            project_id="101",
            iid=77,
            source_branch=TASK_BRANCH,
            target_branch="main",
        )

    def get_merge_request(
        self,
        _repository: object,
        *,
        iid: int,
    ) -> GitLabMergeRequestSnapshot:
        assert iid == 77
        return self.current

    def merge_formal_merge_request(
        self,
        _repository: object,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot:
        assert iid == 77
        assert expected_head_sha == self.branch_head
        self.merged += 1
        self.current = self.snapshot(state="merged").model_copy(
            update={"head_sha": self.branch_head}
        )
        return self.current


def _admission(**changes: object) -> FormalDeliveryAdmission:
    return FormalDeliveryAdmission(
        requirement_id=REQUIREMENT_ID,
        requirement_revision=12,
        requirement_version=2,
        workspace_id=WORKSPACE_ID,
        work_item_id=WORK_ITEM_ID,
        work_item_revision=10,
        repository_id=REPOSITORY_ID,
        task_branch=TASK_BRANCH,
        requested_head_sha=HEAD_SHA,
        human_owner_id="employee-1",
        acceptance_decision_id="99000000-0000-0000-0000-000000000601",
        formal_merge_request_binding_id=None,
        formal_review_decision_id=None,
    ).model_copy(update=changes)


def _envelope(
    *,
    message_id: str,
    kind: FormalDeliveryRequestKind = FormalDeliveryRequestKind.CREATE_MR,
    binding_id: str | None = None,
    review_id: str | None = None,
    requirement_revision: int = 12,
    work_item_revision: int = 10,
) -> FormalDeliveryRequestEnvelope:
    return FormalDeliveryRequestEnvelope(
        message_id=message_id,
        topic=(
            "requirement.formal-merge-request.requested"
            if kind is FormalDeliveryRequestKind.CREATE_MR
            else "requirement.formal-merge.requested"
        ),
        payload_hash="sha256:" + message_id[-1] * 64,
        requirement_id=REQUIREMENT_ID,
        requirement_revision=requirement_revision,
        work_item_id=WORK_ITEM_ID,
        work_item_revision=work_item_revision,
        repository_id=REPOSITORY_ID,
        actor_id="employee-1",
        acceptance_decision_id="99000000-0000-0000-0000-000000000601",
        formal_merge_request_binding_id=binding_id,
        formal_review_decision_id=review_id,
        requested_head_sha=HEAD_SHA,
        kind=kind,
        attempts=1,
    )


def _dependencies(
    source: IsolatedSourceControlDatabase,
    requirement: FakeRequirementFormalDelivery,
    gitlab: FakeFormalGitLab,
    *,
    eligibility: FakeEligibility | None = None,
    binding: FakeRequirementBinding | None = None,
) -> SourceControlDependencies:
    stable_binding = binding or FakeRequirementBinding(requirement)
    return SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=source.runtime,
        requirement=cast(RequirementBindingPort, stable_binding),
        eligibility=eligibility or FakeEligibility(),
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=FixedClock(),
        random=FixedRandom(),
        gitlab_formal_merge_requests=gitlab,
        formal_repository_factory=SqlAlchemySourceControlFormalRepository,
        requirement_formal_delivery=requirement,
        formal_review_routing=StaticRouting(),
    )


def _seed(source: IsolatedSourceControlDatabase) -> None:
    with source.owner.begin() as db:
        _insert_integration_graph(db)


def test_create_and_merge_formal_mr_use_exact_head_effects_and_callbacks(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    create = _envelope(message_id="91000000-0000-0000-0000-000000000601")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            create,
            dependencies=dependencies,
        )
    created = process_formal_delivery_request(
        message_id=create.message_id,
        dependencies=dependencies,
    )

    assert created.effect is not None
    assert created.effect.state is EffectState.SUCCEEDED
    assert created.binding is not None
    assert created.observation is not None
    assert created.binding.kind.value == "FORMAL"
    assert created.binding.creation_origin is MergeRequestCreationOrigin.PLATFORM_CREATED
    assert created.binding.target_branch == "main"
    assert created.binding.head_sha == HEAD_SHA
    assert created.observation.state is MergeRequestState.OPEN
    assert created.assignment is not None
    assert requirement.ready[0].binding_id == created.binding.id
    assert requirement.ready[0].head_sha == HEAD_SHA
    eligibility = cast(FakeEligibility, dependencies.eligibility)
    assert [(context.actor_id, context.required_capabilities) for context in eligibility.seen] == [
        ("employee-1", ("code.change",)),
        ("employee-1", ("formal_merge_request.request",)),
    ] * 2
    eligibility.seen.clear()

    requirement.admission = _admission(
        requirement_revision=14,
        work_item_revision=12,
        formal_merge_request_binding_id=created.binding.id,
        formal_review_decision_id="99000000-0000-0000-0000-000000000602",
    )
    merge = _envelope(
        message_id="91000000-0000-0000-0000-000000000602",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=created.binding.id,
        review_id="99000000-0000-0000-0000-000000000602",
        requirement_revision=14,
        work_item_revision=12,
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            merge,
            dependencies=dependencies,
        )
    merged = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )

    assert merged.effect is not None
    assert merged.effect.state is EffectState.SUCCEEDED
    assert merged.observation is not None
    assert merged.observation.state is MergeRequestState.MERGED
    assert merged.observation.merge_commit_sha == "d" * 40
    assert requirement.merged[0].binding_id == created.binding.id
    assert gitlab.created == 1
    assert gitlab.merged == 1
    assert [(context.actor_id, context.required_capabilities) for context in eligibility.seen] == [
        ("employee-1", ("code.change",)),
        ("employee-1", ("merge_request.merge",)),
    ] * 2


def test_formal_merge_rechecks_actor_eligibility_before_provider_write(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    eligibility = FakeEligibility()
    binding = FakeRequirementBinding(requirement)
    dependencies = _dependencies(
        isolated_source_control_database,
        requirement,
        gitlab,
        eligibility=eligibility,
        binding=binding,
    )
    create = _envelope(message_id="91000000-0000-0000-0000-000000000631")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            create,
            dependencies=dependencies,
        )
    created = process_formal_delivery_request(
        message_id=create.message_id,
        dependencies=dependencies,
    )
    assert created.binding is not None
    requirement.admission = _admission(
        requirement_revision=14,
        work_item_revision=12,
        formal_merge_request_binding_id=created.binding.id,
        formal_review_decision_id="99000000-0000-0000-0000-000000000632",
    )
    merge = _envelope(
        message_id="91000000-0000-0000-0000-000000000632",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=created.binding.id,
        review_id="99000000-0000-0000-0000-000000000632",
        requirement_revision=14,
        work_item_revision=12,
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            merge,
            dependencies=dependencies,
        )

    eligibility.eligible = False
    eligibility.seen.clear()
    blocked = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.blocked_reason == "MERGE_ACTOR_INELIGIBLE"
    assert requirement.blocked[-1].reason_code.value == "MERGE_ACTOR_INELIGIBLE"
    assert len(eligibility.seen) == 2
    assert gitlab.merged == 0

    eligibility.eligible = True
    binding.human_owner_id = "employee-2"
    owner_drift = _envelope(
        message_id="91000000-0000-0000-0000-000000000635",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=created.binding.id,
        review_id="99000000-0000-0000-0000-000000000632",
        requirement_revision=14,
        work_item_revision=12,
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            owner_drift,
            dependencies=dependencies,
        )
    owner_blocked = process_formal_delivery_request(
        message_id=owner_drift.message_id,
        dependencies=dependencies,
    )
    assert owner_blocked.blocked_reason == "MERGE_ACTOR_INELIGIBLE"
    assert requirement.blocked[-1].reason_code.value == "MERGE_ACTOR_INELIGIBLE"
    assert gitlab.merged == 0


def test_formal_merge_eligibility_outage_fails_closed_before_provider_write(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    eligibility = FakeEligibility()
    dependencies = _dependencies(
        isolated_source_control_database,
        requirement,
        gitlab,
        eligibility=eligibility,
    )
    create = _envelope(message_id="91000000-0000-0000-0000-000000000633")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            create,
            dependencies=dependencies,
        )
    created = process_formal_delivery_request(
        message_id=create.message_id,
        dependencies=dependencies,
    )
    assert created.binding is not None
    requirement.admission = _admission(
        requirement_revision=14,
        work_item_revision=12,
        formal_merge_request_binding_id=created.binding.id,
        formal_review_decision_id="99000000-0000-0000-0000-000000000634",
    )
    merge = _envelope(
        message_id="91000000-0000-0000-0000-000000000634",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=created.binding.id,
        review_id="99000000-0000-0000-0000-000000000634",
        requirement_revision=14,
        work_item_revision=12,
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            merge,
            dependencies=dependencies,
        )

    eligibility.error = RuntimeError("eligibility unavailable")
    eligibility.seen.clear()
    with pytest.raises(
        SourceControlDependencyUnavailable,
        match="actor eligibility unavailable",
    ):
        process_formal_delivery_request(
            message_id=merge.message_id,
            dependencies=dependencies,
        )

    assert len(eligibility.seen) == 1
    assert gitlab.merged == 0


def test_unknown_create_is_reconciled_by_exact_provider_readback(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    gitlab.create_error = GitLabResultUnknown("timeout after write")
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000603")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            envelope,
            dependencies=dependencies,
        )
    unknown = process_formal_delivery_request(
        message_id=envelope.message_id,
        dependencies=dependencies,
    )
    assert unknown.effect is not None
    assert unknown.effect.state is EffectState.UNKNOWN
    assert requirement.ready == []

    gitlab.create_error = None
    gitlab.candidates = [gitlab.snapshot()]
    dependencies = replace(dependencies, clock=ReconciliationClock())
    reconciled = reconcile_formal_delivery_effect(
        effect_id=unknown.effect.id,
        dependencies=dependencies,
    )
    assert reconciled.effect is not None
    assert reconciled.effect.state is EffectState.SUCCEEDED
    assert reconciled.binding is not None
    assert reconciled.binding.creation_origin is MergeRequestCreationOrigin.EXTERNAL_ADOPTED
    assert requirement.ready[0].head_sha == HEAD_SHA
    with isolated_source_control_database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM source_control.merge_request_binding WHERE kind='FORMAL'"
                )
            ).scalar_one()
            == 1
        )


def test_existing_formal_mr_is_refreshed_with_append_only_new_head_review_facts(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    initial = _envelope(message_id="91000000-0000-0000-0000-000000000621")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            initial,
            dependencies=dependencies,
        )
    created = process_formal_delivery_request(
        message_id=initial.message_id,
        dependencies=dependencies,
    )
    assert created.binding is not None
    original_binding_head = created.binding.head_sha
    new_head = "e" * 40
    refreshed_acceptance_id = "99000000-0000-0000-0000-000000000604"
    refreshed_snapshot = gitlab.snapshot().model_copy(update={"head_sha": new_head})
    gitlab.branch_head = new_head
    gitlab.current = refreshed_snapshot
    gitlab.candidates = [refreshed_snapshot]
    requirement.admission = _admission(
        requirement_revision=14,
        work_item_revision=12,
        requested_head_sha=new_head,
        acceptance_decision_id=refreshed_acceptance_id,
        formal_merge_request_binding_id=created.binding.id,
        formal_review_decision_id=None,
    )
    refresh = _envelope(
        message_id="91000000-0000-0000-0000-000000000622",
        binding_id=created.binding.id,
        requirement_revision=14,
        work_item_revision=12,
    ).model_copy(
        update={
            "requested_head_sha": new_head,
            "acceptance_decision_id": refreshed_acceptance_id,
        }
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            refresh,
            dependencies=dependencies,
        )

    refreshed = process_formal_delivery_request(
        message_id=refresh.message_id,
        dependencies=dependencies,
    )

    assert refreshed.binding is not None
    assert refreshed.binding.id == created.binding.id
    assert refreshed.binding.head_sha == original_binding_head
    assert refreshed.observation is not None
    assert refreshed.observation.head_sha == new_head
    assert refreshed.assignment is not None
    assert refreshed.assignment.subject_head_sha == new_head
    assert requirement.ready[-1].binding_id == created.binding.id
    assert requirement.ready[-1].head_sha == new_head
    assert gitlab.created == 1
    requirement.admission = _admission(
        requirement_revision=16,
        work_item_revision=14,
        requested_head_sha=new_head,
        acceptance_decision_id=refreshed_acceptance_id,
        formal_merge_request_binding_id=created.binding.id,
        formal_review_decision_id="99000000-0000-0000-0000-000000000622",
    )
    merge = _envelope(
        message_id="91000000-0000-0000-0000-000000000624",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=created.binding.id,
        review_id="99000000-0000-0000-0000-000000000622",
        requirement_revision=16,
        work_item_revision=14,
    ).model_copy(
        update={
            "requested_head_sha": new_head,
            "acceptance_decision_id": refreshed_acceptance_id,
        }
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            merge,
            dependencies=dependencies,
        )
    merged = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )
    assert merged.observation is not None
    assert merged.observation.head_sha == new_head
    assert merged.observation.state is MergeRequestState.MERGED
    assert requirement.merged[-1].head_sha == new_head
    with isolated_source_control_database.owner.connect() as db:
        assignments = db.execute(
            text(
                "SELECT subject_head_sha, superseded_at IS NULL AS is_current "
                "FROM source_control.formal_review_assignment "
                "WHERE binding_id=:binding_id ORDER BY revision"
            ),
            {"binding_id": created.binding.id},
        ).all()
    assert [tuple(row) for row in assignments] == [(HEAD_SHA, False), (new_head, True)]


def test_same_head_new_acceptance_appends_assignment_and_replays_original_cycle(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    first_acceptance_id = "99000000-0000-0000-0000-000000000601"
    second_acceptance_id = "99000000-0000-0000-0000-000000000603"
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    first_request = _envelope(message_id="91000000-0000-0000-0000-000000000625")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            first_request,
            dependencies=dependencies,
        )
    first = process_formal_delivery_request(
        message_id=first_request.message_id,
        dependencies=dependencies,
    )
    assert first.binding is not None
    assert first.effect is not None

    gitlab.candidates = [gitlab.current]
    requirement.admission = _admission(
        requirement_revision=14,
        work_item_revision=12,
        acceptance_decision_id=second_acceptance_id,
        formal_merge_request_binding_id=first.binding.id,
    )
    second_request = _envelope(
        message_id="91000000-0000-0000-0000-000000000626",
        binding_id=first.binding.id,
        requirement_revision=14,
        work_item_revision=12,
    ).model_copy(update={"acceptance_decision_id": second_acceptance_id})
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            second_request,
            dependencies=dependencies,
        )

    second = process_formal_delivery_request(
        message_id=second_request.message_id,
        dependencies=dependencies,
    )
    replayed_first = process_formal_delivery_request(
        message_id=first_request.message_id,
        dependencies=dependencies,
    )

    assert first.assignment is not None
    assert second.assignment is not None
    assert replayed_first.assignment is not None
    assert second.effect is not None
    assert second.effect.id != first.effect.id
    assert first.assignment.acceptance_decision_id == first_acceptance_id
    assert second.assignment.acceptance_decision_id == second_acceptance_id
    assert second.assignment.id != first.assignment.id
    assert replayed_first.assignment.id == first.assignment.id
    assert replayed_first.assignment.acceptance_decision_id == first_acceptance_id
    assert gitlab.created == 1
    with isolated_source_control_database.owner.connect() as db:
        assignments = db.execute(
            text(
                "SELECT acceptance_decision_id, superseded_at IS NULL AS is_current "
                "FROM source_control.formal_review_assignment "
                "WHERE binding_id=:binding_id ORDER BY revision"
            ),
            {"binding_id": first.binding.id},
        ).all()
    assert [(str(row.acceptance_decision_id), row.is_current) for row in assignments] == [
        (first_acceptance_id, False),
        (second_acceptance_id, True),
    ]


def test_provider_head_drift_persists_terminal_effect_before_requirement_callback(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    gitlab.branch_head = "e" * 40
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000623")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            envelope,
            dependencies=dependencies,
        )

    blocked = process_formal_delivery_request(
        message_id=envelope.message_id,
        dependencies=dependencies,
    )
    replay = process_formal_delivery_request(
        message_id=envelope.message_id,
        dependencies=dependencies,
    )

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state.value == "ACKED"
    assert blocked.blocked_reason == "HEAD_SHA_CHANGED"
    assert replay == blocked
    assert len(requirement.blocked) == 1
    assert requirement.blocked[0].reason_code.value == "HEAD_SHA_CHANGED"
    assert requirement.blocked[0].idempotency_key == (
        f"source-control:formal-blocked:{blocked.effect.id}"
    )
    with isolated_source_control_database.owner.connect() as db:
        inbox = db.execute(
            text(
                "SELECT state, last_error_code FROM "
                "source_control.formal_delivery_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": envelope.message_id},
        ).one()
        effect_count = db.execute(
            text(
                "SELECT count(*) FROM source_control.source_control_effect "
                "WHERE operation IN ('CREATE_FORMAL_MR', 'MERGE_FORMAL_MR')"
            )
        ).scalar_one()
    assert inbox == ("PROCESSED", "HEAD_SHA_CHANGED")
    assert effect_count == 1
