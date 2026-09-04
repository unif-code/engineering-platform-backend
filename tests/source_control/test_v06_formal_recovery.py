from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event
from typing import Literal

import pytest
from sqlalchemy import text

from control_plane.app.modules.source_control import SourceControlDependencies
from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlFormalRepository,
)
from control_plane.app.modules.source_control.application.formal import (
    accept_formal_delivery_request,
    process_formal_delivery_request,
    reconcile_due_formal_effects,
    reconcile_formal_delivery_effect,
    replay_pending_formal_callbacks,
)
from control_plane.app.modules.source_control.domain import (
    CreateFormalMergeRequestEffectPayload,
    EffectOperation,
    EffectState,
    FormalDeliveryConflict,
    FormalDeliveryRequestEnvelope,
    FormalDeliveryRequestKind,
    RequirementCallbackState,
    SourceControlDependencyUnavailable,
)
from control_plane.app.modules.source_control.ports import (
    BranchSnapshot,
    FormalDeliveryBlockedCallback,
    FormalMrReadyCallback,
    GitLabAccessDenied,
    GitLabBranchNotFound,
    GitLabMergeRequestBlocked,
    GitLabMergeRequestHeadChanged,
    GitLabMergeRequestLocator,
    GitLabMergeRequestNotFound,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
    GitLabProjectPolicyUnsupported,
    GitLabResultUnknown,
)
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_v06_formal_application import (
    HEAD_SHA,
    NOW,
    FakeEligibility,
    FakeFormalGitLab,
    FakeRequirementFormalDelivery,
    _admission,
    _dependencies,
    _envelope,
    _seed,
)


class FutureClock:
    def now(self) -> datetime:
        return NOW + timedelta(minutes=3)


class LaterClock:
    def now(self) -> datetime:
        return NOW + timedelta(minutes=6)


def test_formal_inbox_claim_excludes_consumers_until_the_lease_expires(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    dependencies = _dependencies(
        isolated_source_control_database,
        requirement,
        FakeFormalGitLab(),
    )
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000611")
    with isolated_source_control_database.runtime.begin() as db:
        repository = SqlAlchemySourceControlFormalRepository(db)
        accept_formal_delivery_request(repository, envelope, dependencies=dependencies)
        first = repository.claim_formal_request(
            envelope.message_id,
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
        )
    with isolated_source_control_database.runtime.begin() as db:
        second = SqlAlchemySourceControlFormalRepository(db).claim_formal_request(
            envelope.message_id,
            now=NOW + timedelta(seconds=1),
            lease_until=NOW + timedelta(minutes=3),
        )
    with isolated_source_control_database.runtime.begin() as db:
        reclaimed = SqlAlchemySourceControlFormalRepository(db).claim_formal_request(
            envelope.message_id,
            now=NOW + timedelta(minutes=2),
            lease_until=NOW + timedelta(minutes=4),
        )

    assert first is not None
    assert first["attempts"] == 1
    assert second is None
    assert reclaimed is not None
    assert reclaimed["attempts"] == 2


class BlockingCreateGitLab(FakeFormalGitLab):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def create_formal_merge_request(
        self,
        repository: object,
        *,
        source_branch: str,
        expected_head_sha: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestLocator:
        self.entered.set()
        assert self.release.wait(timeout=5)
        return super().create_formal_merge_request(
            repository,
            source_branch=source_branch,
            expected_head_sha=expected_head_sha,
            title=title,
            description=description,
        )


def test_parallel_formal_consumers_execute_the_provider_write_once(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = BlockingCreateGitLab()
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000617")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            envelope,
            dependencies=dependencies,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            process_formal_delivery_request,
            message_id=envelope.message_id,
            dependencies=dependencies,
        )
        assert gitlab.entered.wait(timeout=5)
        with pytest.raises(FormalDeliveryConflict, match="request is unavailable"):
            process_formal_delivery_request(
                message_id=envelope.message_id,
                dependencies=dependencies,
            )
        gitlab.release.set()
        result = first.result(timeout=5)

    assert result.effect is not None
    assert result.effect.state is EffectState.SUCCEEDED
    assert gitlab.created == 1


def test_inconclusive_formal_reconciliation_returns_effect_to_unknown(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    gitlab.create_error = GitLabResultUnknown("timeout after write")
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000612")
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
    gitlab.create_error = None
    dependencies = replace(dependencies, clock=FutureClock())

    with pytest.raises(
        SourceControlDependencyUnavailable,
        match="reconciliation remains unknown",
    ):
        reconcile_formal_delivery_effect(
            effect_id=unknown.effect.id,
            dependencies=dependencies,
        )

    with isolated_source_control_database.owner.connect() as db:
        state, next_reconcile_at = db.execute(
            text(
                "SELECT state, next_reconcile_at "
                "FROM source_control.source_control_effect WHERE id=:effect_id"
            ),
            {"effect_id": unknown.effect.id},
        ).one()
    assert state == EffectState.UNKNOWN.value
    assert next_reconcile_at > NOW


def test_reconciliation_converges_to_blocked_when_the_exact_head_has_drifted(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = FakeFormalGitLab()
    gitlab.create_error = GitLabResultUnknown("timeout after write")
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000628")
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
    gitlab.create_error = None
    gitlab.branch_head = "e" * 40
    dependencies = replace(dependencies, clock=FutureClock())

    blocked = reconcile_formal_delivery_effect(
        effect_id=unknown.effect.id,
        dependencies=dependencies,
    )

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state is RequirementCallbackState.ACKED
    assert blocked.blocked_reason == "HEAD_SHA_CHANGED"
    assert len(requirement.blocked) == 1


class MergeAckLossGitLab(FakeFormalGitLab):
    def __init__(self) -> None:
        super().__init__()
        self.branch_deleted = False

    def get_branch(self, repository: object, name: str) -> BranchSnapshot:
        if self.branch_deleted:
            raise GitLabBranchNotFound("source branch was deleted after merge")
        return super().get_branch(repository, name)

    def merge_formal_merge_request(
        self,
        repository: object,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot:
        super().merge_formal_merge_request(
            repository,
            iid=iid,
            expected_head_sha=expected_head_sha,
        )
        self.branch_deleted = True
        raise GitLabResultUnknown("merge response was lost")


def test_formal_merge_reconciliation_accepts_proven_merge_after_source_deletion(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = MergeAckLossGitLab()
    eligibility = FakeEligibility()
    dependencies = _dependencies(
        isolated_source_control_database,
        requirement,
        gitlab,
        eligibility=eligibility,
    )
    create = _envelope(message_id="91000000-0000-0000-0000-000000000613")
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
        formal_review_decision_id="99000000-0000-0000-0000-000000000613",
    )
    merge = _envelope(
        message_id="91000000-0000-0000-0000-000000000614",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=created.binding.id,
        review_id="99000000-0000-0000-0000-000000000613",
        requirement_revision=14,
        work_item_revision=12,
    )
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            merge,
            dependencies=dependencies,
        )
    unknown = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )
    assert unknown.effect is not None
    assert unknown.effect.state is EffectState.UNKNOWN
    assert gitlab.current.state == "merged"
    assert gitlab.current.head_sha == HEAD_SHA
    eligibility.eligible = False
    dependencies = replace(dependencies, clock=FutureClock())

    reconciled = reconcile_formal_delivery_effect(
        effect_id=unknown.effect.id,
        dependencies=dependencies,
    )

    assert reconciled.effect is not None
    assert reconciled.effect.state is EffectState.SUCCEEDED
    assert reconciled.observation is not None
    assert reconciled.observation.state.value == "MERGED"
    assert gitlab.merged == 1


class FlakyRequirementFormalDelivery(FakeRequirementFormalDelivery):
    def __init__(self) -> None:
        super().__init__(_admission())
        self.ready_attempt_keys: list[str] = []
        self.fail_ready = True

    def record_mr_ready(self, callback: FormalMrReadyCallback) -> None:
        self.ready_attempt_keys.append(callback.idempotency_key)
        if self.fail_ready:
            self.fail_ready = False
            raise RuntimeError("Requirement callback timed out")
        super().record_mr_ready(callback)


def test_failed_formal_callback_is_replayed_with_the_same_idempotency_key(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FlakyRequirementFormalDelivery()
    dependencies = _dependencies(
        isolated_source_control_database,
        requirement,
        FakeFormalGitLab(),
    )
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000615")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            envelope,
            dependencies=dependencies,
        )

    first = process_formal_delivery_request(
        message_id=envelope.message_id,
        dependencies=dependencies,
    )
    replayed = replay_pending_formal_callbacks(
        limit=1,
        dependencies=dependencies,
    )

    assert first.effect is not None
    assert first.effect.state is EffectState.SUCCEEDED
    assert first.effect.callback_state is RequirementCallbackState.FAILED
    assert replayed[0].callback_state is RequirementCallbackState.ACKED
    assert len(requirement.ready) == 1
    assert requirement.ready_attempt_keys == [
        f"source-control:formal-ready:{first.effect.id}",
        f"source-control:formal-ready:{first.effect.id}",
    ]


def test_due_effect_backlog_cannot_starve_pending_formal_callback_replay(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FlakyRequirementFormalDelivery()
    dependencies = _dependencies(
        isolated_source_control_database,
        requirement,
        FakeFormalGitLab(),
    )
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000625")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            envelope,
            dependencies=dependencies,
        )
    callback_failed = process_formal_delivery_request(
        message_id=envelope.message_id,
        dependencies=dependencies,
    )
    assert callback_failed.effect is not None
    assert callback_failed.effect.callback_state is RequirementCallbackState.FAILED

    with isolated_source_control_database.runtime.begin() as db:
        repository = SqlAlchemySourceControlFormalRepository(db)
        for suffix in ("631", "632"):
            work_item_id = f"50000000-0000-0000-0000-000000000{suffix}"
            request_fingerprint = "sha256:" + suffix[-1] * 64
            repository.insert_effect(
                id=f"80000000-0000-0000-0000-000000000{suffix}",
                effect_key=f"source-control:create-formal-mr:{suffix}",
                operation=EffectOperation.CREATE_FORMAL_MR.value,
                subject_key=(f"formal-work-item:{work_item_id}:{HEAD_SHA}:{request_fingerprint}"),
                payload=CreateFormalMergeRequestEffectPayload(
                    acceptanceDecisionId="99000000-0000-0000-0000-000000000601",
                    branchBindingId=f"70000000-0000-0000-0000-000000000{suffix}",
                    headSha=HEAD_SHA,
                ),
                work_item_id=work_item_id,
                requirement_id=f"40000000-0000-0000-0000-000000000{suffix}",
                repository_id="10000000-0000-0000-0000-000000000301",
                request_fingerprint=request_fingerprint,
                attempts=1,
                next_reconcile_at=NOW,
                state=EffectState.UNKNOWN.value,
                requirement_callback_state=RequirementCallbackState.PENDING.value,
                now=NOW,
            )
    dependencies = replace(dependencies, clock=FutureClock())

    reconciled = reconcile_due_formal_effects(
        limit=2,
        dependencies=dependencies,
    )

    assert len(reconciled) == 2
    assert any(
        effect.id == callback_failed.effect.id
        and effect.callback_state is RequirementCallbackState.ACKED
        for effect in reconciled
    )
    assert len(requirement.ready) == 1


class CoordinateProbeGitLab(FakeFormalGitLab):
    def __init__(self) -> None:
        super().__init__()
        self.provider_reads = 0

    def get_project_delivery_profile(
        self,
        repository: object,
    ) -> GitLabProjectDeliveryProfile:
        self.provider_reads += 1
        return super().get_project_delivery_profile(repository)


def test_reconciliation_rejects_admission_drift_before_provider_access(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = CoordinateProbeGitLab()
    gitlab.create_error = GitLabResultUnknown("timeout after write")
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000616")
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
    reads_before_reconciliation = gitlab.provider_reads
    requirement.admission = _admission(requested_head_sha="e" * 40)
    dependencies = replace(dependencies, clock=FutureClock())

    with pytest.raises(FormalDeliveryConflict, match="coordinates are stale"):
        reconcile_formal_delivery_effect(
            effect_id=unknown.effect.id,
            dependencies=dependencies,
        )

    assert gitlab.provider_reads == reads_before_reconciliation
    with isolated_source_control_database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT state FROM source_control.source_control_effect WHERE id=:effect_id"),
                {"effect_id": unknown.effect.id},
            ).scalar_one()
            == EffectState.UNKNOWN.value
        )

    requirement.admission = _admission()
    gitlab.create_error = None
    gitlab.candidates = [gitlab.snapshot()]
    dependencies = replace(dependencies, clock=LaterClock())
    recovered = reconcile_formal_delivery_effect(
        effect_id=unknown.effect.id,
        dependencies=dependencies,
    )
    assert recovered.effect is not None
    assert recovered.effect.state is EffectState.SUCCEEDED


class HeadDriftsAfterPreflightGitLab(FakeFormalGitLab):
    def __init__(self) -> None:
        super().__init__()
        self.branch_reads = 0

    def get_branch(self, repository: object, name: str) -> BranchSnapshot:
        self.branch_reads += 1
        if self.branch_reads == 1:
            return super().get_branch(repository, name)
        return BranchSnapshot(name=name, commit_sha="e" * 40)


class DeterministicMergeBlockGitLab(FakeFormalGitLab):
    def __init__(self) -> None:
        super().__init__()
        self.branch_error: Exception | None = None
        self.branch_error_on_read: int | None = None
        self.branch_reads = 0
        self.merge_error: Exception | None = None
        self.merge_result_snapshot: GitLabMergeRequestSnapshot | None = None
        self.merge_request_read_error: Exception | None = None
        self.list_error: Exception | None = None
        self.project_error: Exception | None = None

    def get_project_delivery_profile(
        self,
        repository: object,
    ) -> GitLabProjectDeliveryProfile:
        if self.project_error is not None:
            raise self.project_error
        return super().get_project_delivery_profile(repository)

    def get_branch(self, repository: object, name: str) -> BranchSnapshot:
        self.branch_reads += 1
        if self.branch_error is not None and (
            self.branch_error_on_read is None or self.branch_reads == self.branch_error_on_read
        ):
            raise self.branch_error
        return super().get_branch(repository, name)

    def get_merge_request(
        self,
        repository: object,
        *,
        iid: int,
    ) -> GitLabMergeRequestSnapshot:
        if self.merge_request_read_error is not None:
            raise self.merge_request_read_error
        return super().get_merge_request(repository, iid=iid)

    def list_merge_requests(
        self,
        repository: object,
        *,
        source_branch: str,
        target_branch: str,
        state: Literal["all"] = "all",
    ) -> list[GitLabMergeRequestSnapshot]:
        if self.list_error is not None:
            raise self.list_error
        return super().list_merge_requests(
            repository,
            source_branch=source_branch,
            target_branch=target_branch,
            state=state,
        )

    def merge_formal_merge_request(
        self,
        repository: object,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot:
        if self.merge_error is not None:
            raise self.merge_error
        if self.merge_result_snapshot is not None:
            self.current = self.merge_result_snapshot
            return self.current
        return super().merge_formal_merge_request(
            repository,
            iid=iid,
            expected_head_sha=expected_head_sha,
        )


def _prepare_formal_merge(
    source: IsolatedSourceControlDatabase,
    *,
    gitlab: DeterministicMergeBlockGitLab,
    message_suffix: str,
) -> tuple[
    FakeRequirementFormalDelivery,
    SourceControlDependencies,
    FormalDeliveryRequestEnvelope,
]:
    requirement = FakeRequirementFormalDelivery(_admission())
    dependencies = _dependencies(source, requirement, gitlab)
    create = _envelope(message_id=f"91000000-0000-0000-0000-000000000{message_suffix}1")
    with source.runtime.begin() as db:
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
    review_id = f"99000000-0000-0000-0000-000000000{message_suffix}2"
    requirement.admission = _admission(
        requirement_revision=14,
        work_item_revision=12,
        formal_merge_request_binding_id=created.binding.id,
        formal_review_decision_id=review_id,
    )
    merge = _envelope(
        message_id=f"91000000-0000-0000-0000-000000000{message_suffix}2",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=created.binding.id,
        review_id=review_id,
        requirement_revision=14,
        work_item_revision=12,
    )
    with source.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            merge,
            dependencies=dependencies,
        )
    return requirement, dependencies, merge


@pytest.mark.parametrize(
    ("provider_block", "reason_code"),
    [
        (GitLabMergeRequestHeadChanged("head changed"), "HEAD_SHA_CHANGED"),
        (GitLabMergeRequestBlocked("checks blocked"), "MR_CHECKS_BLOCKED"),
        (GitLabBranchNotFound("source branch missing"), "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION"),
    ],
)
def test_post_effect_merge_provider_blocks_are_terminal(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    provider_block: Exception,
    reason_code: str,
) -> None:
    _seed(isolated_source_control_database)
    gitlab = DeterministicMergeBlockGitLab()
    requirement, dependencies, merge = _prepare_formal_merge(
        isolated_source_control_database,
        gitlab=gitlab,
        message_suffix={
            "HEAD_SHA_CHANGED": "71",
            "MR_CHECKS_BLOCKED": "72",
            "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION": "73",
        }[reason_code],
    )
    if isinstance(provider_block, GitLabBranchNotFound):
        gitlab.branch_error = provider_block
        gitlab.branch_error_on_read = gitlab.branch_reads + 2
    else:
        gitlab.merge_error = provider_block

    blocked = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state is RequirementCallbackState.ACKED
    assert blocked.effect.last_error_code == reason_code
    assert blocked.blocked_reason == reason_code
    assert requirement.blocked[-1].reason_code.value == reason_code
    with isolated_source_control_database.owner.connect() as db:
        inbox = db.execute(
            text(
                "SELECT state, last_error_code FROM "
                "source_control.formal_delivery_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": merge.message_id},
        ).one()
    assert inbox == ("PROCESSED", reason_code)


def test_new_formal_merge_request_retries_after_block_with_append_only_effect(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    gitlab = DeterministicMergeBlockGitLab()
    requirement, dependencies, first_request = _prepare_formal_merge(
        isolated_source_control_database,
        gitlab=gitlab,
        message_suffix="82",
    )
    gitlab.merge_error = GitLabMergeRequestBlocked("checks blocked")

    first = process_formal_delivery_request(
        message_id=first_request.message_id,
        dependencies=dependencies,
    )
    assert first.effect is not None
    assert first.effect.state is EffectState.BLOCKED
    assert first.blocked_reason == "MR_CHECKS_BLOCKED"

    gitlab.merge_error = None
    requirement.admission = requirement.admission.model_copy(
        update={"requirement_revision": 16, "work_item_revision": 14}
    )
    second_request = _envelope(
        message_id="91000000-0000-0000-0000-000000000823",
        kind=FormalDeliveryRequestKind.MERGE_MR,
        binding_id=first_request.formal_merge_request_binding_id,
        review_id=first_request.formal_review_decision_id,
        requirement_revision=16,
        work_item_revision=14,
    )
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

    assert second.effect is not None
    assert second.effect.state is EffectState.SUCCEEDED
    assert second.effect.id != first.effect.id
    assert replayed_first.effect is not None
    assert replayed_first.effect.id == first.effect.id
    assert replayed_first.effect.state is EffectState.BLOCKED
    assert replayed_first.blocked_reason == "MR_CHECKS_BLOCKED"
    assert gitlab.merged == 1
    with isolated_source_control_database.owner.connect() as db:
        effects = db.execute(
            text(
                "SELECT id, request_fingerprint, state "
                "FROM source_control.source_control_effect "
                "WHERE operation='MERGE_FORMAL_MR' ORDER BY created_at, id"
            )
        ).all()
    assert [(str(row.id), row.request_fingerprint, row.state) for row in effects] == [
        (first.effect.id, first_request.payload_hash, "BLOCKED"),
        (second.effect.id, second_request.payload_hash, "SUCCEEDED"),
    ]


@pytest.mark.parametrize(
    ("provider_block", "reason_code"),
    [
        (GitLabMergeRequestHeadChanged("head changed"), "HEAD_SHA_CHANGED"),
        (GitLabMergeRequestBlocked("checks blocked"), "MR_CHECKS_BLOCKED"),
        (GitLabBranchNotFound("source branch missing"), "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION"),
        (GitLabMergeRequestNotFound("merge request missing"), "MR_CLOSED"),
    ],
)
def test_reconciliation_provider_blocks_do_not_return_to_unknown(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    provider_block: Exception,
    reason_code: str,
) -> None:
    _seed(isolated_source_control_database)
    gitlab = DeterministicMergeBlockGitLab()
    requirement, dependencies, merge = _prepare_formal_merge(
        isolated_source_control_database,
        gitlab=gitlab,
        message_suffix={
            "HEAD_SHA_CHANGED": "74",
            "MR_CHECKS_BLOCKED": "75",
            "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION": "76",
            "MR_CLOSED": "77",
        }[reason_code],
    )
    gitlab.merge_error = GitLabResultUnknown("merge result unknown")
    unknown = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )
    assert unknown.effect is not None
    assert unknown.effect.state is EffectState.UNKNOWN
    gitlab.merge_error = None
    if isinstance(provider_block, GitLabBranchNotFound):
        gitlab.branch_error = provider_block
    elif isinstance(provider_block, GitLabMergeRequestNotFound):
        gitlab.merge_request_read_error = provider_block
    else:
        gitlab.merge_error = provider_block
    dependencies = replace(dependencies, clock=FutureClock())

    blocked = reconcile_formal_delivery_effect(
        effect_id=unknown.effect.id,
        dependencies=dependencies,
    )

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state is RequirementCallbackState.ACKED
    assert blocked.effect.last_error_code == reason_code
    assert blocked.blocked_reason == reason_code
    assert requirement.blocked[-1].reason_code.value == reason_code


def test_open_merge_reconciliation_rechecks_revoked_actor_before_second_write(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    gitlab = DeterministicMergeBlockGitLab()
    requirement, dependencies, merge = _prepare_formal_merge(
        isolated_source_control_database,
        gitlab=gitlab,
        message_suffix="84",
    )
    gitlab.merge_error = GitLabResultUnknown("merge failed before provider write")
    unknown = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )
    assert unknown.effect is not None
    assert unknown.effect.state is EffectState.UNKNOWN
    assert gitlab.merged == 0

    gitlab.merge_error = None
    assert isinstance(dependencies.eligibility, FakeEligibility)
    dependencies.eligibility.eligible = False
    dependencies = replace(dependencies, clock=FutureClock())
    blocked = reconcile_formal_delivery_effect(
        effect_id=unknown.effect.id,
        dependencies=dependencies,
    )

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state is RequirementCallbackState.ACKED
    assert blocked.blocked_reason == "MERGE_ACTOR_INELIGIBLE"
    assert requirement.blocked[-1].reason_code.value == "MERGE_ACTOR_INELIGIBLE"
    assert gitlab.merged == 0


def test_failed_inbox_converges_after_reconciliation_blocks_revoked_actor(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    gitlab = DeterministicMergeBlockGitLab()
    requirement, dependencies, merge = _prepare_formal_merge(
        isolated_source_control_database,
        gitlab=gitlab,
        message_suffix="85",
    )
    assert isinstance(dependencies.eligibility, FakeEligibility)
    dependencies.eligibility.error = RuntimeError("eligibility unavailable")
    with pytest.raises(SourceControlDependencyUnavailable, match="eligibility unavailable"):
        process_formal_delivery_request(
            message_id=merge.message_id,
            dependencies=dependencies,
        )

    with isolated_source_control_database.runtime.begin() as db:
        repository = SqlAlchemySourceControlFormalRepository(db)
        request = repository.formal_request(merge.message_id, for_update=True)
        assert request is not None
        failed = repository.fail_formal_request(
            merge.message_id,
            expected_attempts=request["attempts"],
            now=NOW,
            retry_at=FutureClock().now(),
            error_code="CONNECTOR_UNAVAILABLE",
        )
        assert failed is not None
    with isolated_source_control_database.owner.connect() as db:
        effect_id = str(
            db.execute(
                text(
                    "SELECT id FROM source_control.source_control_effect "
                    "WHERE operation='MERGE_FORMAL_MR' "
                    "AND request_fingerprint=:request_fingerprint"
                ),
                {"request_fingerprint": merge.payload_hash},
            ).scalar_one()
        )

    dependencies.eligibility.error = None
    dependencies.eligibility.eligible = False
    dependencies = replace(dependencies, clock=FutureClock())
    blocked = reconcile_formal_delivery_effect(
        effect_id=effect_id,
        dependencies=dependencies,
    )
    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state is RequirementCallbackState.ACKED
    assert requirement.blocked[-1].reason_code.value == "MERGE_ACTOR_INELIGIBLE"

    dependencies = replace(dependencies, clock=LaterClock())
    replayed = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )
    assert replayed.effect is not None
    assert replayed.effect.id == effect_id
    assert replayed.effect.state is EffectState.BLOCKED
    assert replayed.blocked_reason == "MERGE_ACTOR_INELIGIBLE"
    assert gitlab.merged == 0
    with isolated_source_control_database.owner.connect() as db:
        inbox = db.execute(
            text(
                "SELECT state, last_error_code FROM "
                "source_control.formal_delivery_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": merge.message_id},
        ).one()
    assert inbox == ("PROCESSED", "MERGE_ACTOR_INELIGIBLE")


def test_post_effect_create_policy_block_is_terminal(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = DeterministicMergeBlockGitLab()
    gitlab.create_error = GitLabProjectPolicyUnsupported("merge policy changed")
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000781")
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

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.last_error_code == "PROJECT_PROFILE_UNSUPPORTED"
    assert blocked.blocked_reason == "PROJECT_PROFILE_UNSUPPORTED"


def test_create_reconciliation_terminalizes_deterministic_candidate_read_error(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = DeterministicMergeBlockGitLab()
    gitlab.create_error = GitLabResultUnknown("create result unknown")
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000782")
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
    gitlab.create_error = None
    gitlab.list_error = GitLabAccessDenied("candidate read denied")
    dependencies = replace(dependencies, clock=FutureClock())

    blocked = reconcile_formal_delivery_effect(
        effect_id=unknown.effect.id,
        dependencies=dependencies,
    )

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.last_error_code == "REPOSITORY_NOT_AUTHORIZED"
    assert blocked.blocked_reason == "REPOSITORY_NOT_AUTHORIZED"


@pytest.mark.parametrize(
    ("readback_changes", "reason_code"),
    [
        ({"state": "closed"}, "MR_CLOSED"),
        ({"has_conflicts": True}, "MERGE_CONFLICT"),
        ({"head_pipeline_status": "failed"}, "MR_CHECKS_BLOCKED"),
    ],
)
def test_post_merge_deterministic_readback_is_terminal(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    readback_changes: dict[str, object],
    reason_code: str,
) -> None:
    _seed(isolated_source_control_database)
    gitlab = DeterministicMergeBlockGitLab()
    requirement, dependencies, merge = _prepare_formal_merge(
        isolated_source_control_database,
        gitlab=gitlab,
        message_suffix={
            "MR_CLOSED": "79",
            "MERGE_CONFLICT": "80",
            "MR_CHECKS_BLOCKED": "81",
        }[reason_code],
    )
    gitlab.merge_result_snapshot = gitlab.current.model_copy(update=readback_changes)

    blocked = process_formal_delivery_request(
        message_id=merge.message_id,
        dependencies=dependencies,
    )

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.last_error_code == reason_code
    assert blocked.blocked_reason == reason_code
    assert requirement.blocked[-1].reason_code.value == reason_code


def test_missing_target_branch_persists_terminal_effect_before_callback(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = DeterministicMergeBlockGitLab()
    gitlab.project_error = GitLabBranchNotFound("delivery target branch missing")
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000821")
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

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state is RequirementCallbackState.ACKED
    assert blocked.blocked_reason == "TARGET_BRANCH_NOT_FOUND"
    assert requirement.blocked[-1].reason_code.value == "TARGET_BRANCH_NOT_FOUND"


class FlakyBlockedRequirement(FakeRequirementFormalDelivery):
    def __init__(self) -> None:
        super().__init__(_admission())
        self.blocked_attempt_keys: list[str] = []
        self.fail_blocked = True

    def record_blocked(self, callback: FormalDeliveryBlockedCallback) -> None:
        self.blocked_attempt_keys.append(callback.idempotency_key)
        if self.fail_blocked:
            self.fail_blocked = False
            raise RuntimeError("Requirement blocked callback timed out")
        super().record_blocked(callback)


def test_preflight_block_is_durable_before_callback_and_replays_after_context_drift(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FlakyBlockedRequirement()
    gitlab = FakeFormalGitLab()
    gitlab.branch_head = "e" * 40
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000829")
    with isolated_source_control_database.runtime.begin() as db:
        accept_formal_delivery_request(
            SqlAlchemySourceControlFormalRepository(db),
            envelope,
            dependencies=dependencies,
        )

    first = process_formal_delivery_request(
        message_id=envelope.message_id,
        dependencies=dependencies,
    )
    assert first.effect is not None
    assert first.effect.state is EffectState.BLOCKED
    assert first.effect.callback_state is RequirementCallbackState.FAILED
    with isolated_source_control_database.owner.connect() as db:
        inbox = db.execute(
            text(
                "SELECT state, last_error_code FROM "
                "source_control.formal_delivery_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": envelope.message_id},
        ).one()
    assert inbox == ("PROCESSED", "HEAD_SHA_CHANGED")

    requirement.admission = requirement.admission.model_copy(
        update={"requirement_revision": 99, "work_item_revision": 99}
    )
    replayed = process_formal_delivery_request(
        message_id=envelope.message_id,
        dependencies=dependencies,
    )

    assert replayed.effect is not None
    assert replayed.effect.callback_state is RequirementCallbackState.ACKED
    assert requirement.blocked_attempt_keys == [
        f"source-control:formal-blocked:{first.effect.id}",
        f"source-control:formal-blocked:{first.effect.id}",
    ]
    assert len(requirement.blocked) == 1
    assert gitlab.created == 0


def test_head_drift_after_effect_acquisition_is_terminal_and_callback_replayable(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FakeRequirementFormalDelivery(_admission())
    gitlab = HeadDriftsAfterPreflightGitLab()
    dependencies = _dependencies(isolated_source_control_database, requirement, gitlab)
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000626")
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

    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state is RequirementCallbackState.ACKED
    assert blocked.blocked_reason == "HEAD_SHA_CHANGED"
    assert len(requirement.blocked) == 1
    assert requirement.blocked[0].reason_code.value == "HEAD_SHA_CHANGED"
    assert gitlab.created == 0
    with isolated_source_control_database.owner.connect() as db:
        inbox = db.execute(
            text(
                "SELECT state, last_error_code FROM "
                "source_control.formal_delivery_request_inbox "
                "WHERE message_id=:message_id"
            ),
            {"message_id": envelope.message_id},
        ).one()
    assert inbox == ("PROCESSED", "HEAD_SHA_CHANGED")


def test_failed_terminal_block_callback_replays_with_stable_idempotency_key(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    _seed(isolated_source_control_database)
    requirement = FlakyBlockedRequirement()
    dependencies = _dependencies(
        isolated_source_control_database,
        requirement,
        HeadDriftsAfterPreflightGitLab(),
    )
    envelope = _envelope(message_id="91000000-0000-0000-0000-000000000627")
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
    assert blocked.effect is not None
    assert blocked.effect.callback_state is RequirementCallbackState.FAILED

    replayed = replay_pending_formal_callbacks(limit=1, dependencies=dependencies)

    assert replayed[0].callback_state is RequirementCallbackState.ACKED
    assert requirement.blocked_attempt_keys == [
        f"source-control:formal-blocked:{blocked.effect.id}",
        f"source-control:formal-blocked:{blocked.effect.id}",
    ]
    assert len(requirement.blocked) == 1
