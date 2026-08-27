from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from typing import Any, Literal, TypedDict
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text

from control_plane.app.modules.requirement import get_requirement
from control_plane.app.modules.source_control import (
    EffectOperation,
    EffectState,
    RequirementCallbackUnavailable,
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    process_integration_mr_request,
)
from control_plane.app.modules.source_control.adapters import (
    RequirementFacadeBindingAdapter,
    RequirementFacadeDeliveryAdapter,
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
    GitLabMergeRequestLocator,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
    GitLabProjectPolicyUnsupported,
    GitLabProviderUnavailable,
    GitLabResultUnknown,
    GitLabTargetBranchNotProtected,
    IntegrationDeliveryBlockedResult,
    IntegrationMergedResult,
    IntegrationMrReadyResult,
    IntegrationReconciliationPendingResult,
    RequirementBindingContext,
    RequirementDeliveryContext,
    SourceControlIntegrationRepositoryFactory,
)
from tests.requirement.conftest import (
    IsolatedRequirementDatabase,
    _temporary_requirement_role_engine,
)
from tests.requirement.test_baseline_gate import _gate_dependencies
from tests.requirement.test_integration_delivery_relay import _requested_mr

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
SECOND_MESSAGE_ID = "30000000-0000-0000-0000-000000000702"
SECOND_REQUIREMENT_ID = "40000000-0000-0000-0000-000000000702"
SECOND_WORK_ITEM_ID = "50000000-0000-0000-0000-000000000702"


class _IntegrationEffectOverrides(TypedDict, total=False):
    request_fingerprint: str
    requirement_id: str
    branch_binding_id: str


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


class MappedRequirementBinding(FakeRequirementBinding):
    def __init__(self, contexts: dict[str, RequirementBindingContext]) -> None:
        self.contexts = contexts

    def binding_context(self, work_item_id: str) -> RequirementBindingContext:
        return self.contexts[work_item_id]


class FakeRequirementDelivery:
    def __init__(self, context: RequirementDeliveryContext | None = None) -> None:
        self.context = context or _delivery_context()
        self.ready: list[IntegrationMrReadyResult] = []
        self.blocked: list[IntegrationDeliveryBlockedResult] = []
        self.pending: list[IntegrationReconciliationPendingResult] = []
        self.external_drift: list[ExternalMergeDriftResult] = []
        self.fail_ready = False
        self.fail_blocked = False
        self.fail_pending = False
        self.ready_attempts = 0
        self.blocked_attempts = 0
        self.pending_attempts = 0
        self.external_drift_attempts = 0
        self._ready_keys: set[str] = set()
        self._blocked_keys: set[str] = set()
        self._pending_keys: set[str] = set()
        self._external_drift_keys: set[str] = set()

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
        self.ready_attempts += 1
        if self.fail_ready:
            raise RuntimeError("requirement callback unavailable")
        if result.idempotency_key in self._ready_keys:
            return
        self._ready_keys.add(result.idempotency_key)
        self.ready.append(result)

    def record_blocked(self, result: IntegrationDeliveryBlockedResult) -> None:
        self.blocked_attempts += 1
        if self.fail_blocked:
            raise RuntimeError("requirement callback unavailable")
        if result.idempotency_key in self._blocked_keys:
            return
        self._blocked_keys.add(result.idempotency_key)
        self.blocked.append(result)

    def record_pending(self, result: IntegrationReconciliationPendingResult) -> None:
        self.pending_attempts += 1
        if self.fail_pending:
            raise RuntimeError("requirement callback unavailable")
        if result.idempotency_key in self._pending_keys:
            return
        self._pending_keys.add(result.idempotency_key)
        self.pending.append(result)

    def record_merged(self, result: IntegrationMergedResult) -> None:
        raise AssertionError(result)

    def record_external_merge_drift(self, result: ExternalMergeDriftResult) -> None:
        self.external_drift_attempts += 1
        if result.idempotency_key in self._external_drift_keys:
            return
        self._external_drift_keys.add(result.idempotency_key)
        self.external_drift.append(result)


class AdvancingRequirementDelivery(FakeRequirementDelivery):
    def record_mr_ready(self, result: IntegrationMrReadyResult) -> None:
        super().record_mr_ready(result)
        self.context = self.context.model_copy(
            update={
                "requirement_revision": self.context.requirement_revision + 1,
                "requirement_state": "VERIFYING",
                "work_item_revision": self.context.work_item_revision + 1,
                "work_item_state": "VERIFYING",
                "integration_delivery_state": "MR_OPEN",
                "integration_merge_request_binding_id": result.binding_id,
            }
        )

    def record_blocked(self, result: IntegrationDeliveryBlockedResult) -> None:
        super().record_blocked(result)
        self.context = self.context.model_copy(
            update={
                "work_item_revision": self.context.work_item_revision + 1,
                "integration_delivery_state": "BLOCKED",
            }
        )

    def record_pending(self, result: IntegrationReconciliationPendingResult) -> None:
        super().record_pending(result)
        self.context = self.context.model_copy(
            update={
                "work_item_revision": self.context.work_item_revision + 1,
                "integration_delivery_state": "RECONCILIATION_PENDING",
            }
        )

    def record_external_merge_drift(self, result: ExternalMergeDriftResult) -> None:
        super().record_external_merge_drift(result)
        self.context = self.context.model_copy(
            update={
                "work_item_revision": self.context.work_item_revision + 1,
                "work_item_state": "VERIFYING",
                "integration_delivery_state": "BLOCKED",
                "integration_merge_request_binding_id": result.binding_id,
            }
        )


class MappedRequirementDelivery(FakeRequirementDelivery):
    def __init__(self, contexts: dict[str, RequirementDeliveryContext]) -> None:
        super().__init__(next(iter(contexts.values())))
        self.contexts = contexts

    def delivery_context(self, work_item_id: str) -> RequirementDeliveryContext:
        return self.contexts[work_item_id]


class BlockingCallbackRequirementDelivery(FakeRequirementDelivery):
    def __init__(self) -> None:
        super().__init__()
        self.callback_entered = Event()
        self.duplicate_callback_entered = Event()
        self.release_callback = Event()
        self._entry_lock = Lock()
        self._callback_entries = 0

    def _hold_callback(self) -> None:
        with self._entry_lock:
            self._callback_entries += 1
            if self._callback_entries == 1:
                self.callback_entered.set()
            else:
                self.duplicate_callback_entered.set()
        assert self.release_callback.wait(timeout=5)

    def record_mr_ready(self, result: IntegrationMrReadyResult) -> None:
        self._hold_callback()
        super().record_mr_ready(result)

    def record_blocked(self, result: IntegrationDeliveryBlockedResult) -> None:
        self._hold_callback()
        super().record_blocked(result)


class DependencyUnavailableCallbackRequirementDelivery(FakeRequirementDelivery):
    def __init__(self) -> None:
        super().__init__()
        self.unavailable = True

    def record_mr_ready(self, result: IntegrationMrReadyResult) -> None:
        if self.unavailable:
            self.ready_attempts += 1
            raise SourceControlDependencyUnavailable("requirement callback unavailable")
        super().record_mr_ready(result)


class FakeGitLabMergeRequests:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.calls: list[str] = []
        self.source_head = HEAD_SHA
        self.source_readback_head: str | None = None
        self.source_readback_error: Exception | None = None
        self._source_reads = 0
        self.candidates: list[GitLabMergeRequestSnapshot] = []
        self.readback = _mr_snapshot()
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.list_error: Exception | None = None
        self.profile_error: Exception | None = None
        self.branch_errors: dict[str, Exception] = {}
        self.before_readback: Callable[[], None] | None = None
        self.expected_title = f"feat: integrate {WORK_ITEM_ID}"
        self.expected_effect_head = HEAD_SHA
        self.expected_source_branch = TASK_BRANCH

    def get_project_delivery_profile(self, _repository: object) -> GitLabProjectDeliveryProfile:
        self.calls.append("profile")
        if self.profile_error is not None:
            raise self.profile_error
        if "dev" in self.branch_errors:
            raise self.branch_errors["dev"]
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
        if name == self.expected_source_branch:
            self._source_reads += 1
            if self._source_reads > 1 and self.source_readback_error is not None:
                raise self.source_readback_error
            source_head = (
                self.source_head
                if self._source_reads == 1 or self.source_readback_head is None
                else self.source_readback_head
            )
        else:
            source_head = "c" * 40
        return BranchSnapshot(
            name=name,
            commit_sha=source_head,
        )

    def list_merge_requests(
        self,
        _repository: object,
        *,
        source_branch: str,
        target_branch: str,
        state: Literal["all"] | None = None,
    ) -> list[GitLabMergeRequestSnapshot]:
        self.calls.append("list_mr")
        assert (source_branch, target_branch) == (self.expected_source_branch, "dev")
        assert state == "all"
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
    ) -> GitLabMergeRequestLocator:
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
        assert expected_head_sha == self.expected_effect_head
        assert title == self.expected_title
        assert description == (
            f"Requirement: {REQUIREMENT_ID}\n"
            f"Work-Item: {WORK_ITEM_ID}\n"
            f"Source-Control-Effect: {effect['id']}"
        )
        if self.create_error is not None:
            raise self.create_error
        return GitLabMergeRequestLocator(
            project_id="101",
            iid=17,
            source_branch=TASK_BRANCH,
            target_branch="dev",
        )

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
        requirement_revision=3,
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


def _seed_integration_effect(
    engine: Engine,
    *,
    state: EffectState = EffectState.PLANNED,
    requirement_id: str = REQUIREMENT_ID,
    request_fingerprint: str = "sha256:delivery-request",
    branch_binding_id: str = BRANCH_BINDING_ID,
    head_sha: str = HEAD_SHA,
) -> None:
    with engine.begin() as db:
        SqlAlchemySourceControlIntegrationRepository(db).insert_effect(
            id="80000000-0000-0000-0000-000000000701",
            effect_key=f"source-control:create-integration-mr:{WORK_ITEM_ID}",
            operation=EffectOperation.CREATE_INTEGRATION_MR.value,
            subject_key=f"work-item:{WORK_ITEM_ID}",
            payload={"branchBindingId": branch_binding_id, "headSha": head_sha},
            work_item_id=WORK_ITEM_ID,
            requirement_id=requirement_id,
            repository_id=REPOSITORY_ID,
            request_fingerprint=request_fingerprint,
            attempts=1 if state is EffectState.IN_FLIGHT else 0,
            next_reconcile_at=(
                NOW + timedelta(minutes=2) if state is EffectState.IN_FLIGHT else None
            ),
            state=state.value,
            requirement_callback_state="PENDING",
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
            expected_topic="requirement.integration-merge-request.requested",
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
            expected_topic="requirement.integration-merge-request.requested",
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )
    with engine.begin() as db:
        second = SqlAlchemySourceControlIntegrationRepository(db).claim_delivery_request(
            MESSAGE_ID,
            expected_topic="requirement.integration-merge-request.requested",
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


def test_mismatched_originating_requirement_revision_fails_closed_before_provider_calls(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = FakeRequirementDelivery(
        _delivery_context().model_copy(update={"requirement_revision": 4})
    )
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, _requirement, _gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
    )

    with pytest.raises(SourceControlDependencyUnavailable):
        process_integration_mr_request(
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )

    assert gitlab.calls == []


@pytest.mark.parametrize(
    "effect_values",
    [
        {"request_fingerprint": "sha256:other-request"},
        {"requirement_id": "40000000-0000-0000-0000-000000000799"},
        {"branch_binding_id": "70000000-0000-0000-0000-000000000799"},
    ],
    ids=("fingerprint", "requirement", "branch-binding"),
)
def test_existing_planned_effect_local_collision_blocks_without_provider_calls(
    isolated_source_control_database: Any,
    effect_values: _IntegrationEffectOverrides,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    _seed_integration_effect(engine, **effect_values)
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == "MR_CONFLICT"
    assert requirement.blocked[0].reason_code == "MR_CONFLICT"
    assert gitlab.calls == []


def test_existing_planned_effect_frozen_head_change_blocks_before_list_or_post(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    frozen_head = "d" * 40
    _seed_integration_effect(engine, head_sha=frozen_head)
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.payload.model_dump(by_alias=True) == {
        "branchBindingId": BRANCH_BINDING_ID,
        "headSha": frozen_head,
    }
    assert result.effect.state is EffectState.BLOCKED
    assert result.blocked_reason == "HEAD_SHA_CHANGED"
    assert requirement.blocked[0].reason_code == "HEAD_SHA_CHANGED"
    assert "list_mr" not in gitlab.calls
    assert "create_mr" not in gitlab.calls


def test_existing_in_flight_effect_replays_pending_without_provider_write(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    _seed_integration_effect(engine, state=EffectState.IN_FLIGHT)
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.IN_FLIGHT
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert len(requirement.pending) == 1
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
        "list_mr",
        "create_mr",
        "get_mr",
        "source_branch",
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
    assert gitlab.calls == ["profile", "source_branch"]
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
        "list_mr",
        "get_mr",
        "source_branch",
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
    dependencies, _returned_requirement, gitlab = _dependencies(
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


def test_terminal_ready_replay_precedes_advanced_requirement_admission(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = AdvancingRequirementDelivery()
    dependencies, _returned_requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    provider_calls = tuple(gitlab.calls)
    second = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.callback_state.value == "ACKED"
    assert second.effect is not None
    assert second.effect.callback_state.value == "ACKED"
    assert second.binding == first.binding
    assert tuple(gitlab.calls) == provider_calls
    assert requirement.ready_attempts == 1


def test_terminal_blocked_replay_precedes_advanced_requirement_admission(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = AdvancingRequirementDelivery()
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.candidates = [_mr_snapshot(), _mr_snapshot(iid=18)]
    dependencies, _returned_requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    provider_calls = tuple(gitlab.calls)
    second = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.state is EffectState.BLOCKED
    assert second.effect is not None
    assert second.effect.state is EffectState.BLOCKED
    assert second.blocked_reason == "MR_CONFLICT"
    assert tuple(gitlab.calls) == provider_calls
    assert requirement.blocked_attempts == 1


def test_terminal_pending_replay_precedes_advanced_requirement_admission(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = AdvancingRequirementDelivery()
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.create_error = GitLabResultUnknown("post timed out")
    dependencies, _returned_requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    provider_calls = tuple(gitlab.calls)
    second = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.state is EffectState.UNKNOWN
    assert second.effect is not None
    assert second.effect.state is EffectState.UNKNOWN
    assert second.blocked_reason == "RECONCILIATION_PENDING"
    assert tuple(gitlab.calls) == provider_calls
    assert requirement.pending_attempts == 1


def test_terminal_success_replay_advances_real_requirement_once_without_provider(
    isolated_source_control_database: Any,
) -> None:
    source_engine = isolated_source_control_database.runtime
    with _temporary_requirement_role_engine(
        isolated_source_control_database.owner
    ) as requirement_engine:
        requirement_database = IsolatedRequirementDatabase(
            owner=isolated_source_control_database.owner,
            runtime=requirement_engine,
        )
        requested = _requested_mr(
            requirement_database,
            key_suffix="source-control-terminal-replay",
        )
        requirement_dependencies = _gate_dependencies()
        delivery = RequirementFacadeDeliveryAdapter(
            engine=requirement_engine,
            dependencies=requirement_dependencies,
        )
        binding_requirement = RequirementFacadeBindingAdapter(
            engine=requirement_engine,
            dependencies=requirement_dependencies,
            clock=FixedClock(),
        )
        envelope = delivery.claim_requests(
            limit=1,
            lease_until=NOW + timedelta(minutes=1),
        )[0]
        delivery_context = delivery.delivery_context(requested.work_item.id)
        branch_effect_id = "60000000-0000-0000-0000-000000000901"
        branch_binding_id = "70000000-0000-0000-0000-000000000901"
        integration_effect_id = "80000000-0000-0000-0000-000000000901"
        merge_request_binding_id = "90000000-0000-0000-0000-000000000901"
        head_sha = "b" * 40
        assert delivery_context.base_commit_sha is not None
        assert delivery_context.task_branch is not None
        with source_engine.begin() as db:
            source = SqlAlchemySourceControlRepository(db)
            integration = SqlAlchemySourceControlIntegrationRepository(db)
            source.insert_workspace_repository(
                id=delivery_context.repository_id,
                workspace_id=delivery_context.workspace_id,
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
            source.insert_effect(
                id=branch_effect_id,
                effect_key=f"source-control:create-task-branch:{requested.work_item.id}",
                operation="CREATE_TASK_BRANCH",
                work_item_id=requested.work_item.id,
                requirement_id=requested.requirement.id,
                repository_id=delivery_context.repository_id,
                work_item_number=901,
                branch_name=delivery_context.task_branch,
                base_commit_sha=delivery_context.base_commit_sha,
                request_fingerprint="sha256:real-branch",
                attempts=1,
                state="SUCCEEDED",
                requirement_callback_state="ACKED",
                next_reconcile_at=None,
                completed_at=NOW,
                now=NOW,
            )
            source.insert_binding(
                id=branch_binding_id,
                work_item_id=requested.work_item.id,
                requirement_id=requested.requirement.id,
                workspace_id=delivery_context.workspace_id,
                repository_id=delivery_context.repository_id,
                work_item_number=901,
                base_commit_sha=delivery_context.base_commit_sha,
                branch_name=delivery_context.task_branch,
                effect_id=branch_effect_id,
                now=NOW,
            )
            integration.accept_delivery_request(
                message_id=envelope.message_id,
                topic=envelope.topic,
                payload_hash=envelope.payload_hash,
                requirement_id=envelope.requirement_id,
                requirement_revision=envelope.requirement_revision,
                work_item_id=envelope.work_item_id,
                work_item_revision=envelope.work_item_revision,
                repository_id=envelope.repository_id,
                actor_id=envelope.actor_id,
                integration_merge_request_binding_id=None,
                now=NOW,
            )
            claimed = integration.claim_delivery_request(
                envelope.message_id,
                expected_topic="requirement.integration-merge-request.requested",
                now=NOW,
                lease_until=NOW + timedelta(minutes=1),
            )
            assert claimed is not None
            completed = integration.complete_delivery_request(
                envelope.message_id,
                expected_attempts=claimed["attempts"],
                now=NOW,
            )
            assert completed is not None
            integration.insert_effect(
                id=integration_effect_id,
                effect_key=(f"source-control:create-integration-mr:{requested.work_item.id}"),
                operation=EffectOperation.CREATE_INTEGRATION_MR.value,
                subject_key=f"work-item:{requested.work_item.id}",
                payload={
                    "branchBindingId": branch_binding_id,
                    "headSha": head_sha,
                },
                work_item_id=requested.work_item.id,
                requirement_id=requested.requirement.id,
                repository_id=delivery_context.repository_id,
                request_fingerprint=envelope.payload_hash,
                attempts=1,
                next_reconcile_at=None,
                state=EffectState.SUCCEEDED.value,
                requirement_callback_state="PENDING",
                completed_at=NOW,
                now=NOW,
            )
            integration.insert_merge_request_binding(
                id=merge_request_binding_id,
                kind="INTEGRATION",
                work_item_id=requested.work_item.id,
                requirement_id=requested.requirement.id,
                workspace_id=delivery_context.workspace_id,
                repository_id=delivery_context.repository_id,
                branch_binding_id=branch_binding_id,
                external_project_id="101",
                merge_request_iid=17,
                source_branch=delivery_context.task_branch,
                target_branch="dev",
                create_effect_id=integration_effect_id,
                head_sha=head_sha,
                creation_origin="PLATFORM_CREATED",
                now=NOW,
            )
            integration.append_merge_request_observation(
                id="91000000-0000-0000-0000-000000000901",
                binding_id=merge_request_binding_id,
                head_sha=head_sha,
                state="OPEN",
                merge_commit_sha=None,
                external_merge_user_id=None,
                merged_at=None,
                observation_digest="sha256:real-terminal-replay",
                observed_at=NOW,
            )

        gitlab = FakeGitLabMergeRequests(source_engine)
        dependencies = SourceControlDependencies(
            repository_factory=SqlAlchemySourceControlRepository,
            engine=source_engine,
            requirement=binding_requirement,
            eligibility=FakeEligibility(),
            audit=FakeAudit(),
            clock=FixedClock(),
            random=FixedRandom(),
            policy=FixedPolicy(),
            delivery_repository_factory=SqlAlchemySourceControlIntegrationRepository,
            requirement_delivery=delivery,
            gitlab_merge_requests=gitlab,
        )

        first = process_integration_mr_request(
            message_id=envelope.message_id,
            dependencies=dependencies,
        )
        after_first = delivery.delivery_context(requested.work_item.id)
        second = process_integration_mr_request(
            message_id=envelope.message_id,
            dependencies=dependencies,
        )
        after_second = delivery.delivery_context(requested.work_item.id)

    assert first.effect is not None
    assert first.effect.callback_state.value == "ACKED"
    assert second.effect == first.effect
    assert after_first.integration_delivery_state == "MR_OPEN"
    assert after_first.integration_merge_request_binding_id == merge_request_binding_id
    assert after_second == after_first
    assert gitlab.calls == []


@pytest.mark.parametrize("candidate_state", ["closed", "merged"])
def test_create_terminal_mr_installs_real_requirement_binding_once(
    isolated_source_control_database: Any,
    candidate_state: Literal["closed", "merged"],
) -> None:
    source_engine = isolated_source_control_database.runtime
    with _temporary_requirement_role_engine(
        isolated_source_control_database.owner
    ) as requirement_engine:
        requirement_database = IsolatedRequirementDatabase(
            owner=isolated_source_control_database.owner,
            runtime=requirement_engine,
        )
        requested = _requested_mr(
            requirement_database,
            key_suffix=f"source-control-create-{candidate_state}",
        )
        requirement_dependencies = _gate_dependencies()
        delivery = RequirementFacadeDeliveryAdapter(
            engine=requirement_engine,
            dependencies=requirement_dependencies,
        )
        binding_requirement = RequirementFacadeBindingAdapter(
            engine=requirement_engine,
            dependencies=requirement_dependencies,
            clock=FixedClock(),
        )
        envelope = delivery.claim_requests(
            limit=1,
            lease_until=NOW + timedelta(minutes=1),
        )[0]
        delivery_context = delivery.delivery_context(requested.work_item.id)
        assert delivery_context.base_commit_sha is not None
        assert delivery_context.task_branch is not None
        branch_effect_id = "60000000-0000-0000-0000-000000000902"
        branch_binding_id = "70000000-0000-0000-0000-000000000902"
        with source_engine.begin() as db:
            source = SqlAlchemySourceControlRepository(db)
            integration = SqlAlchemySourceControlIntegrationRepository(db)
            source.insert_workspace_repository(
                id=delivery_context.repository_id,
                workspace_id=delivery_context.workspace_id,
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
            source.insert_effect(
                id=branch_effect_id,
                effect_key=f"source-control:create-task-branch:{requested.work_item.id}",
                operation="CREATE_TASK_BRANCH",
                work_item_id=requested.work_item.id,
                requirement_id=requested.requirement.id,
                repository_id=delivery_context.repository_id,
                work_item_number=902,
                branch_name=delivery_context.task_branch,
                base_commit_sha=delivery_context.base_commit_sha,
                request_fingerprint="sha256:real-create-terminal-branch",
                attempts=1,
                state="SUCCEEDED",
                requirement_callback_state="ACKED",
                next_reconcile_at=None,
                completed_at=NOW,
                now=NOW,
            )
            source.insert_binding(
                id=branch_binding_id,
                work_item_id=requested.work_item.id,
                requirement_id=requested.requirement.id,
                workspace_id=delivery_context.workspace_id,
                repository_id=delivery_context.repository_id,
                work_item_number=902,
                base_commit_sha=delivery_context.base_commit_sha,
                branch_name=delivery_context.task_branch,
                effect_id=branch_effect_id,
                now=NOW,
            )
            integration.accept_delivery_request(
                message_id=envelope.message_id,
                topic=envelope.topic,
                payload_hash=envelope.payload_hash,
                requirement_id=envelope.requirement_id,
                requirement_revision=envelope.requirement_revision,
                work_item_id=envelope.work_item_id,
                work_item_revision=envelope.work_item_revision,
                repository_id=envelope.repository_id,
                actor_id=envelope.actor_id,
                integration_merge_request_binding_id=None,
                now=NOW,
            )

        candidate = _mr_snapshot(
            source_branch=delivery_context.task_branch,
            state=candidate_state,
        )
        if candidate_state == "merged":
            candidate = candidate.model_copy(
                update={
                    "merge_commit_sha": "c" * 40,
                    "merge_user_id": "provider-user-17",
                    "merged_at": NOW,
                }
            )
        gitlab = FakeGitLabMergeRequests(source_engine)
        gitlab.expected_source_branch = delivery_context.task_branch
        gitlab.candidates = [candidate]
        gitlab.readback = candidate
        dependencies = SourceControlDependencies(
            repository_factory=SqlAlchemySourceControlRepository,
            engine=source_engine,
            requirement=binding_requirement,
            eligibility=FakeEligibility(),
            audit=FakeAudit(),
            clock=FixedClock(),
            random=FixedRandom(),
            policy=FixedPolicy(),
            delivery_repository_factory=SqlAlchemySourceControlIntegrationRepository,
            requirement_delivery=delivery,
            gitlab_merge_requests=gitlab,
        )

        first = process_integration_mr_request(
            message_id=envelope.message_id,
            dependencies=dependencies,
        )
        provider_calls = tuple(gitlab.calls)
        with requirement_engine.connect() as db:
            after_first = get_requirement(
                db,
                requirement_id=requested.requirement.id,
                dependencies=requirement_dependencies,
            )
        second = process_integration_mr_request(
            message_id=envelope.message_id,
            dependencies=dependencies,
        )
        with requirement_engine.connect() as db:
            after_second = get_requirement(
                db,
                requirement_id=requested.requirement.id,
                dependencies=requirement_dependencies,
            )

    assert first.effect is not None
    assert first.effect.state is EffectState.BLOCKED
    assert first.effect.callback_state.value == "ACKED"
    assert first.binding is not None
    assert first.observation is not None
    expected_reason = {
        "closed": "MR_CLOSED",
        "merged": "EXTERNAL_MERGE_DRIFT",
    }[candidate_state]
    assert first.effect.last_error_code == expected_reason
    assert first.blocked_reason == expected_reason
    assert first.observation.state.value == candidate_state.upper()
    work_item = after_first.work_items[0]
    assert work_item.integration_merge_request_binding_id == first.binding.id
    assert work_item.integration_delivery_state.value == "BLOCKED"
    assert work_item.integration_blocked_reason_code is not None
    assert work_item.integration_blocked_reason_code.value == expected_reason
    assert work_item.state.value == ("IN_PROGRESS" if candidate_state == "closed" else "VERIFYING")
    assert after_first.requirement.state.value == (
        "IN_PROGRESS" if candidate_state == "closed" else "VERIFYING"
    )
    assert after_first.requirement.revision == requested.requirement.revision + (
        0 if candidate_state == "closed" else 1
    )
    assert second.effect == first.effect
    assert after_second == after_first
    assert tuple(gitlab.calls) == provider_calls


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


class CommitThenRaiseCompleteInboxRepository(SqlAlchemySourceControlIntegrationRepository):
    def __init__(self, db: Connection, state: dict[str, bool]) -> None:
        super().__init__(db)
        self.state = state

    def complete_delivery_request(
        self,
        message_id: str,
        *,
        expected_attempts: int,
        now: datetime,
    ) -> object:
        completed = super().complete_delivery_request(
            message_id,
            expected_attempts=expected_attempts,
            now=now,
        )
        if completed is not None and not self.state["raised"]:
            self.state["raised"] = True
            self.db.commit()
            raise RuntimeError("local commit acknowledgement was lost")
        return completed


class CommitThenRaiseCompleteInboxRepositoryFactory:
    def __init__(self) -> None:
        self.state = {"raised": False}

    def __call__(self, db: Connection) -> CommitThenRaiseCompleteInboxRepository:
        return CommitThenRaiseCompleteInboxRepository(db, self.state)


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


def test_committed_source_control_facts_survive_local_commit_ack_loss(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    factory = CommitThenRaiseCompleteInboxRepositoryFactory()
    dependencies, requirement, _gitlab = _dependencies(
        engine,
        delivery_repository_factory=factory,
    )

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.SUCCEEDED
    assert result.binding is not None
    assert result.observation is not None
    assert len(requirement.ready) == 1
    with engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        binding = repository.merge_request_binding_by_work_item(WORK_ITEM_ID)
        assert binding is not None
        observation_count = db.execute(
            text(
                "SELECT count(*) FROM source_control.merge_request_observation "
                "WHERE binding_id = CAST(:binding_id AS uuid)"
            ),
            {"binding_id": str(binding["id"])},
        ).scalar_one()
        inbox = repository.delivery_request(MESSAGE_ID)
    assert observation_count == 1
    assert inbox is not None
    assert inbox["state"] == "PROCESSED"


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


def test_dependency_unavailable_callback_is_durably_failed_for_terminal_replay(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = DependencyUnavailableCallbackRequirementDelivery()
    dependencies, _requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    first_provider_calls = tuple(gitlab.calls)
    requirement.unavailable = False
    replay = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.state is EffectState.SUCCEEDED
    assert first.effect.callback_state.value == "FAILED"
    assert replay.effect is not None
    assert replay.effect.callback_state.value == "ACKED"
    assert requirement.ready_attempts == 2
    assert len(requirement.ready) == 1
    assert tuple(gitlab.calls) == first_provider_calls
    assert "create_mr" in gitlab.calls


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
    assert requirement.blocked[0].idempotency_key == (
        f"source-control:integration-blocked:{MESSAGE_ID}:{WORK_ITEM_ID}:NO_DELIVERY_COMMIT"
    )
    assert gitlab.calls == [
        "profile",
        "source_branch",
    ]
    with engine.connect() as db:
        inbox = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(MESSAGE_ID)
    assert inbox is not None
    assert inbox["state"] == "PROCESSED"
    assert inbox["attempts"] == 2
    assert inbox["last_error_code"] == "NO_DELIVERY_COMMIT"


def test_preflight_callback_success_survives_inbox_completion_ack_loss(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = FakeRequirementDelivery()
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.source_head = BASE_SHA
    clock = MutableClock()
    dependencies, requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
        clock=clock,
        delivery_repository_factory=CommitThenRaiseCompleteInboxRepositoryFactory(),
    )

    with pytest.raises(RuntimeError, match="acknowledgement was lost"):
        process_integration_mr_request(
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )
    clock.current = NOW + timedelta(minutes=3)
    replay = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert replay.blocked_reason == "NO_DELIVERY_COMMIT"
    assert len(requirement.blocked) == 1
    assert requirement.blocked_attempts == 1
    with engine.connect() as db:
        inbox = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(MESSAGE_ID)
    assert inbox["state"] == "PROCESSED"
    assert inbox["attempts"] == 1
    assert inbox["last_error_code"] == "NO_DELIVERY_COMMIT"


def test_preflight_callback_keys_are_scoped_to_message_and_work_item(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    with engine.begin() as db:
        SqlAlchemySourceControlIntegrationRepository(db).accept_delivery_request(
            message_id=SECOND_MESSAGE_ID,
            topic="requirement.integration-merge-request.requested",
            payload_hash="sha256:second-delivery-request",
            requirement_id=SECOND_REQUIREMENT_ID,
            requirement_revision=3,
            work_item_id=SECOND_WORK_ITEM_ID,
            work_item_revision=5,
            repository_id=REPOSITORY_ID,
            actor_id="employee-1",
            integration_merge_request_binding_id=None,
            now=NOW,
        )
    delivery_contexts = {
        WORK_ITEM_ID: _delivery_context().model_copy(update={"human_owner_id": "employee-2"}),
        SECOND_WORK_ITEM_ID: _delivery_context().model_copy(
            update={
                "requirement_id": SECOND_REQUIREMENT_ID,
                "work_item_id": SECOND_WORK_ITEM_ID,
                "human_owner_id": "employee-2",
            }
        ),
    }
    binding_contexts = {
        WORK_ITEM_ID: _binding_context().model_copy(update={"human_owner_id": "employee-2"}),
        SECOND_WORK_ITEM_ID: _binding_context().model_copy(
            update={
                "requirement_id": SECOND_REQUIREMENT_ID,
                "work_item_id": SECOND_WORK_ITEM_ID,
                "human_owner_id": "employee-2",
            }
        ),
    }
    requirement = MappedRequirementDelivery(delivery_contexts)
    gitlab = FakeGitLabMergeRequests(engine)
    dependencies, _returned_requirement, gitlab = _dependencies(
        engine,
        requirement=requirement,
        binding_requirement=MappedRequirementBinding(binding_contexts),
        gitlab=gitlab,
    )

    first = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )
    second = process_integration_mr_request(
        message_id=SECOND_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.blocked_reason == "OWNER_MISMATCH"
    assert second.blocked_reason == "OWNER_MISMATCH"
    assert [result.idempotency_key for result in requirement.blocked] == [
        f"source-control:integration-blocked:{MESSAGE_ID}:{WORK_ITEM_ID}:OWNER_MISMATCH",
        (
            "source-control:integration-blocked:"
            f"{SECOND_MESSAGE_ID}:{SECOND_WORK_ITEM_ID}:OWNER_MISMATCH"
        ),
    ]
    assert gitlab.calls == []


def test_readback_head_drift_appends_latest_observation_without_mutating_effect_payload(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    moved_head = "d" * 40
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.readback = _mr_snapshot(head_sha=moved_head)
    gitlab.source_readback_head = moved_head
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
    assert result.binding.head_sha == moved_head
    assert result.observation is not None
    assert result.observation.head_sha == moved_head


@pytest.mark.parametrize(
    ("readback_head", "source_readback_head", "source_readback_error"),
    [
        ("d" * 40, HEAD_SHA, None),
        (HEAD_SHA, HEAD_SHA, GitLabProviderUnavailable("second source read unavailable")),
    ],
    ids=("head-mismatch", "second-source-unavailable"),
)
def test_final_mr_and_source_branch_double_read_failure_is_unknown(
    isolated_source_control_database: Any,
    readback_head: str,
    source_readback_head: str,
    source_readback_error: Exception | None,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.readback = _mr_snapshot(head_sha=readback_head)
    gitlab.source_readback_head = source_readback_head
    gitlab.source_readback_error = source_readback_error
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

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
    assert gitlab.calls[-2:] == ["get_mr", "source_branch"]


def test_adopt_candidate_head_is_decided_only_by_final_double_read_proof(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.candidates = [_mr_snapshot(head_sha="d" * 40)]
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.SUCCEEDED
    assert result.binding is not None
    assert result.binding.creation_origin.value == "EXTERNAL_ADOPTED"
    assert len(requirement.ready) == 1
    assert "create_mr" not in gitlab.calls
    assert gitlab.calls[-2:] == ["get_mr", "source_branch"]


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


def test_ambiguous_provider_list_enters_reconciliation(
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
    assert result.effect.state is EffectState.UNKNOWN
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert len(requirement.pending) == 1


@pytest.mark.parametrize(
    "readback",
    [
        _mr_snapshot(project_id="202"),
        _mr_snapshot(source_branch="feat/other"),
        _mr_snapshot(target_branch="main"),
    ],
    ids=("project", "source", "target"),
)
def test_incompatible_final_readback_enters_reconciliation(
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
    assert result.effect.state is EffectState.UNKNOWN
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert len(requirement.pending) == 1


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
    assert gitlab.calls == ["profile"]


def test_provider_list_unavailable_enters_reconciliation_without_post(
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
    assert result.effect.state is EffectState.UNKNOWN
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert len(requirement.pending) == 1
    assert "create_mr" not in gitlab.calls


def test_provider_transient_before_effect_releases_inbox_without_blocked_callback(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.profile_error = GitLabProviderUnavailable("profile unavailable")
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    with pytest.raises(RequirementCallbackUnavailable):
        process_integration_mr_request(
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )

    with engine.connect() as db:
        inbox = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(MESSAGE_ID)
    assert inbox["state"] == "FAILED"
    assert inbox["last_error_code"] == "PROVIDER_UNAVAILABLE"
    assert requirement.blocked == []
    assert requirement.pending == []


@pytest.mark.parametrize(
    ("profile_error", "branch_error", "reason_code"),
    [
        (GitLabProjectPolicyUnsupported("unsupported policy"), None, "PROJECT_PROFILE_UNSUPPORTED"),
        (None, GitLabTargetBranchNotProtected("dev is unprotected"), "TARGET_BRANCH_NOT_PROTECTED"),
    ],
    ids=("project-policy", "target-protection"),
)
def test_stable_provider_policy_errors_are_allowlisted_preflight_blocks(
    isolated_source_control_database: Any,
    profile_error: Exception | None,
    branch_error: Exception | None,
    reason_code: str,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.profile_error = profile_error
    if branch_error is not None:
        gitlab.branch_errors["dev"] = branch_error
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == reason_code
    assert requirement.blocked[0].reason_code == reason_code


def test_list_all_states_treats_every_exact_mr_as_a_candidate(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.candidates = [
        _mr_snapshot(iid=17, state="opened"),
        _mr_snapshot(iid=18, state="closed"),
        _mr_snapshot(iid=19, state="merged"),
    ]
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.blocked_reason == "MR_CONFLICT"
    assert len(requirement.blocked) == 1
    assert "create_mr" not in gitlab.calls


@pytest.mark.parametrize("candidate_state", ["opened", "closed", "merged", "locked"])
def test_final_provider_state_selects_exact_create_effect_and_requirement_outcome(
    isolated_source_control_database: Any,
    candidate_state: Literal["opened", "closed", "merged", "locked"],
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    candidate = _mr_snapshot(state=candidate_state)
    if candidate_state == "merged":
        candidate = candidate.model_copy(
            update={
                "merge_commit_sha": "c" * 40,
                "merge_user_id": "provider-user-17",
                "merged_at": NOW,
            }
        )
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.candidates = [candidate]
    gitlab.readback = candidate
    requirement = AdvancingRequirementDelivery()
    dependencies, _returned_requirement, _gitlab = _dependencies(
        engine,
        gitlab=gitlab,
        requirement=requirement,
    )

    result = process_integration_mr_request(
        message_id=MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    if candidate_state == "opened":
        assert result.effect.state is EffectState.SUCCEEDED
        assert result.blocked_reason is None
        assert result.binding is not None
        assert result.observation is not None
        assert result.observation.state.value == "OPEN"
        assert len(requirement.ready) == 1
        assert requirement.blocked == []
        assert requirement.external_drift == []
        assert requirement.context.integration_delivery_state == "MR_OPEN"
    elif candidate_state == "closed":
        assert result.effect.state is EffectState.BLOCKED
        assert result.effect.last_error_code == "MR_CLOSED"
        assert result.blocked_reason == "MR_CLOSED"
        assert result.binding is not None
        assert result.observation is not None
        assert result.observation.state.value == "CLOSED"
        assert requirement.ready == []
        assert requirement.external_drift == []
        assert [blocked.reason_code for blocked in requirement.blocked] == ["MR_CLOSED"]
        assert requirement.blocked[0].binding_id == result.binding.id
        assert requirement.context.integration_delivery_state == "BLOCKED"
    elif candidate_state == "merged":
        assert result.effect.state is EffectState.BLOCKED
        assert result.effect.last_error_code == "EXTERNAL_MERGE_DRIFT"
        assert result.blocked_reason == "EXTERNAL_MERGE_DRIFT"
        assert result.binding is not None
        assert result.observation is not None
        assert result.observation.state.value == "MERGED"
        assert requirement.ready == []
        assert requirement.blocked == []
        assert [drift.binding_id for drift in requirement.external_drift] == [result.binding.id]
        assert requirement.context.integration_delivery_state == "BLOCKED"
    else:
        assert result.effect.state is EffectState.UNKNOWN
        assert result.blocked_reason == "RECONCILIATION_PENDING"
        assert result.binding is None
        assert result.observation is None
        assert requirement.ready == []
        assert requirement.blocked == []
        assert requirement.external_drift == []
        assert len(requirement.pending) == 1
        assert requirement.context.integration_delivery_state == "RECONCILIATION_PENDING"
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
                expected_topic="requirement.integration-merge-request.requested",
                now=NOW + timedelta(minutes=3),
                lease_until=NOW + timedelta(minutes=5),
            )
        assert stolen_effect is not None
        assert stolen_inbox is not None

    gitlab.before_readback = steal_leases
    dependencies, requirement, _gitlab = _dependencies(engine, gitlab=gitlab)

    with pytest.raises(RequirementCallbackUnavailable):
        process_integration_mr_request(
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
        observation_count = db.execute(
            text("SELECT count(*) FROM source_control.merge_request_observation")
        ).scalar_one()
    assert effect["state"] == "RECONCILIATION"
    assert effect["attempts"] == 2
    assert effect["requirement_callback_state"] == "PENDING"
    assert inbox["state"] == "PROCESSING"
    assert inbox["attempts"] == 2
    assert binding is None
    assert observation_count == 0
    assert requirement.ready == []
    assert requirement.blocked == []
    assert requirement.pending == []


def test_effect_callback_holds_fence_until_requirement_and_callback_state_commit(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = BlockingCallbackRequirementDelivery()
    dependencies, _returned_requirement, _gitlab = _dependencies(
        engine,
        requirement=requirement,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            process_integration_mr_request,
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )
        assert requirement.callback_entered.wait(timeout=5)
        second = executor.submit(
            process_integration_mr_request,
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )
        callback_interleaved = requirement.duplicate_callback_entered.wait(timeout=0.5)
        requirement.release_callback.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert not callback_interleaved
    assert first_result.effect is not None
    assert first_result.effect.callback_state.value == "ACKED"
    assert second_result.effect is not None
    assert second_result.effect.callback_state.value == "ACKED"
    assert requirement.ready_attempts == 1


def test_preflight_callback_holds_inbox_fence_until_completion(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_source_control(engine)
    requirement = BlockingCallbackRequirementDelivery()
    gitlab = FakeGitLabMergeRequests(engine)
    gitlab.source_head = BASE_SHA
    clock = MutableClock()
    dependencies, _returned_requirement, _gitlab = _dependencies(
        engine,
        requirement=requirement,
        gitlab=gitlab,
        clock=clock,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            process_integration_mr_request,
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )
        assert requirement.callback_entered.wait(timeout=5)
        clock.current = NOW + timedelta(minutes=3)
        second = executor.submit(
            process_integration_mr_request,
            message_id=MESSAGE_ID,
            dependencies=dependencies,
        )
        callback_interleaved = requirement.duplicate_callback_entered.wait(timeout=0.5)
        requirement.release_callback.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert not callback_interleaved
    assert first_result.blocked_reason == "NO_DELIVERY_COMMIT"
    assert second_result.blocked_reason == "NO_DELIVERY_COMMIT"
    assert requirement.blocked_attempts == 1
    with engine.connect() as db:
        inbox = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(MESSAGE_ID)
    assert inbox is not None
    assert inbox["state"] == "PROCESSED"


@pytest.mark.parametrize(
    "candidate",
    [
        _mr_snapshot(project_id="202"),
        _mr_snapshot(source_branch="feat/other"),
        _mr_snapshot(target_branch="main"),
    ],
    ids=("project", "source", "target"),
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
