from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any, Literal, TypedDict

import pytest
from sqlalchemy import Engine, text

from control_plane.app.modules.source_control import (
    EffectOperation,
    EffectState,
    RequirementCallbackUnavailable,
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    process_integration_merge_request,
    process_integration_mr_request,
)
from control_plane.app.modules.source_control.adapters import (
    RequirementFacadeBindingAdapter,
    RequirementFacadeDeliveryAdapter,
    SqlAlchemySourceControlIntegrationRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.domain.reasons import SourceControlReason
from control_plane.app.modules.source_control.ports import (
    ActorEligibilityContext,
    BindingEligibility,
    BranchSnapshot,
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabMergeRequestBlocked,
    GitLabMergeRequestHeadChanged,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectNotFound,
    GitLabProviderUnavailable,
    GitLabResultUnknown,
    IntegrationDeliveryBlockedResult,
    IntegrationMergedResult,
    RequirementBindingContext,
    RequirementDeliveryContext,
    SourceControlIntegrationRepositoryFactory,
)
from tests.requirement.conftest import (
    IsolatedRequirementDatabase,
    _temporary_requirement_role_engine,
)
from tests.requirement.test_baseline_gate import _gate_dependencies
from tests.requirement.test_integration_delivery_relay import (
    BINDING_ID as REAL_MR_BINDING_ID,
)
from tests.requirement.test_integration_delivery_relay import _requested_merge
from tests.source_control.test_integration_mr_saga import (
    BASE_SHA,
    BRANCH_BINDING_ID,
    HEAD_SHA,
    REPOSITORY_ID,
    REQUIREMENT_ID,
    TASK_BRANCH,
    WORK_ITEM_ID,
    WORKSPACE_ID,
    CommitThenRaiseCompleteInboxRepositoryFactory,
    FakeAudit,
    FakeEligibility,
    FakeRequirementBinding,
    FakeRequirementDelivery,
    FixedPolicy,
    FixedRandom,
    MutableClock,
    _seed_source_control,
)
from tests.source_control.test_integration_mr_saga import (
    MESSAGE_ID as CREATE_MESSAGE_ID,
)

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
MERGE_MESSAGE_ID = "30000000-0000-0000-0000-000000000801"
CREATE_EFFECT_ID = "80000000-0000-0000-0000-000000000801"
MR_BINDING_ID = "81000000-0000-0000-0000-000000000801"
MERGE_COMMIT_SHA = "d" * 40
REAL_BRANCH_EFFECT_ID = "60000000-0000-0000-0000-000000000891"
REAL_BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000891"
REAL_CREATE_EFFECT_ID = "80000000-0000-0000-0000-000000000891"
REAL_OPEN_OBSERVATION_ID = "82000000-0000-0000-0000-000000000891"


class _MergeEffectOverrides(TypedDict, total=False):
    request_fingerprint: str
    requirement_id: str
    effect_binding_id: str


class FixedClock:
    def now(self) -> datetime:
        return NOW


class MergeEligibility:
    def __init__(self, eligible: bool = True) -> None:
        self.eligible = eligible
        self.seen: list[ActorEligibilityContext] = []

    def evaluate(self, context: ActorEligibilityContext) -> BindingEligibility:
        self.seen.append(context)
        return BindingEligibility(
            eligible=self.eligible,
            reason_code=(None if self.eligible else SourceControlReason.OWNER_INELIGIBLE),
        )


class MergeRequirementDelivery(FakeRequirementDelivery):
    def __init__(self, context: RequirementDeliveryContext) -> None:
        super().__init__(context)
        self.merged: list[IntegrationMergedResult] = []
        self.merged_attempts = 0
        self.fail_merged = False
        self._merged_keys: set[str] = set()

    def record_merged(self, result: IntegrationMergedResult) -> None:
        self.merged_attempts += 1
        if self.fail_merged:
            raise RuntimeError("requirement callback unavailable")
        if result.idempotency_key in self._merged_keys:
            return
        self._merged_keys.add(result.idempotency_key)
        self.merged.append(result)
        self.context = self.context.model_copy(
            update={
                "work_item_revision": self.context.work_item_revision + 1,
                "integration_delivery_state": "INTEGRATED",
            }
        )


class CommitThenRaiseBlockedMergeRequirement(MergeRequirementDelivery):
    def record_blocked(self, result: IntegrationDeliveryBlockedResult) -> None:
        super().record_blocked(result)
        if self.blocked_attempts == 1:
            raise RequirementCallbackUnavailable("Requirement callback commit acknowledgement lost")


class CommitThenRaiseMergedRequirement:
    def __init__(self, delegate: RequirementFacadeDeliveryAdapter) -> None:
        self.delegate = delegate
        self.merged_attempts = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def record_merged(self, result: IntegrationMergedResult) -> None:
        self.merged_attempts += 1
        self.delegate.record_merged(result)
        if self.merged_attempts == 1:
            raise RequirementCallbackUnavailable("Requirement callback commit acknowledgement lost")


class CommitThenRaiseBlockedRequirement:
    def __init__(self, delegate: RequirementFacadeDeliveryAdapter) -> None:
        self.delegate = delegate
        self.blocked_attempts = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def record_blocked(self, result: Any) -> None:
        self.blocked_attempts += 1
        self.delegate.record_blocked(result)
        if self.blocked_attempts == 1:
            raise RequirementCallbackUnavailable("Requirement callback commit acknowledgement lost")


@dataclass(frozen=True, slots=True)
class RealMergeCase:
    work_item_id: str
    message_id: str
    delivery: RequirementFacadeDeliveryAdapter
    dependencies: SourceControlDependencies
    gitlab: "FakeMergeGitLab"


def _opened_snapshot(*, head_sha: str = HEAD_SHA) -> GitLabMergeRequestSnapshot:
    return GitLabMergeRequestSnapshot(
        project_id="101",
        iid=17,
        source_branch=TASK_BRANCH,
        target_branch="dev",
        head_sha=head_sha,
        state="opened",
        detailed_merge_status="mergeable",
        has_conflicts=False,
        blocking_discussions_resolved=True,
        head_pipeline_status="success",
        merge_commit_sha=None,
        merge_user_id=None,
        merged_at=None,
    )


def _merged_snapshot(*, head_sha: str = HEAD_SHA) -> GitLabMergeRequestSnapshot:
    return _opened_snapshot(head_sha=head_sha).model_copy(
        update={
            "state": "merged",
            "merge_commit_sha": MERGE_COMMIT_SHA,
            "merge_user_id": "provider-user-17",
            "merged_at": NOW,
        }
    )


class FakeMergeGitLab:
    def __init__(
        self,
        engine: Engine,
        *,
        work_item_id: str = WORK_ITEM_ID,
        binding_id: str = MR_BINDING_ID,
        source_branch: str = TASK_BRANCH,
    ) -> None:
        self.engine = engine
        self.work_item_id = work_item_id
        self.binding_id = binding_id
        self.expected_source_branch = source_branch
        self.calls: list[str] = []
        self.merge_calls: list[tuple[int, str]] = []
        self.source_head = HEAD_SHA
        self.expected_effect_head = HEAD_SHA
        self._merged = False
        self.before_merge: Callable[[], None] | None = None
        self.after_preflight_read: Callable[[], None] | None = None
        self.before_readback: Callable[[], None] | None = None
        self.profile_error: Exception | None = None
        self.preflight_snapshot: GitLabMergeRequestSnapshot | None = None
        self.post_merge_snapshot: GitLabMergeRequestSnapshot | None = None
        self.merge_error: Exception | None = None
        self.merge_happened_before_error = False
        self.get_after_merge_error: Exception | None = None
        self.source_after_merge_error: Exception | None = None
        self.provider_default_branch = "main"
        self.expected_effect_state = EffectState.IN_FLIGHT

    def get_project_delivery_profile(self, _repository: object) -> Any:
        from control_plane.app.modules.source_control.ports import (
            GitLabProjectDeliveryProfile,
        )

        self.calls.append("profile")
        if self.profile_error is not None:
            raise self.profile_error
        return GitLabProjectDeliveryProfile(
            project_id="101",
            project_path="platform/backend",
            default_branch=self.provider_default_branch,
            merge_method="merge",
        )

    def get_branch(self, _repository: object, name: str) -> BranchSnapshot:
        self.calls.append("dev_branch" if name == "dev" else "source_branch")
        if (
            self._merged
            and name == self.expected_source_branch
            and self.source_after_merge_error is not None
        ):
            raise self.source_after_merge_error
        return BranchSnapshot(
            name=name,
            commit_sha=(self.source_head if name == self.expected_source_branch else "c" * 40),
        )

    def list_merge_requests(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError("merge saga must not list merge requests")

    def create_merge_request(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError("merge saga must not create merge requests")

    def get_merge_request(
        self,
        _repository: object,
        *,
        iid: int,
    ) -> GitLabMergeRequestSnapshot:
        self.calls.append("get_mr")
        assert iid == 17
        if self._merged and self.before_readback is not None:
            callback = self.before_readback
            self.before_readback = None
            callback()
        if self._merged and self.get_after_merge_error is not None:
            raise self.get_after_merge_error
        if not self._merged and self.after_preflight_read is not None:
            callback = self.after_preflight_read
            self.after_preflight_read = None
            callback()
        if self._merged and self.post_merge_snapshot is not None:
            return self.post_merge_snapshot
        if not self._merged and self.preflight_snapshot is not None:
            return self.preflight_snapshot
        snapshot = (
            _merged_snapshot(head_sha=self.source_head)
            if self._merged
            else _opened_snapshot(head_sha=self.source_head)
        )
        return snapshot.model_copy(update={"source_branch": self.expected_source_branch})

    def merge_merge_request(
        self,
        _repository: object,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot:
        self.calls.append("merge_mr")
        self.merge_calls.append((iid, expected_head_sha))
        with self.engine.connect() as db:
            effect = SqlAlchemySourceControlIntegrationRepository(db).effect_by_operation_subject(
                EffectOperation.MERGE_INTEGRATION_MR.value,
                f"mr:{self.binding_id}:{self.expected_effect_head}",
            )
        assert effect is not None
        assert effect["state"] == self.expected_effect_state.value
        assert dict(effect["payload"]) == {
            "bindingId": self.binding_id,
            "requestedHeadSha": self.expected_effect_head,
        }
        if self.before_merge is not None:
            self.before_merge()
        if self.merge_error is not None:
            self._merged = self.merge_happened_before_error
            raise self.merge_error
        self._merged = True
        return _merged_snapshot(head_sha=self.source_head).model_copy(
            update={"source_branch": self.expected_source_branch}
        )


def _delivery_context() -> RequirementDeliveryContext:
    return RequirementDeliveryContext(
        requirement_id=REQUIREMENT_ID,
        requirement_revision=4,
        requirement_state="VERIFYING",
        workspace_id=WORKSPACE_ID,
        work_item_id=WORK_ITEM_ID,
        work_item_revision=7,
        work_item_state="VERIFYING",
        repository_id=REPOSITORY_ID,
        repository_state="BOUND",
        human_owner_id="employee-1",
        required_capabilities=("work_item.execute",),
        base_commit_sha=BASE_SHA,
        task_branch=TASK_BRANCH,
        integration_delivery_state="MERGE_PENDING",
        integration_merge_request_binding_id=MR_BINDING_ID,
        request_actor_id="employee-1",
    )


def _binding_context() -> RequirementBindingContext:
    return RequirementBindingContext(
        requirement_id=REQUIREMENT_ID,
        requirement_type="feat",
        requirement_title="Integration MR",
        workspace_id=WORKSPACE_ID,
        work_item_id=WORK_ITEM_ID,
        work_item_revision=7,
        repository_id=REPOSITORY_ID,
        assignment_state="ASSIGNED",
        human_owner_id="employee-1",
        required_capabilities=("work_item.execute",),
    )


def _seed_merge_request(
    engine: Engine,
    *,
    historical_head: str = HEAD_SHA,
    default_branch: str = "main",
    external_project_id: str = "101",
) -> None:
    _seed_source_control(engine, default_branch=default_branch)
    with engine.begin() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        repository.insert_effect(
            id=CREATE_EFFECT_ID,
            effect_key=f"source-control:create-integration-mr:{WORK_ITEM_ID}",
            operation=EffectOperation.CREATE_INTEGRATION_MR.value,
            subject_key=f"work-item:{WORK_ITEM_ID}",
            payload={"branchBindingId": BRANCH_BINDING_ID, "headSha": historical_head},
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            repository_id=REPOSITORY_ID,
            request_fingerprint="sha256:create-delivery-request",
            attempts=1,
            next_reconcile_at=None,
            state=EffectState.SUCCEEDED.value,
            requirement_callback_state="ACKED",
            completed_at=NOW,
            now=NOW,
        )
        repository.insert_merge_request_binding(
            id=MR_BINDING_ID,
            kind="INTEGRATION",
            work_item_id=WORK_ITEM_ID,
            requirement_id=REQUIREMENT_ID,
            workspace_id=WORKSPACE_ID,
            repository_id=REPOSITORY_ID,
            branch_binding_id=BRANCH_BINDING_ID,
            external_project_id=external_project_id,
            merge_request_iid=17,
            source_branch=TASK_BRANCH,
            target_branch="dev",
            create_effect_id=CREATE_EFFECT_ID,
            head_sha=historical_head,
            creation_origin="PLATFORM_CREATED",
            now=NOW,
        )
        repository.append_merge_request_observation(
            id="82000000-0000-0000-0000-000000000801",
            binding_id=MR_BINDING_ID,
            head_sha=historical_head,
            state="OPEN",
            merge_commit_sha=None,
            external_merge_user_id=None,
            merged_at=None,
            observation_digest="sha256:open-merge-request",
            observed_at=NOW,
        )
        repository.accept_delivery_request(
            message_id=MERGE_MESSAGE_ID,
            topic="requirement.integration-merge.requested",
            payload_hash="sha256:merge-delivery-request",
            requirement_id=REQUIREMENT_ID,
            requirement_revision=4,
            work_item_id=WORK_ITEM_ID,
            work_item_revision=7,
            repository_id=REPOSITORY_ID,
            actor_id="employee-1",
            integration_merge_request_binding_id=MR_BINDING_ID,
            now=NOW,
        )


def _seed_planned_merge_effect(
    engine: Engine,
    *,
    frozen_head: str,
    effect_id: str = "83000000-0000-0000-0000-000000000801",
    state: EffectState = EffectState.PLANNED,
    request_fingerprint: str = "sha256:merge-delivery-request",
    requirement_id: str = REQUIREMENT_ID,
    effect_binding_id: str = MR_BINDING_ID,
) -> None:
    with engine.begin() as db:
        SqlAlchemySourceControlIntegrationRepository(db).insert_effect(
            id=effect_id,
            effect_key=(f"source-control:merge-integration-mr:{effect_binding_id}:{frozen_head}"),
            operation=EffectOperation.MERGE_INTEGRATION_MR.value,
            subject_key=f"mr:{effect_binding_id}:{frozen_head}",
            payload={
                "bindingId": effect_binding_id,
                "requestedHeadSha": frozen_head,
            },
            work_item_id=WORK_ITEM_ID,
            requirement_id=requirement_id,
            repository_id=REPOSITORY_ID,
            request_fingerprint=request_fingerprint,
            attempts=1 if state is not EffectState.PLANNED else 0,
            next_reconcile_at=(NOW if state is not EffectState.PLANNED else None),
            state=state.value,
            requirement_callback_state="PENDING",
            now=NOW,
        )


def _dependencies(
    engine: Engine,
    *,
    requirement: MergeRequirementDelivery | None = None,
    audit: FakeAudit | None = None,
    clock: FixedClock | MutableClock | None = None,
    delivery_repository_factory: SourceControlIntegrationRepositoryFactory = (
        SqlAlchemySourceControlIntegrationRepository
    ),
) -> tuple[
    SourceControlDependencies,
    MergeRequirementDelivery,
    MergeEligibility,
    FakeMergeGitLab,
]:
    requirement = requirement or MergeRequirementDelivery(_delivery_context())
    eligibility = MergeEligibility()
    gitlab = FakeMergeGitLab(engine)
    return (
        SourceControlDependencies(
            repository_factory=SqlAlchemySourceControlRepository,
            engine=engine,
            requirement=FakeRequirementBinding(_binding_context()),
            eligibility=eligibility,
            audit=audit or FakeAudit(),
            clock=clock or FixedClock(),
            random=FixedRandom(),
            policy=FixedPolicy(),
            delivery_repository_factory=delivery_repository_factory,
            requirement_delivery=requirement,
            gitlab_merge_requests=gitlab,
        ),
        requirement,
        eligibility,
        gitlab,
    )


def _seed_real_requirement_merge_case(
    source_engine: Engine,
    requirement_engine: Engine,
    *,
    key_suffix: str,
    callback_ack_loss: bool = False,
    blocked_callback_ack_loss: bool = False,
) -> RealMergeCase:
    requirement_database = IsolatedRequirementDatabase(
        owner=source_engine,
        runtime=requirement_engine,
    )
    requested = _requested_merge(requirement_database, key_suffix=key_suffix)
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
    envelopes = delivery.claim_requests(
        limit=2,
        lease_until=NOW + timedelta(minutes=1),
    )
    envelope = next(
        item for item in envelopes if item.topic == "requirement.integration-merge.requested"
    )
    context = delivery.delivery_context(requested.work_item.id)
    assert context.base_commit_sha is not None
    assert context.task_branch is not None
    assert context.integration_merge_request_binding_id == REAL_MR_BINDING_ID
    with source_engine.begin() as db:
        source = SqlAlchemySourceControlRepository(db)
        integration = SqlAlchemySourceControlIntegrationRepository(db)
        source.insert_workspace_repository(
            id=context.repository_id,
            workspace_id=context.workspace_id,
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
            id=REAL_BRANCH_EFFECT_ID,
            effect_key=f"source-control:create-task-branch:{requested.work_item.id}",
            operation="CREATE_TASK_BRANCH",
            work_item_id=requested.work_item.id,
            requirement_id=requested.requirement.id,
            repository_id=context.repository_id,
            work_item_number=891,
            branch_name=context.task_branch,
            base_commit_sha=context.base_commit_sha,
            request_fingerprint="sha256:real-merge-branch",
            attempts=1,
            state="SUCCEEDED",
            requirement_callback_state="ACKED",
            next_reconcile_at=None,
            completed_at=NOW,
            now=NOW,
        )
        source.insert_binding(
            id=REAL_BRANCH_BINDING_ID,
            work_item_id=requested.work_item.id,
            requirement_id=requested.requirement.id,
            workspace_id=context.workspace_id,
            repository_id=context.repository_id,
            work_item_number=891,
            base_commit_sha=context.base_commit_sha,
            branch_name=context.task_branch,
            effect_id=REAL_BRANCH_EFFECT_ID,
            now=NOW,
        )
        integration.insert_effect(
            id=REAL_CREATE_EFFECT_ID,
            effect_key=f"source-control:create-integration-mr:{requested.work_item.id}",
            operation=EffectOperation.CREATE_INTEGRATION_MR.value,
            subject_key=f"work-item:{requested.work_item.id}",
            payload={"branchBindingId": REAL_BRANCH_BINDING_ID, "headSha": HEAD_SHA},
            work_item_id=requested.work_item.id,
            requirement_id=requested.requirement.id,
            repository_id=context.repository_id,
            request_fingerprint="sha256:real-create-delivery",
            attempts=1,
            next_reconcile_at=None,
            state=EffectState.SUCCEEDED.value,
            requirement_callback_state="ACKED",
            completed_at=NOW,
            now=NOW,
        )
        integration.insert_merge_request_binding(
            id=REAL_MR_BINDING_ID,
            kind="INTEGRATION",
            work_item_id=requested.work_item.id,
            requirement_id=requested.requirement.id,
            workspace_id=context.workspace_id,
            repository_id=context.repository_id,
            branch_binding_id=REAL_BRANCH_BINDING_ID,
            external_project_id="101",
            merge_request_iid=17,
            source_branch=context.task_branch,
            target_branch="dev",
            create_effect_id=REAL_CREATE_EFFECT_ID,
            head_sha=HEAD_SHA,
            creation_origin="PLATFORM_CREATED",
            now=NOW,
        )
        integration.append_merge_request_observation(
            id=REAL_OPEN_OBSERVATION_ID,
            binding_id=REAL_MR_BINDING_ID,
            head_sha=HEAD_SHA,
            state="OPEN",
            merge_commit_sha=None,
            external_merge_user_id=None,
            merged_at=None,
            observation_digest="sha256:real-merge-open",
            observed_at=NOW,
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
            integration_merge_request_binding_id=(envelope.integration_merge_request_binding_id),
            now=NOW,
        )
    gitlab = FakeMergeGitLab(
        source_engine,
        work_item_id=requested.work_item.id,
        binding_id=REAL_MR_BINDING_ID,
        source_branch=context.task_branch,
    )
    requirement_delivery: Any = delivery
    if callback_ack_loss:
        requirement_delivery = CommitThenRaiseMergedRequirement(delivery)
    if blocked_callback_ack_loss:
        requirement_delivery = CommitThenRaiseBlockedRequirement(delivery)
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
        requirement_delivery=requirement_delivery,
        gitlab_merge_requests=gitlab,
    )
    return RealMergeCase(
        work_item_id=requested.work_item.id,
        message_id=envelope.message_id,
        delivery=delivery,
        dependencies=dependencies,
        gitlab=gitlab,
    )


def test_merge_saga_uses_current_exact_sha_and_preserves_source_branch(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, eligibility, gitlab = _dependencies(engine)

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert gitlab.merge_calls == [(17, HEAD_SHA)]
    assert "dev_branch" not in gitlab.calls
    assert result.effect is not None
    assert result.effect.operation is EffectOperation.MERGE_INTEGRATION_MR
    assert result.effect.state is EffectState.SUCCEEDED
    assert result.effect.subject_key == f"mr:{MR_BINDING_ID}:{HEAD_SHA}"
    assert result.binding is not None
    assert result.binding.id == MR_BINDING_ID
    assert result.observation is not None
    assert result.observation.state.value == "MERGED"
    assert result.observation.merge_commit_sha == MERGE_COMMIT_SHA
    assert gitlab.source_head == HEAD_SHA
    assert requirement.merged[0].binding_id == MR_BINDING_ID
    assert eligibility.seen[-1].actor_id == "employee-1"
    assert eligibility.seen[-1].required_capabilities == ("merge_request.merge",)


@pytest.mark.parametrize(
    ("admission_failure", "reason_code"),
    [
        ("assignment", "OWNER_MISMATCH"),
        ("eligibility", "MERGE_ACTOR_INELIGIBLE"),
    ],
)
def test_merge_admission_blocks_inconsistent_assignment_or_merge_actor_ineligibility(
    isolated_source_control_database: Any,
    admission_failure: str,
    reason_code: str,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    context = _delivery_context()
    if admission_failure == "assignment":
        context = context.model_copy(update={"human_owner_id": "employee-2"})
    requirement = MergeRequirementDelivery(context)
    dependencies, requirement, _eligibility, gitlab = _dependencies(
        engine,
        requirement=requirement,
    )
    if admission_failure == "eligibility":
        dependencies = replace(dependencies, eligibility=MergeEligibility(False))

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == reason_code
    assert gitlab.calls == []
    assert requirement.blocked[0].reason_code == reason_code


def test_merge_admission_revalidates_the_request_actor_not_the_work_item_owner(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    context = _delivery_context().model_copy(update={"request_actor_id": "merge-operator-1"})
    requirement = MergeRequirementDelivery(context)
    dependencies, _requirement, eligibility, gitlab = _dependencies(
        engine,
        requirement=requirement,
    )
    with engine.begin() as db:
        db.execute(
            text(
                "UPDATE source_control.delivery_request_inbox "
                "SET actor_id='merge-operator-1' WHERE message_id=:message_id"
            ),
            {"message_id": MERGE_MESSAGE_ID},
        )

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert gitlab.merge_calls == [(17, HEAD_SHA)]
    assert eligibility.seen[-1].actor_id == "merge-operator-1"
    assert eligibility.seen[-1].required_capabilities == ("merge_request.merge",)


@pytest.mark.parametrize(
    "dependency_name",
    (
        "delivery_repository_factory",
        "requirement_delivery",
        "requirement",
        "eligibility",
        "gitlab_merge_requests",
        "policy",
    ),
)
def test_missing_merge_dependency_fails_closed_before_inbox_claim(
    isolated_source_control_database: Any,
    dependency_name: Literal[
        "delivery_repository_factory",
        "requirement_delivery",
        "requirement",
        "eligibility",
        "gitlab_merge_requests",
        "policy",
    ],
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, _requirement, _eligibility, gitlab = _dependencies(engine)
    if dependency_name == "delivery_repository_factory":
        dependencies = replace(dependencies, delivery_repository_factory=None)
    elif dependency_name == "requirement_delivery":
        dependencies = replace(dependencies, requirement_delivery=None)
    elif dependency_name == "requirement":
        dependencies = replace(dependencies, requirement=None)
    elif dependency_name == "eligibility":
        dependencies = replace(dependencies, eligibility=None)
    elif dependency_name == "gitlab_merge_requests":
        dependencies = replace(dependencies, gitlab_merge_requests=None)
    else:
        dependencies = replace(dependencies, policy=None)

    with pytest.raises(SourceControlDependencyUnavailable):
        process_integration_merge_request(
            message_id=MERGE_MESSAGE_ID,
            dependencies=dependencies,
        )

    with engine.connect() as db:
        inbox = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(MERGE_MESSAGE_ID)
    assert inbox is not None
    assert inbox["state"] == "RECEIVED"
    assert inbox["attempts"] == 0
    assert gitlab.calls == []


@pytest.mark.parametrize(
    ("processor", "message_id"),
    [
        (process_integration_mr_request, MERGE_MESSAGE_ID),
        (process_integration_merge_request, CREATE_MESSAGE_ID),
    ],
    ids=("create-processor-on-merge", "merge-processor-on-create"),
)
def test_wrong_delivery_topic_never_acquires_an_inbox_lease(
    isolated_source_control_database: Any,
    processor: Any,
    message_id: str,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, _requirement, _eligibility, gitlab = _dependencies(engine)
    with engine.connect() as db:
        before = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(message_id)

    with pytest.raises(SourceControlDependencyUnavailable):
        processor(message_id=message_id, dependencies=dependencies)

    with engine.connect() as db:
        inbox = SqlAlchemySourceControlIntegrationRepository(db).delivery_request(message_id)
    assert before is not None
    assert inbox is not None
    assert inbox["state"] == before["state"] == "RECEIVED"
    assert inbox["attempts"] == before["attempts"] == 0
    assert inbox["available_at"] == before["available_at"]
    assert gitlab.calls == []


def test_new_merge_effect_freezes_current_double_read_head_not_historical_head(
    isolated_source_control_database: Any,
) -> None:
    current_head = "e" * 40
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine, historical_head=HEAD_SHA)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.source_head = current_head
    gitlab.expected_effect_head = current_head

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.subject_key == f"mr:{MR_BINDING_ID}:{current_head}"
    assert result.effect.payload.model_dump(by_alias=True) == {
        "bindingId": MR_BINDING_ID,
        "requestedHeadSha": current_head,
    }
    assert gitlab.merge_calls == [(17, current_head)]
    assert result.observation is not None
    assert result.observation.head_sha == current_head
    assert requirement.merged[0].binding_id == MR_BINDING_ID


def test_existing_planned_effect_head_change_blocks_without_put(
    isolated_source_control_database: Any,
) -> None:
    current_head = "e" * 40
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine, historical_head=HEAD_SHA)
    _seed_planned_merge_effect(engine, frozen_head=HEAD_SHA)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.source_head = current_head

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.effect.last_error_code == "HEAD_SHA_CHANGED"
    assert result.blocked_reason == "HEAD_SHA_CHANGED"
    assert gitlab.merge_calls == []
    assert requirement.blocked[0].binding_id == MR_BINDING_ID


@pytest.mark.parametrize(
    ("effect_kind", "reason_code"),
    [
        ("none", "EXTERNAL_MERGE_DRIFT"),
        ("exact", "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION"),
        ("stale", "HEAD_SHA_CHANGED"),
    ],
)
def test_pre_put_merged_source_missing_preserves_fact_after_effect_classification(
    isolated_source_control_database: Any,
    effect_kind: str,
    reason_code: str,
) -> None:
    current_head = "e" * 40 if effect_kind == "stale" else HEAD_SHA
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    if effect_kind != "none":
        _seed_planned_merge_effect(engine, frozen_head=HEAD_SHA)
    audit = FakeAudit()
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine, audit=audit)
    gitlab.source_head = current_head
    gitlab._merged = True
    gitlab.source_after_merge_error = GitLabBranchNotFound("source branch removed")

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.blocked_reason == reason_code
    assert result.observation is not None
    assert result.observation.state.value == "MERGED"
    assert result.observation.head_sha == current_head
    assert result.observation.merge_commit_sha == MERGE_COMMIT_SHA
    assert gitlab.merge_calls == []
    if effect_kind == "none":
        assert result.effect is None
        assert len(requirement.external_drift) == 1
        assert [
            (entry.action, entry.target_id, entry.correlation_id) for entry in audit.entries
        ] == [
            (
                "source_control.integration_merge.external_drift",
                MR_BINDING_ID,
                requirement.external_drift[0].correlation_id,
            )
        ]
        assert requirement.external_drift[0].correlation_id == (
            f"source-control:inbox:{MERGE_MESSAGE_ID}"
        )
    else:
        assert result.effect is not None
        assert result.effect.state is EffectState.BLOCKED
        assert requirement.external_drift == []
        assert requirement.blocked[0].reason_code == reason_code


@pytest.mark.parametrize("collision_kind", ["different-fingerprint", "duplicate"])
def test_merged_provider_fact_waits_for_effect_tri_state_before_drift_callback(
    isolated_source_control_database: Any,
    collision_kind: str,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    if collision_kind == "different-fingerprint":
        _seed_planned_merge_effect(
            engine,
            frozen_head=HEAD_SHA,
            request_fingerprint="sha256:different-merge-request",
        )
    else:
        _seed_planned_merge_effect(engine, frozen_head=HEAD_SHA)
        _seed_planned_merge_effect(
            engine,
            frozen_head="e" * 40,
            effect_id="83000000-0000-0000-0000-000000000802",
        )
    audit = FakeAudit()
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine, audit=audit)
    gitlab._merged = True

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == "MR_CONFLICT"
    assert gitlab.merge_calls == []
    assert requirement.external_drift == []
    assert requirement.blocked[0].reason_code == "MR_CONFLICT"
    assert result.observation is not None
    assert result.observation.state.value == "MERGED"
    assert result.observation.merge_commit_sha == MERGE_COMMIT_SHA
    assert [entry.action for entry in audit.entries] == [
        "source_control.integration_merge.provider_fact_conflict",
        "source_control.integration_delivery.blocked",
    ]
    assert all(
        entry.correlation_id == requirement.blocked[0].correlation_id for entry in audit.entries
    )
    assert requirement.blocked[0].correlation_id == f"source-control:inbox:{MERGE_MESSAGE_ID}"
    with engine.connect() as db:
        observation_count = db.execute(
            text(
                "SELECT count(*) FROM source_control.merge_request_observation "
                "WHERE binding_id=CAST(:binding_id AS uuid)"
            ),
            {"binding_id": MR_BINDING_ID},
        ).scalar_one()
    assert observation_count == 2


def test_proven_merge_conflict_callback_ack_loss_replays_without_duplicate_observation(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    _seed_planned_merge_effect(
        engine,
        frozen_head=HEAD_SHA,
        request_fingerprint="sha256:different-merge-request",
    )
    requirement = CommitThenRaiseBlockedMergeRequirement(_delivery_context())
    audit = FakeAudit()
    clock = MutableClock(NOW)
    dependencies, _returned_requirement, _eligibility, gitlab = _dependencies(
        engine,
        requirement=requirement,
        audit=audit,
        clock=clock,
    )
    gitlab._merged = True

    first = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )
    provider_calls = tuple(gitlab.calls)
    clock.current = NOW + timedelta(minutes=3)
    second = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    with engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        inbox = repository.delivery_request(MERGE_MESSAGE_ID)
        observations = (
            db.execute(
                text(
                    "SELECT state FROM source_control.merge_request_observation "
                    "WHERE binding_id=CAST(:binding_id AS uuid) ORDER BY observed_at, id"
                ),
                {"binding_id": MR_BINDING_ID},
            )
            .scalars()
            .all()
        )
    assert first.observation is not None
    assert first.observation.state.value == "MERGED"
    assert second == first
    assert observations == ["OPEN", "MERGED"]
    assert inbox is not None
    assert inbox["state"] == "PROCESSED"
    assert requirement.blocked_attempts == 2
    assert len(requirement.blocked) == 1
    assert tuple(gitlab.calls) == provider_calls
    assert gitlab.merge_calls == []
    assert [entry.action for entry in audit.entries] == [
        "source_control.integration_merge.provider_fact_conflict",
        "source_control.integration_delivery.blocked",
    ]
    assert all(
        entry.correlation_id == requirement.blocked[0].correlation_id for entry in audit.entries
    )


@pytest.mark.parametrize(
    "effect_values",
    [
        {"request_fingerprint": "sha256:other-merge-request"},
        {"requirement_id": "40000000-0000-0000-0000-000000000899"},
        {"effect_binding_id": "81000000-0000-0000-0000-000000000899"},
    ],
    ids=("fingerprint", "requirement", "binding"),
)
def test_existing_merge_effect_identity_collision_blocks_without_put(
    isolated_source_control_database: Any,
    effect_values: _MergeEffectOverrides,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    _seed_planned_merge_effect(
        engine,
        frozen_head=HEAD_SHA,
        **effect_values,
    )
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == "MR_CONFLICT"
    assert gitlab.merge_calls == []
    assert requirement.blocked[0].reason_code == "MR_CONFLICT"


@pytest.mark.parametrize(
    ("snapshot", "reason_code"),
    [
        (
            _opened_snapshot().model_copy(update={"head_pipeline_status": "failed"}),
            "MR_CHECKS_BLOCKED",
        ),
        (
            _opened_snapshot().model_copy(update={"blocking_discussions_resolved": False}),
            "MR_CHECKS_BLOCKED",
        ),
        (
            _opened_snapshot().model_copy(update={"has_conflicts": True}),
            "MERGE_CONFLICT",
        ),
        (
            _opened_snapshot().model_copy(update={"state": "closed"}),
            "MR_CLOSED",
        ),
    ],
    ids=("pipeline", "discussions", "conflict", "closed"),
)
def test_deterministic_preflight_blocks_without_planning_or_put(
    isolated_source_control_database: Any,
    snapshot: GitLabMergeRequestSnapshot,
    reason_code: str,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.preflight_snapshot = snapshot

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == reason_code
    assert gitlab.merge_calls == []
    assert requirement.blocked[0].binding_id == MR_BINDING_ID
    assert requirement.blocked[0].reason_code == reason_code


def test_deep_project_profile_dev_missing_is_target_not_source_failure(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.profile_error = GitLabBranchNotFound("protected dev branch missing")

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == "TARGET_BRANCH_NOT_FOUND"
    assert gitlab.calls == ["profile"]
    assert gitlab.merge_calls == []
    assert requirement.blocked[0].reason_code == "TARGET_BRANCH_NOT_FOUND"


@pytest.mark.parametrize(
    ("platform_default_branch", "provider_default_branch"),
    [
        ("master", "master"),
        ("master", "main"),
        ("main", "master"),
    ],
    ids=("both-non-main", "platform-non-main", "provider-non-main"),
)
def test_non_main_platform_or_provider_default_is_unsupported(
    isolated_source_control_database: Any,
    platform_default_branch: str,
    provider_default_branch: str,
) -> None:
    engine = isolated_source_control_database.runtime
    if platform_default_branch != "main":
        with isolated_source_control_database.owner.begin() as db:
            db.execute(
                text(
                    "ALTER TABLE source_control.workspace_repository "
                    "DROP CONSTRAINT ck_source_control_default_branch"
                )
            )
    _seed_merge_request(engine, default_branch=platform_default_branch)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.provider_default_branch = provider_default_branch

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    with engine.connect() as db:
        effect_count = db.execute(
            text(
                "SELECT count(*) FROM source_control.source_control_effect "
                "WHERE operation='MERGE_INTEGRATION_MR'"
            )
        ).scalar_one()
    assert result.effect is None
    assert result.blocked_reason == "PROJECT_PROFILE_UNSUPPORTED"
    assert gitlab.calls == ["profile"]
    assert gitlab.merge_calls == []
    assert requirement.blocked[0].reason_code == "PROJECT_PROFILE_UNSUPPORTED"
    assert effect_count == 0


@pytest.mark.parametrize("missing_field", ["merge_commit_sha", "merged_at"])
def test_incomplete_merged_preflight_fact_releases_inbox_without_observation(
    isolated_source_control_database: Any,
    missing_field: str,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.preflight_snapshot = _merged_snapshot().model_copy(update={missing_field: None})

    with pytest.raises(RequirementCallbackUnavailable):
        process_integration_merge_request(
            message_id=MERGE_MESSAGE_ID,
            dependencies=dependencies,
        )

    with engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        inbox = repository.delivery_request(MERGE_MESSAGE_ID)
        effect_count = db.execute(
            text(
                "SELECT count(*) FROM source_control.source_control_effect "
                "WHERE operation='MERGE_INTEGRATION_MR'"
            )
        ).scalar_one()
        observations = (
            db.execute(
                text(
                    "SELECT state FROM source_control.merge_request_observation "
                    "WHERE binding_id=CAST(:binding_id AS uuid) ORDER BY observed_at, id"
                ),
                {"binding_id": MR_BINDING_ID},
            )
            .scalars()
            .all()
        )
    assert inbox is not None
    assert inbox["state"] == "FAILED"
    assert inbox["last_error_code"] == "PROVIDER_UNAVAILABLE"
    assert effect_count == 0
    assert observations == ["OPEN"]
    assert gitlab.calls == ["profile", "get_mr"]
    assert gitlab.merge_calls == []
    assert requirement.blocked == []
    assert requirement.external_drift == []


@pytest.mark.parametrize(
    "snapshot",
    [
        _opened_snapshot().model_copy(update={"project_id": "202"}),
        _opened_snapshot().model_copy(update={"iid": 18}),
        _opened_snapshot().model_copy(update={"source_branch": "feat/other"}),
        _opened_snapshot().model_copy(update={"target_branch": "main"}),
    ],
    ids=("project", "iid", "source", "target"),
)
def test_merge_request_coordinate_mismatch_is_conflict_without_put(
    isolated_source_control_database: Any,
    snapshot: GitLabMergeRequestSnapshot,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.preflight_snapshot = snapshot

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.blocked_reason == "MR_CONFLICT"
    assert gitlab.merge_calls == []
    assert requirement.blocked[0].reason_code == "MR_CONFLICT"


@pytest.mark.parametrize(
    ("merge_error", "reason_code"),
    [
        (GitLabMergeRequestHeadChanged("head changed"), "HEAD_SHA_CHANGED"),
        (GitLabMergeRequestBlocked("merge rejected"), "MERGE_CONFLICT"),
        (GitLabAccessDenied("merge denied"), "REPOSITORY_NOT_AUTHORIZED"),
        (GitLabProjectNotFound("project missing"), "REPOSITORY_NOT_AUTHORIZED"),
        (GitLabMergeRequestNotFound("merge request missing"), "MR_CLOSED"),
    ],
    ids=("409", "405-or-422", "403", "project-404", "mr-404"),
)
def test_stable_put_rejection_blocks_the_frozen_effect(
    isolated_source_control_database: Any,
    merge_error: Exception,
    reason_code: str,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.merge_error = merge_error

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    with engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        effect = repository.effect_by_operation_subject(
            EffectOperation.MERGE_INTEGRATION_MR.value,
            f"mr:{MR_BINDING_ID}:{HEAD_SHA}",
        )
        inbox = repository.delivery_request(MERGE_MESSAGE_ID)

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.effect.last_error_code == reason_code
    assert result.blocked_reason == reason_code
    assert gitlab.merge_calls == [(17, HEAD_SHA)]
    assert requirement.blocked[0].binding_id == MR_BINDING_ID
    assert effect is not None
    assert effect["state"] == EffectState.BLOCKED.value
    assert effect["last_error_code"] == reason_code
    assert inbox is not None
    assert inbox["state"] == "PROCESSED"


@pytest.mark.parametrize(
    "merge_error",
    [
        GitLabResultUnknown("write acknowledgement lost"),
        GitLabProviderUnavailable("provider unavailable"),
    ],
    ids=("unknown", "5xx"),
)
def test_uncertain_put_marks_effect_unknown_without_guessing_success(
    isolated_source_control_database: Any,
    merge_error: Exception,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.merge_error = merge_error

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.UNKNOWN
    assert result.effect.last_error_code == "EXTERNAL_RESULT_UNKNOWN"
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert requirement.merged == []
    assert requirement.pending[0].binding_id == MR_BINDING_ID


def test_post_put_readback_unavailable_is_unknown(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.get_after_merge_error = GitLabProviderUnavailable("malformed readback")

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.UNKNOWN
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert requirement.pending[0].binding_id == MR_BINDING_ID
    assert requirement.merged == []


def test_merged_readback_with_missing_source_preserves_observation_and_blocks(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.source_after_merge_error = GitLabBranchNotFound("source missing")

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.BLOCKED
    assert result.effect.last_error_code == "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION"
    assert result.observation is not None
    assert result.observation.state.value == "MERGED"
    assert result.observation.merge_commit_sha == MERGE_COMMIT_SHA
    assert result.blocked_reason == "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION"
    assert requirement.merged == []
    assert requirement.blocked[0].binding_id == MR_BINDING_ID


def test_merged_provider_fact_without_valid_merge_effect_is_external_drift(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)
    gitlab.preflight_snapshot = _merged_snapshot()

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is None
    assert result.binding is not None
    assert result.observation is not None
    assert result.observation.state.value == "MERGED"
    assert result.blocked_reason == "EXTERNAL_MERGE_DRIFT"
    assert gitlab.merge_calls == []
    assert requirement.merged == []
    assert requirement.external_drift[0].binding_id == MR_BINDING_ID


def test_terminal_success_replay_does_not_repeat_provider_or_callback(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)

    first = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )
    provider_calls = tuple(gitlab.calls)
    second = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert second == first
    assert tuple(gitlab.calls) == provider_calls
    assert requirement.merged_attempts == 1
    assert len(requirement.merged) == 1


def test_merged_callback_failure_replays_without_repeating_put(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    requirement = MergeRequirementDelivery(_delivery_context())
    requirement.fail_merged = True
    dependencies, requirement, _eligibility, gitlab = _dependencies(
        engine,
        requirement=requirement,
    )

    first = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )
    provider_calls = tuple(gitlab.calls)
    requirement.fail_merged = False
    second = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.state is EffectState.SUCCEEDED
    assert first.effect.callback_state.value == "FAILED"
    assert second.effect is not None
    assert second.effect.callback_state.value == "ACKED"
    assert tuple(gitlab.calls) == provider_calls
    assert requirement.merged_attempts == 2
    assert len(requirement.merged) == 1


def test_existing_in_flight_effect_is_handed_to_reconciliation_without_second_put(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    _seed_planned_merge_effect(
        engine,
        frozen_head=HEAD_SHA,
        state=EffectState.IN_FLIGHT,
    )
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.IN_FLIGHT
    assert result.blocked_reason == "RECONCILIATION_PENDING"
    assert gitlab.calls == []
    assert requirement.pending[0].binding_id == MR_BINDING_ID


def test_committed_merge_facts_survive_source_control_commit_ack_loss(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    factory = CommitThenRaiseCompleteInboxRepositoryFactory()
    dependencies, requirement, _eligibility, _gitlab = _dependencies(
        engine,
        delivery_repository_factory=factory,
    )

    result = process_integration_merge_request(
        message_id=MERGE_MESSAGE_ID,
        dependencies=dependencies,
    )

    assert result.effect is not None
    assert result.effect.state is EffectState.SUCCEEDED
    assert result.observation is not None
    assert result.observation.state.value == "MERGED"
    assert len(requirement.merged) == 1
    with engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        inbox = repository.delivery_request(MERGE_MESSAGE_ID)
        observation_count = db.execute(
            text(
                "SELECT count(*) FROM source_control.merge_request_observation "
                "WHERE binding_id=CAST(:binding_id AS uuid)"
            ),
            {"binding_id": MR_BINDING_ID},
        ).scalar_one()
    assert inbox is not None
    assert inbox["state"] == "PROCESSED"
    assert observation_count == 2


def test_stale_worker_cannot_commit_merge_facts_after_leases_are_stolen(
    isolated_source_control_database: Any,
) -> None:
    engine = isolated_source_control_database.runtime
    _seed_merge_request(engine)
    dependencies, requirement, _eligibility, gitlab = _dependencies(engine)

    def steal_leases() -> None:
        with engine.begin() as db:
            repository = SqlAlchemySourceControlIntegrationRepository(db)
            effect = repository.effect_by_operation_subject(
                EffectOperation.MERGE_INTEGRATION_MR.value,
                f"mr:{MR_BINDING_ID}:{HEAD_SHA}",
            )
            assert effect is not None
            stolen_effect = repository.transition_effect(
                str(effect["id"]),
                expected_state=EffectState.IN_FLIGHT.value,
                expected_attempts=effect["attempts"],
                values={
                    "state": EffectState.RECONCILIATION.value,
                    "attempts": effect["attempts"] + 1,
                    "next_reconcile_at": NOW,
                    "updated_at": NOW,
                },
            )
            stolen_inbox = repository.claim_delivery_request(
                MERGE_MESSAGE_ID,
                expected_topic="requirement.integration-merge.requested",
                now=NOW.replace(hour=12),
                lease_until=NOW.replace(hour=13),
            )
        assert stolen_effect is not None
        assert stolen_inbox is not None

    gitlab.before_readback = steal_leases

    with pytest.raises(RequirementCallbackUnavailable):
        process_integration_merge_request(
            message_id=MERGE_MESSAGE_ID,
            dependencies=dependencies,
        )

    with engine.connect() as db:
        repository = SqlAlchemySourceControlIntegrationRepository(db)
        effect = repository.effect_by_operation_subject(
            EffectOperation.MERGE_INTEGRATION_MR.value,
            f"mr:{MR_BINDING_ID}:{HEAD_SHA}",
        )
        inbox = repository.delivery_request(MERGE_MESSAGE_ID)
        observation_count = db.execute(
            text("SELECT count(*) FROM source_control.merge_request_observation")
        ).scalar_one()
    assert effect is not None
    assert effect["state"] == "RECONCILIATION"
    assert effect["attempts"] == 2
    assert inbox is not None
    assert inbox["state"] == "PROCESSING"
    assert inbox["attempts"] == 2
    assert observation_count == 1
    assert requirement.merged == []


def test_real_requirement_merge_callback_commit_ack_loss_is_integrated_once(
    isolated_source_control_database: Any,
) -> None:
    source_engine = isolated_source_control_database.runtime
    with _temporary_requirement_role_engine(
        isolated_source_control_database.owner
    ) as requirement_engine:
        case = _seed_real_requirement_merge_case(
            source_engine,
            requirement_engine,
            key_suffix="task8-merge-callback-ack-loss",
            callback_ack_loss=True,
        )
        callback = case.dependencies.requirement_delivery
        assert isinstance(callback, CommitThenRaiseMergedRequirement)

        first = process_integration_merge_request(
            message_id=case.message_id,
            dependencies=case.dependencies,
        )
        after_first = case.delivery.delivery_context(case.work_item_id)
        provider_calls = tuple(case.gitlab.calls)
        second = process_integration_merge_request(
            message_id=case.message_id,
            dependencies=case.dependencies,
        )
        after_second = case.delivery.delivery_context(case.work_item_id)

    assert first.effect is not None
    assert first.effect.state is EffectState.SUCCEEDED
    assert first.effect.callback_state.value == "FAILED"
    assert second.effect is not None
    assert second.effect.callback_state.value == "ACKED"
    assert after_first.integration_delivery_state == "INTEGRATED"
    assert after_second == after_first
    assert callback.merged_attempts == 2
    assert tuple(case.gitlab.calls) == provider_calls
    assert case.gitlab.merge_calls == [(17, HEAD_SHA)]


def test_real_requirement_preflight_callback_ack_loss_replays_before_admission(
    isolated_source_control_database: Any,
) -> None:
    source_engine = isolated_source_control_database.runtime
    with _temporary_requirement_role_engine(
        isolated_source_control_database.owner
    ) as requirement_engine:
        case = _seed_real_requirement_merge_case(
            source_engine,
            requirement_engine,
            key_suffix="task8-preflight-callback-ack-loss",
            blocked_callback_ack_loss=True,
        )
        callback = case.dependencies.requirement_delivery
        assert isinstance(callback, CommitThenRaiseBlockedRequirement)
        clock = MutableClock(NOW)
        case = replace(case, dependencies=replace(case.dependencies, clock=clock))
        case.gitlab.preflight_snapshot = _opened_snapshot().model_copy(
            update={
                "project_id": "202",
                "source_branch": case.gitlab.expected_source_branch,
            }
        )

        first = process_integration_merge_request(
            message_id=case.message_id,
            dependencies=case.dependencies,
        )
        after_first = case.delivery.delivery_context(case.work_item_id)
        provider_calls = tuple(case.gitlab.calls)
        clock.current = NOW + timedelta(minutes=3)
        second = process_integration_merge_request(
            message_id=case.message_id,
            dependencies=case.dependencies,
        )
        after_second = case.delivery.delivery_context(case.work_item_id)

    assert first.effect is None
    assert first.blocked_reason == "MR_CONFLICT"
    assert second == first
    assert after_first.integration_delivery_state == "BLOCKED"
    assert after_second == after_first
    assert callback.blocked_attempts == 2
    assert tuple(case.gitlab.calls) == provider_calls
    assert case.gitlab.merge_calls == []


def test_real_requirement_second_worker_cannot_cross_active_inbox_fence(
    isolated_source_control_database: Any,
) -> None:
    source_engine = isolated_source_control_database.runtime
    with _temporary_requirement_role_engine(
        isolated_source_control_database.owner
    ) as requirement_engine:
        case = _seed_real_requirement_merge_case(
            source_engine,
            requirement_engine,
            key_suffix="task8-double-worker-fence",
        )
        merge_started = Event()
        permit_merge = Event()

        def pause_in_flight_merge() -> None:
            merge_started.set()
            assert permit_merge.wait(timeout=10)

        case.gitlab.before_merge = pause_in_flight_merge
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_worker = executor.submit(
                process_integration_merge_request,
                message_id=case.message_id,
                dependencies=case.dependencies,
            )
            assert merge_started.wait(timeout=10)
            try:
                with pytest.raises(RequirementCallbackUnavailable):
                    process_integration_merge_request(
                        message_id=case.message_id,
                        dependencies=case.dependencies,
                    )
                during_merge = case.delivery.delivery_context(case.work_item_id)
            finally:
                permit_merge.set()
            first = first_worker.result(timeout=10)
        after_merge = case.delivery.delivery_context(case.work_item_id)

    assert during_merge.integration_delivery_state == "MERGE_PENDING"
    assert first.effect is not None
    assert first.effect.state is EffectState.SUCCEEDED
    assert first.effect.callback_state.value == "ACKED"
    assert after_merge.integration_delivery_state == "INTEGRATED"
    assert case.gitlab.merge_calls == [(17, HEAD_SHA)]


@pytest.mark.parametrize(
    ("provider_outcome", "reason_code", "expected_puts"),
    [
        ("external-drift", "EXTERNAL_MERGE_DRIFT", 0),
        (
            "source-missing",
            "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION",
            1,
        ),
    ],
)
def test_real_requirement_blocks_on_proven_merged_drift_or_missing_source(
    isolated_source_control_database: Any,
    provider_outcome: str,
    reason_code: str,
    expected_puts: int,
) -> None:
    source_engine = isolated_source_control_database.runtime
    with _temporary_requirement_role_engine(
        isolated_source_control_database.owner
    ) as requirement_engine:
        case = _seed_real_requirement_merge_case(
            source_engine,
            requirement_engine,
            key_suffix=f"task8-{provider_outcome}",
        )
        before = case.delivery.delivery_context(case.work_item_id)
        if provider_outcome == "external-drift":
            case.gitlab._merged = True
        else:
            case.gitlab.source_after_merge_error = GitLabBranchNotFound(
                "source branch no longer exists"
            )

        result = process_integration_merge_request(
            message_id=case.message_id,
            dependencies=case.dependencies,
        )
        after = case.delivery.delivery_context(case.work_item_id)

    assert result.blocked_reason == reason_code
    assert result.observation is not None
    assert result.observation.state.value == "MERGED"
    assert result.observation.merge_commit_sha == MERGE_COMMIT_SHA
    assert len(case.gitlab.merge_calls) == expected_puts
    assert after.work_item_revision == before.work_item_revision + 1
    assert after.integration_delivery_state == "BLOCKED"
    if provider_outcome == "external-drift":
        assert result.effect is None
    else:
        assert result.effect is not None
        assert result.effect.state is EffectState.BLOCKED
        assert result.effect.callback_state.value == "ACKED"
