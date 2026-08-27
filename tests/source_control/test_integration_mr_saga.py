from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine

from control_plane.app.modules.source_control import (
    EffectOperation,
    EffectState,
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    process_integration_mr_request,
)
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlIntegrationRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.ports import (
    BindingBlockedResult,
    BindingEligibility,
    BindingReadyResult,
    BranchSnapshot,
    ExternalMergeDriftResult,
    GitLabBranchNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
    GitLabProviderUnavailable,
    GitLabResultUnknown,
    IntegrationDeliveryBlockedResult,
    IntegrationMergedResult,
    IntegrationMrReadyResult,
    IntegrationReconciliationPendingResult,
    RequirementBindingContext,
    RequirementDeliveryContext,
    SourceControlIntegrationRepositoryFactory,
)

NOW = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
MESSAGE_ID = "30000000-0000-0000-0000-000000000701"
REQUIREMENT_ID = "40000000-0000-0000-0000-000000000701"
WORK_ITEM_ID = "50000000-0000-0000-0000-000000000701"
REPOSITORY_ID = "10000000-0000-0000-0000-000000000701"
WORKSPACE_ID = "20000000-0000-0000-0000-000000000701"
BRANCH_EFFECT_ID = "60000000-0000-0000-0000-000000000701"
BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000701"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
TASK_BRANCH = "feat/wi-701-integration-mr"


class FixedClock:
    def now(self) -> datetime:
        return NOW


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class FixedRandom:
    def __init__(self) -> None:
        self._next = 700

    def uuid4(self) -> UUID:
        self._next += 1
        return UUID(f"90000000-0000-0000-0000-{self._next:012d}")


class FakeAudit:
    def append_in_transaction(self, _db: object, _envelope: object) -> None:
        return None


class FixedPolicy:
    def next_reconcile_at(self, *, now: datetime, attempts: int) -> datetime:
        return now + timedelta(minutes=max(1, attempts))

    def webhook_replay_window(self) -> timedelta:
        return timedelta(minutes=5)


class FakeEligibility:
    def __init__(self, eligible: bool = True) -> None:
        self.eligible = eligible
        self.seen: list[RequirementBindingContext] = []

    def evaluate(self, context: RequirementBindingContext) -> BindingEligibility:
        self.seen.append(context)
        if not self.eligible:
            return BindingEligibility(eligible=False, reason_code="OWNER_INELIGIBLE")
        return BindingEligibility(eligible=True)


class FakeRequirementBinding:
    def __init__(self, context: RequirementBindingContext | None = None) -> None:
        self.context = context or _binding_context()

    def binding_context(self, work_item_id: str) -> RequirementBindingContext:
        assert work_item_id == WORK_ITEM_ID
        return self.context

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[Any, ...]:
        raise AssertionError((limit, lease_until))

    def acknowledge_request(self, message_id: str) -> None:
        raise AssertionError(message_id)

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        raise AssertionError((message_id, error_code, retry_at))

    def record_ready(self, result: BindingReadyResult) -> None:
        raise AssertionError(result)

    def record_blocked(self, result: BindingBlockedResult) -> None:
        raise AssertionError(result)


class FakeRequirementDelivery:
    def __init__(self, context: RequirementDeliveryContext | None = None) -> None:
        self.context = context or _delivery_context()
        self.ready: list[IntegrationMrReadyResult] = []
        self.blocked: list[IntegrationDeliveryBlockedResult] = []
        self.pending: list[IntegrationReconciliationPendingResult] = []
        self.fail_ready = False
        self.fail_blocked = False
        self.fail_pending = False

    def delivery_context(self, work_item_id: str) -> RequirementDeliveryContext:
        assert work_item_id == WORK_ITEM_ID
        return self.context

    def claim_requests(
        self,
        *,
        limit: int,
        lease_until: datetime,
    ) -> tuple[Any, ...]:
        raise AssertionError((limit, lease_until))

    def acknowledge_request(self, message_id: str) -> None:
        raise AssertionError(message_id)

    def release_request(
        self,
        message_id: str,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        raise AssertionError((message_id, error_code, retry_at))

    def record_mr_ready(self, result: IntegrationMrReadyResult) -> None:
        if self.fail_ready:
            raise RuntimeError("requirement callback unavailable")
        self.ready.append(result)

    def record_blocked(self, result: IntegrationDeliveryBlockedResult) -> None:
        if self.fail_blocked:
            raise RuntimeError("requirement callback unavailable")
        self.blocked.append(result)

    def record_pending(self, result: IntegrationReconciliationPendingResult) -> None:
        if self.fail_pending:
            raise RuntimeError("requirement callback unavailable")
        self.pending.append(result)

    def record_merged(self, result: IntegrationMergedResult) -> None:
        raise AssertionError(result)

    def record_external_merge_drift(self, result: ExternalMergeDriftResult) -> None:
        raise AssertionError(result)


class FakeGitLabMergeRequests:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.calls: list[str] = []
        self.source_head = HEAD_SHA
        self.candidates: list[GitLabMergeRequestSnapshot] = []
        self.readback = _mr_snapshot()
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.list_error: Exception | None = None
        self.profile_error: Exception | None = None
        self.branch_errors: dict[str, Exception] = {}
        self.before_readback: Callable[[], None] | None = None
        self.expected_title = f"feat: integrate {WORK_ITEM_ID}"

    def get_project_delivery_profile(self, _repository: object) -> GitLabProjectDeliveryProfile:
        self.calls.append("profile")
        if self.profile_error is not None:
            raise self.profile_error
        return GitLabProjectDeliveryProfile(
            project_id="101",
            project_path="platform/backend",
            default_branch="main",
            merge_method="merge",
        )

    def get_branch(self, _repository: object, name: str) -> BranchSnapshot:
        self.calls.append("dev_branch" if name == "dev" else "source_branch")
        if name in self.branch_errors:
            raise self.branch_errors[name]
        return BranchSnapshot(
            name=name,
            commit_sha=self.source_head if name == TASK_BRANCH else "c" * 40,
        )

    def list_merge_requests(
        self,
        _repository: object,
        *,
        source_branch: str,
        target_branch: str,
    ) -> list[GitLabMergeRequestSnapshot]:
        self.calls.append("list_mr")
        assert (source_branch, target_branch) == (TASK_BRANCH, "dev")
        if self.list_error is not None:
            raise self.list_error
        return self.candidates

    def create_merge_request(
        self,
        _repository: object,
        *,
        source_branch: str,
        target_branch: str,
        expected_head_sha: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestSnapshot:
        self.calls.append("create_mr")
        with self.engine.connect() as db:
            effect = SqlAlchemySourceControlIntegrationRepository(db).effect_by_operation_subject(
                EffectOperation.CREATE_INTEGRATION_MR.value,
                f"work-item:{WORK_ITEM_ID}",
            )
        assert effect is not None
        assert effect["state"] == EffectState.IN_FLIGHT.value
        assert dict(effect["payload"]) == {
            "branchBindingId": BRANCH_BINDING_ID,
            "headSha": HEAD_SHA,
        }
        assert source_branch == TASK_BRANCH
        assert target_branch == "dev"
        assert expected_head_sha == HEAD_SHA
        assert title == self.expected_title
        assert description == (
            f"Requirement: {REQUIREMENT_ID}\n"
            f"Work-Item: {WORK_ITEM_ID}\n"
            f"Source-Control-Effect: {effect['id']}"
        )
        if self.create_error is not None:
            raise self.create_error
        return _mr_snapshot()

    def get_merge_request(
        self,
        _repository: object,
        *,
        iid: int,
    ) -> GitLabMergeRequestSnapshot:
        self.calls.append("get_mr")
        if self.get_error is not None:
            raise self.get_error
        if self.before_readback is not None:
            self.before_readback()
        assert iid == 17
        return self.readback

    def merge_merge_request(
        self,
        _repository: object,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot:
        raise AssertionError((iid, expected_head_sha))


def _mr_snapshot(
    *,
    iid: int = 17,
    project_id: str = "101",
    source_branch: str = TASK_BRANCH,
    target_branch: str = "dev",
    head_sha: str = HEAD_SHA,
    state: Literal["opened", "merged", "closed", "locked"] = "opened",
) -> GitLabMergeRequestSnapshot:
    return GitLabMergeRequestSnapshot(
        project_id=project_id,
        iid=iid,
        source_branch=source_branch,
        target_branch=target_branch,
        head_sha=head_sha,
        state=state,
        detailed_merge_status="checking",
        has_conflicts=False,
        blocking_discussions_resolved=True,
        head_pipeline_status=None,
        merge_commit_sha=None,
        merge_user_id=None,
        merged_at=None,
    )


def _delivery_context() -> RequirementDeliveryContext:
    return RequirementDeliveryContext(
        requirement_id=REQUIREMENT_ID,
        requirement_state="IN_PROGRESS",
        workspace_id=WORKSPACE_ID,
        work_item_id=WORK_ITEM_ID,
        work_item_revision=5,
        work_item_state="IN_PROGRESS",
        repository_id=REPOSITORY_ID,
        repository_state="BOUND",
        human_owner_id="employee-1",
        required_capabilities=("work_item.execute",),
        base_commit_sha=BASE_SHA,
        task_branch=TASK_BRANCH,
        integration_delivery_state="MR_PENDING",
        integration_merge_request_binding_id=None,
        request_actor_id="employee-1",
    )


def _binding_context() -> RequirementBindingContext:
    return RequirementBindingContext(
        requirement_id=REQUIREMENT_ID,
        requirement_type="feat",
        requirement_title="Integration MR",
        workspace_id=WORKSPACE_ID,
        work_item_id=WORK_ITEM_ID,
        work_item_revision=5,
        repository_id=REPOSITORY_ID,
        assignment_state="ASSIGNED",
        human_owner_id="employee-1",
        required_capabilities=("work_item.execute",),
    )


def _seed_source_control(engine: Engine) -> None:
    with engine.begin() as db:
        branch = SqlAlchemySourceControlRepository(db)
        delivery = SqlAlchemySourceControlIntegrationRepository(db)
        branch.insert_workspace_repository(
            id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            provider="GITLAB",
            project_id="101",
            project_path="platform/backend",
            default_branch="main",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref="secret-ref:webhook",
            status="AUTHORIZED",
            revision=1,
            now=NOW,
        )
        branch.insert_effect(
            id=BRANCH_EFFECT_ID,
            effect_key=f"source-control:create-task-branch:{WORK_ITEM_ID}",
            operation="CREATE_TASK_BRANCH",
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            repository_id=REPOSITORY_ID,
            work_item_number=701,
            branch_name=TASK_BRANCH,
            base_commit_sha=BASE_SHA,
            request_fingerprint="sha256:branch",
            attempts=0,
            state="IN_FLIGHT",
            requirement_callback_state="ACKED",
            next_reconcile_at=None,
            now=NOW,
        )
        branch.transition_effect(
            BRANCH_EFFECT_ID,
            expected_state="IN_FLIGHT",
            values={"state": "SUCCEEDED", "completed_at": NOW, "updated_at": NOW},
        )
        branch.insert_binding(
            id=BRANCH_BINDING_ID,
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            workspace_id=WORKSPACE_ID,
            repository_id=REPOSITORY_ID,
            work_item_number=701,
            base_commit_sha=BASE_SHA,
            branch_name=TASK_BRANCH,
            effect_id=BRANCH_EFFECT_ID,
            now=NOW,
        )
        delivery.accept_delivery_request(
            message_id=MESSAGE_ID,
            topic="requirement.integration-merge-request.requested",
            payload_hash="sha256:delivery-request",
            requirement_id=REQUIREMENT_ID,
            requirement_revision=3,
            work_item_id=WORK_ITEM_ID,
            work_item_revision=5,
            repository_id=REPOSITORY_ID,
            actor_id="employee-1",
            integration_merge_request_binding_id=None,
            now=NOW,
        )


def _dependencies(
    engine: Engine,
    *,
    requirement: FakeRequirementDelivery | None = None,
    gitlab: FakeGitLabMergeRequests | None = None,
    eligibility: FakeEligibility | None = None,
    binding_requirement: FakeRequirementBinding | None = None,
    clock: FixedClock | MutableClock | None = None,
    delivery_repository_factory: SourceControlIntegrationRepositoryFactory = (
        SqlAlchemySourceControlIntegrationRepository
    ),
) -> tuple[
    SourceControlDependencies,
    FakeRequirementDelivery,
    FakeGitLabMergeRequests,
]:
    requirement = requirement or FakeRequirementDelivery()
    gitlab = gitlab or FakeGitLabMergeRequests(engine)
    binding_requirement = binding_requirement or FakeRequirementBinding()
    return (
        SourceControlDependencies(
            repository_factory=SqlAlchemySourceControlRepository,
            engine=engine,
            requirement=binding_requirement,
            eligibility=eligibility or FakeEligibility(),
            audit=FakeAudit(),
            clock=clock or FixedClock(),
            random=FixedRandom(),
            policy=FixedPolicy(),
            delivery_repository_factory=delivery_repository_factory,
            requirement_delivery=requirement,
            gitlab_merge_requests=gitlab,
        ),
        requirement,
        gitlab,
    )


def test_exact_delivery_claim_does_not_lease_an_earlier_due_message(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    earlier_message_id = "30000000-0000-0000-0000-000000000700"
    with engine.begin() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        repository.accept_delivery_request(
            message_id=earlier_message_id,
            topic="requirement.integration-merge-request.requested",
            payload_hash="sha256:earlier",
            requirement_id=REQUIREMENT_ID,
            requirement_revision=3,
            work_item_id=WORK_ITEM_ID,
            work_item_revision=5,
            repository_id=REPOSITORY_ID,
            actor_id="employee-1",
            integration_merge_request_binding_id=None,
            now=NOW - timedelta(minutes=1),
        )
        claimed = repository.claim_delivery_request(
            MESSAGE_ID,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )
        earlier = repository.delivery_request(earlier_message_id)

    assert claimed is not None
    assert claimed["state"] == "PROCESSING"
    assert claimed["attempts"] == 1
    assert earlier["state"] == "RECEIVED"
    assert earlier["attempts"] == 0


def test_exact_delivery_claim_fences_a_second_worker(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    with engine.begin() as db:
        first = SqlAlchemySourceControlIntegrationRepository(db).claim_delivery_request(
            MESSAGE_ID,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )
    with engine.begin() as db:
        second = SqlAlchemySourceControlIntegrationRepository(db).claim_delivery_request(
            MESSAGE_ID,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )

    assert first is not None
    assert first["attempts"] == 1
    assert second is None


def test_title_and_eligibility_use_real_requirement_binding_context(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    delivery_context = _delivery_context().model_copy(
        update={"required_capabilities": ("work_item.execute", "repository.read")}
    )
    binding_context = _binding_context().model_copy(
        update={
            "requirement_type": "chore",
            "required_capabilities": ("work_item.execute", "repository.read"),
        }
    )
    requirement = FakeRequirementDelivery(delivery_context)
    binding_requirement = FakeRequirementBinding(binding_context)
    eligibility = FakeEligibility()
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.expected_title = f"chore: integrate {WORK_ITEM_ID}"
    dependencies, _requirement, _gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
        eligibility=eligibility,
        binding_requirement=binding_requirement,
    )

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.SUCCEEDED
    assert eligibility.seen == [binding_context]


def test_mismatched_requirement_contexts_fail_closed_before_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    mismatched = _binding_context().model_copy(
        update={"requirement_id": "40000000-0000-0000-0000-000000000799"}
    )
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, _requirement, _gitlab = _dependencies(
        engine,
        gitlab=gitlab,
        binding_requirement=FakeRequirementBinding(mismatched),
    )

    with pytest.raises(SourceControlDependencyUnavailable, match="context"):
        process_integration_mr_request(
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )

    assert gitlab.calls == []


def test_create_mr_saga_persists_effect_before_post_and_reads_back(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    dependencies, requirement, gitlab = _dependencies(engine)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.operation is EffectOperation.CREATE_INTEGRATION_MR
    assert result.effect.state is EffectState.SUCCEEDED
    assert result.binding is not None
    assert result.binding.source_branch == TASK_BRANCH
    assert result.binding.target_branch == "dev"
    assert result.observation is not None
    assert result.observation.head_sha == HEAD_SHA
    assert requirement.ready[0].binding_id == result.binding.id
    assert gitlab.calls == [
        "profile",
        "source_branch",
        "dev_branch",
        "list_mr",
        "create_mr",
        "get_mr",
    ]


def test_head_equal_to_base_blocks_without_creating_an_effect(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.source_head = BASE_SHA
    dependencies, requirement, gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.binding is None
    assert result.observation is None
    assert result.blocked_reason == "NO_DELIVERY_COMMIT"
    assert requirement.blocked[0].reason_code == "NO_DELIVERY_COMMIT"
    assert gitlab.calls == ["profile", "source_branch", "dev_branch"]
    with engine.connect() as db:
        effect = SqlAlchemySourceControlIntegrationRepository(db).effect_by_operation_subject(
            EffectOperation.CREATE_INTEGRATION_MR.value,
            f"work-item:{WORK_ITEM_ID}",
        )
    assert effect is None


def test_unique_matching_merge_request_is_adopted_without_post(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.candidates = [_mr_snapshot()]
    dependencies, requirement, gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.SUCCEEDED
    assert result.binding is not None
    assert result.binding.creation_origin.value == "EXTERNAL_ADOPTED"
    assert requirement.ready[0].binding_id == result.binding.id
    assert gitlab.calls == [
        "profile",
        "source_branch",
        "dev_branch",
        "list_mr",
        "get_mr",
    ]


def test_multiple_merge_request_candidates_block_as_conflict(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.candidates = [_mr_snapshot(), _mr_snapshot(iid=18)]
    dependencies, requirement, gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.blocked_reason == "MR_CONFLICT"
    assert requirement.blocked[0].reason_code == "MR_CONFLICT"
    assert "create_mr" not in gitlab.calls


@pytest.mark.parametrize(
    "error",
    [
        GitLabResultUnknown("post timed out"),
        GitLabProviderUnavailable("post returned malformed response"),
    ],
)
def test_post_uncertainty_marks_effect_unknown_without_guessing_success(
    isolated_source_control_database: Any,
    error: Exception,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.create_error = error
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.UNKNOWN
    assert result.effect.next_reconcile_at is not None
    assert result.binding is None
    assert result.observation is None
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert requirement.pending[0].work_item_id == WORK_ITEM_ID


def test_post_success_followed_by_readback_failure_marks_effect_unknown(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.get_error = GitLabProviderUnavailable("readback unavailable")
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.UNKNOWN
    assert result.binding is None
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert len(requirement.pending) == 1
    assert gitlab.calls[-2:] == ["create_mr", "get_mr"]


def test_pending_callback_failure_replays_without_repeating_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = FakeRequirementDelivery()
    requirement.fail_pending = True
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.create_error = GitLabResultUnknown("post timed out")
    dependencies, requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    first_calls = tuple(gitlab.calls)
    requirement.fail_pending = False
    second = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.state is EffectState.UNKNOWN
    assert first.effect.callback_state.value == "FAILED"
    assert second.effect is not None
    assert second.effect.state is EffectState.UNKNOWN
    assert second.effect.callback_state.value == "ACKED"
    assert second.blocked_reason == "RECONCILIATION_PENDING"
    assert tuple(gitlab.calls) == first_calls
    assert len(requirement.pending) == 1


class FailOnceOnBindingRepository(SqlAlchemySourceControlIntegrationRepository):
    def __init__(self, db: Connection, state: dict[str, bool]) -> None:
        super().__init__(db)
        self.state = state

    def insert_merge_request_binding(self, **values: object) -> object:
        if not self.state["failed"]:
            self.state["failed"] = True
            raise RuntimeError("local commit outcome unavailable")
        return super().insert_merge_request_binding(**values)


class FailOnceOnBindingRepositoryFactory:
    def __init__(self) -> None:
        self.state = {"failed": False}

    def __call__(self, db: Connection) -> FailOnceOnBindingRepository:
        return FailOnceOnBindingRepository(db, self.state)


def test_local_persistence_failure_after_create_marks_effect_unknown(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    factory = FailOnceOnBindingRepositoryFactory()
    dependencies, requirement, _gitlab = _dependencies(
        engine,
        delivery_repository_factory=factory,
    )

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.UNKNOWN
    assert result.binding is None
    assert result.observation is None
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert len(requirement.pending) == 1
    with engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        assert repository.merge_request_binding_by_work_item(WORK_ITEM_ID) is None


def test_ready_callback_failure_replays_without_repeating_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = FakeRequirementDelivery()
    requirement.fail_ready = True
    dependencies, requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    first_calls = tuple(gitlab.calls)
    requirement.fail_ready = False
    second = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.state is EffectState.SUCCEEDED
    assert first.effect.callback_state.value == "FAILED"
    assert second.effect is not None
    assert second.effect.callback_state.value == "ACKED"
    assert second.binding == first.binding
    assert tuple(gitlab.calls) == first_calls
    assert len(requirement.ready) == 1


def test_blocked_effect_callback_failure_replays_without_repeating_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = FakeRequirementDelivery()
    requirement.fail_blocked = True
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.candidates = [_mr_snapshot(), _mr_snapshot(iid=18)]
    dependencies, requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    first_calls = tuple(gitlab.calls)
    requirement.fail_blocked = False
    second = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.state is EffectState.BLOCKED
    assert first.effect.callback_state.value == "FAILED"
    assert second.effect is not None
    assert second.effect.callback_state.value == "ACKED"
    assert second.blocked_reason == "MR_CONFLICT"
    assert tuple(gitlab.calls) == first_calls
    assert len(requirement.blocked) == 1


def test_preflight_block_callback_failure_remains_replayable_after_lease(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = FakeRequirementDelivery()
    requirement.fail_blocked = True
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.source_head = BASE_SHA
    clock = MutableClock()
    dependencies, requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
        clock=clock,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    requirement.fail_blocked = False
    clock.current = NOW + timedelta(minutes=3)
    second = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is None
    assert first.blocked_reason == "NO_DELIVERY_COMMIT"
    assert second.effect is None
    assert second.blocked_reason == "NO_DELIVERY_COMMIT"
    assert len(requirement.blocked) == 1
    assert gitlab.calls == [
        "profile",
        "source_branch",
        "dev_branch",
        "profile",
        "source_branch",
        "dev_branch",
    ]
    with engine.connect() as db:
        inbox = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(MESSAGE_ID)
    assert inbox is not None
    assert inbox["state"] == "PROCESSED"
    assert inbox["attempts"] == 2


def test_readback_head_drift_appends_latest_observation_without_mutating_effect_payload(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    moved_head = "d" * 40
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.readback = _mr_snapshot(head_sha=moved_head)
    dependencies, _requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.payload.model_dump(by_alias=True) == {
        "branchBindingId": BRANCH_BINDING_ID,
        "headSha": HEAD_SHA,
    }
    assert result.binding is not None
    assert result.binding.head_sha == HEAD_SHA
    assert result.observation is not None
    assert result.observation.head_sha == moved_head


def test_current_owner_mismatch_blocks_before_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    delivery_context = _delivery_context().model_copy(update={"human_owner_id": "employee-2"})
    binding_context = _binding_context().model_copy(update={"human_owner_id": "employee-2"})
    requirement = FakeRequirementDelivery(delivery_context)
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, requirement, _gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
        binding_requirement=FakeRequirementBinding(binding_context),
    )

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.blocked_reason == "OWNER_MISMATCH"
    assert requirement.blocked[0].reason_code == "OWNER_MISMATCH"
    assert gitlab.calls == []


def test_current_owner_ineligible_blocks_before_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    eligibility = FakeEligibility(eligible=False)
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, requirement, _gitlab = _dependencies(
        engine,
        eligibility=eligibility,
        gitlab=gitlab,
    )

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.blocked_reason == "OWNER_INELIGIBLE"
    assert requirement.blocked[0].reason_code == "OWNER_INELIGIBLE"
    assert gitlab.calls == []


def test_unbound_requirement_repository_blocks_before_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = FakeRequirementDelivery(
        _delivery_context().model_copy(update={"repository_state": "BLOCKED"})
    )
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, requirement, _gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
    )

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.blocked_reason == "REPOSITORY_NOT_AUTHORIZED"
    assert requirement.blocked[0].reason_code == "REPOSITORY_NOT_AUTHORIZED"
    assert gitlab.calls == []


def test_branch_binding_context_mismatch_blocks_before_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = FakeRequirementDelivery(
        _delivery_context().model_copy(update={"task_branch": "feat/other"})
    )
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, requirement, _gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
    )

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.blocked_reason == "BRANCH_BINDING_MISSING"
    assert requirement.blocked[0].reason_code == "BRANCH_BINDING_MISSING"
    assert gitlab.calls == []


def test_ambiguous_provider_list_blocks_as_mr_conflict(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.list_error = GitLabResultUnknown("ambiguous candidates")
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.blocked_reason == "MR_CONFLICT"
    assert requirement.blocked[0].reason_code == "MR_CONFLICT"


@pytest.mark.parametrize(
    "readback",
    [
        _mr_snapshot(project_id="202"),
        _mr_snapshot(source_branch="feat/other"),
        _mr_snapshot(target_branch="main"),
        _mr_snapshot(state="closed"),
    ],
    ids=("project", "source", "target", "state"),
)
def test_incompatible_readback_blocks_as_mr_conflict(
    isolated_source_control_database: Any,
    readback: GitLabMergeRequestSnapshot,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.readback = readback
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.blocked_reason == "MR_CONFLICT"
    assert requirement.blocked[0].reason_code == "MR_CONFLICT"


def test_missing_requirement_binding_dependency_fails_closed_without_claiming(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    dependencies, _requirement, _gitlab = _dependencies(engine)
    dependencies = replace(dependencies, requirement=None)

    with pytest.raises(SourceControlDependencyUnavailable):
        process_integration_mr_request(
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )

    with engine.connect() as db:
        inbox = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(MESSAGE_ID)
    assert inbox["state"] == "RECEIVED"
    assert inbox["attempts"] == 0


def test_target_branch_missing_blocks_without_planning_an_effect(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.branch_errors["dev"] = GitLabBranchNotFound("target missing")
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == "TARGET_BRANCH_NOT_FOUND"
    assert requirement.blocked[0].reason_code == "TARGET_BRANCH_NOT_FOUND"
    assert gitlab.calls == ["profile", "source_branch", "dev_branch"]


def test_provider_list_unavailable_blocks_without_post(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.list_error = GitLabProviderUnavailable("list unavailable")
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.blocked_reason == "PROVIDER_UNAVAILABLE"
    assert requirement.blocked[0].reason_code == "PROVIDER_UNAVAILABLE"
    assert "create_mr" not in gitlab.calls


def test_stale_worker_cannot_commit_facts_after_effect_and_inbox_leases_are_stolen(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)

    def steal_leases() -> None:
        with engine.begin() as db:
            repository = SqlAlchemySourceControlIntegrationRepository(db)
            effect = repository.effect_by_operation_subject(
                EffectOperation.CREATE_INTEGRATION_MR.value,
                f"work-item:{WORK_ITEM_ID}",
            )
            assert effect is not None
            stolen_effect = repository.transition_effect(
                str(effect["id"]),
                expected_state=EffectState.IN_FLIGHT.value,
                expected_attempts=effect["attempts"],
                values={
                    "state": "RECONCILIATION",
                    "attempts": effect["attempts"] + 1,
                    "next_reconcile_at": NOW + timedelta(minutes=4),
                    "updated_at": NOW + timedelta(minutes=3),
                },
            )
            stolen_inbox = repository.claim_delivery_request(
                MESSAGE_ID,
                now=NOW + timedelta(minutes=3),
                lease_until=NOW + timedelta(minutes=5),
            )
        assert stolen_effect is not None
        assert stolen_inbox is not None

    gitlab.before_readback = steal_leases
    dependencies, _requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    with engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        effect = repository.effect_by_operation_subject(
            EffectOperation.CREATE_INTEGRATION_MR.value,
            f"work-item:{WORK_ITEM_ID}",
        )
        inbox = repository.delivery_request(MESSAGE_ID)
        binding = repository.merge_request_binding_by_work_item(WORK_ITEM_ID)
    assert effect["state"] == "RECONCILIATION"
    assert effect["attempts"] == 2
    assert result.effect is not None
    assert result.effect.state is EffectState.RECONCILIATION
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert inbox["state"] == "PROCESSING"
    assert inbox["attempts"] == 2
    assert binding is None


@pytest.mark.parametrize(
    "candidate",
    [
        _mr_snapshot(project_id="202"),
        _mr_snapshot(source_branch="feat/other"),
        _mr_snapshot(target_branch="main"),
        _mr_snapshot(head_sha="d" * 40),
    ],
    ids=("project", "source", "target", "head"),
)
def test_incompatible_merge_request_candidate_blocks_as_conflict(
    isolated_source_control_database: Any,
    candidate: GitLabMergeRequestSnapshot,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.candidates = [candidate]
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.blocked_reason == "MR_CONFLICT"
    assert requirement.blocked[0].reason_code == "MR_CONFLICT"
