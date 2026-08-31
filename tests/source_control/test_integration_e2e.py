from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.requirement import (
    DecisionOutcome,
    IntegrationDeliveryState,
    RequirementDependencies,
    RequirementState,
    WorkItemState,
    decide_baseline,
    get_requirement,
    register_sdd_baseline,
    submit_baseline_confirmation,
)
from control_plane.app.modules.requirement.api import (
    RequirementHttpRuntime,
    create_requirement_delivery_router,
)
from control_plane.app.modules.source_control import (
    EffectOperation,
    SourceControlDependencies,
    process_due_source_control_inboxes,
    register_workspace_repository,
    relay_due_source_control_requests,
)
from control_plane.app.modules.source_control.adapters import (
    RequirementFacadeBindingAdapter,
    RequirementFacadeDeliveryAdapter,
    SqlAlchemySourceControlIntegrationRepository,
    SqlAlchemySourceControlRepository,
)
from control_plane.app.modules.source_control.ports import (
    ActorEligibilityContext,
    BindingEligibility,
    BranchSnapshot,
    GitLabMergeRequestLocator,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
)
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from tests.requirement.conftest import (
    IsolatedRequirementDatabase,
    isolated_requirement_database,
    requirement_owner_engine,
)
from tests.requirement.test_baseline_gate import _gate_dependencies
from tests.requirement.test_commands import Actor
from tests.requirement.test_source_control_relay import (
    WORKSPACE_ID,
    create_assigned_requirement,
)
from tests.requirement.test_source_control_relay import (
    dependencies as binding_requirement_dependencies,
)
from tests.source_control.conftest import IsolatedSourceControlDatabase

NOW = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
HEAD_SHA = "b" * 40
DEV_SHA = "c" * 40
MERGE_SHA = "d" * 40
REPOSITORY_ID = "repository-source-control-1"
SAME_ORIGIN = {"Origin": "http://testserver"}

# Register the cross-module PostgreSQL fixtures in this test module.
assert isolated_requirement_database and requirement_owner_engine


class MutableClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


class RandomValues:
    def uuid4(self) -> UUID:
        return uuid4()


@dataclass(slots=True)
class EligibleActor:
    seen: list[ActorEligibilityContext]

    def evaluate(self, context: ActorEligibilityContext) -> BindingEligibility:
        self.seen.append(context)
        return BindingEligibility(eligible=True)


class FixedPolicy:
    def next_reconcile_at(self, *, now: datetime, attempts: int) -> datetime:
        return now + timedelta(minutes=max(1, attempts))

    def webhook_replay_window(self) -> timedelta:
        return timedelta(minutes=5)


class StatefulFakeGitLab:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.branches = {"main": "a" * 40, "dev": DEV_SHA}
        self.source_branch: str | None = None
        self.calls: list[str] = []
        self.merge_request: GitLabMergeRequestSnapshot | None = None

    def validate_repository(self, _repository: object) -> None:
        self.calls.append("validate_repository")

    def get_project_delivery_profile(self, _repository: object) -> GitLabProjectDeliveryProfile:
        self.calls.append("profile")
        return GitLabProjectDeliveryProfile(
            project_id="101",
            project_path="platform/backend",
            default_branch="main",
            merge_method="merge",
        )

    def get_branch(self, _repository: object, name: str) -> BranchSnapshot:
        self.calls.append(f"get_branch:{name}")
        return BranchSnapshot(name=name, commit_sha=self.branches[name])

    def create_branch(
        self,
        _repository: object,
        *,
        name: str,
        ref_sha: str,
    ) -> BranchSnapshot:
        self.calls.append("create_branch")
        assert name not in self.branches
        self.source_branch = name
        self.branches[name] = ref_sha
        return BranchSnapshot(name=name, commit_sha=ref_sha)

    def list_merge_requests(
        self,
        _repository: object,
        *,
        source_branch: str,
        target_branch: str,
        state: Literal["all"] = "all",
    ) -> list[GitLabMergeRequestSnapshot]:
        self.calls.append("list_mr")
        assert self.source_branch is not None
        assert (source_branch, target_branch, state) == (self.source_branch, "dev", "all")
        return [] if self.merge_request is None else [self.merge_request]

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
        assert self.source_branch is not None
        assert self.merge_request is None
        assert source_branch == self.source_branch
        assert target_branch == "dev"
        assert self.branches[source_branch] == expected_head_sha == HEAD_SHA
        assert title.startswith("feat: integrate ")
        assert "Source-Control-Effect:" in description
        self.merge_request = self._snapshot(state="opened")
        return GitLabMergeRequestLocator(
            project_id="101",
            iid=17,
            source_branch=source_branch,
            target_branch=target_branch,
        )

    def get_merge_request(
        self,
        _repository: object,
        *,
        iid: int,
    ) -> GitLabMergeRequestSnapshot:
        self.calls.append("get_mr")
        assert iid == 17
        assert self.merge_request is not None
        return self.merge_request

    def merge_merge_request(
        self,
        _repository: object,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot:
        self.calls.append("merge_mr")
        assert self.source_branch is not None
        assert self.merge_request is not None
        assert self.merge_request.state == "opened"
        assert iid == 17
        assert self.branches[self.source_branch] == expected_head_sha == HEAD_SHA
        self.merge_request = self._snapshot(state="merged")
        return self.merge_request

    def _snapshot(
        self,
        *,
        state: Literal["opened", "merged"],
    ) -> GitLabMergeRequestSnapshot:
        assert self.source_branch is not None
        merged = state == "merged"
        return GitLabMergeRequestSnapshot(
            project_id="101",
            iid=17,
            source_branch=self.source_branch,
            target_branch="dev",
            head_sha=HEAD_SHA,
            state=state,
            detailed_merge_status="mergeable",
            has_conflicts=False,
            blocking_discussions_resolved=True,
            head_pipeline_status="success",
            merge_commit_sha=MERGE_SHA if merged else None,
            merge_user_id="provider-user-17" if merged else None,
            merged_at=self.clock.now() if merged else None,
        )


def _requirement_dependencies() -> RequirementDependencies:
    return replace(
        _gate_dependencies(),
        clock=binding_requirement_dependencies().clock,
    )


def _source_control_dependencies(
    source: IsolatedSourceControlDatabase,
    requirement: IsolatedRequirementDatabase,
    gitlab: StatefulFakeGitLab,
    clock: MutableClock,
) -> SourceControlDependencies:
    requirement_dependencies = _requirement_dependencies()
    return SourceControlDependencies(
        repository_factory=SqlAlchemySourceControlRepository,
        engine=source.runtime,
        requirement=RequirementFacadeBindingAdapter(
            requirement.runtime,
            requirement_dependencies,
            clock,
        ),
        eligibility=EligibleActor([]),
        audit=SqlAlchemyTransactionalAuditAppender(),
        clock=clock,
        random=RandomValues(),
        gitlab=gitlab,
        policy=FixedPolicy(),
        delivery_repository_factory=SqlAlchemySourceControlIntegrationRepository,
        requirement_delivery=RequirementFacadeDeliveryAdapter(
            requirement.runtime,
            requirement_dependencies,
        ),
        gitlab_merge_requests=gitlab,
    )


def _register_repository(
    source: IsolatedSourceControlDatabase,
    dependencies: SourceControlDependencies,
) -> None:
    with source.runtime.begin() as db:
        register_workspace_repository(
            SqlAlchemySourceControlRepository(db),
            repository_id=REPOSITORY_ID,
            workspace_id=WORKSPACE_ID,
            project_id="101",
            project_path="platform/backend",
            connection_ref="gitlab-dev",
            credential_secret_ref="secret-ref:credential",
            webhook_signing_secret_ref="secret-ref:webhook",
            actor="SYSTEM",
            dependencies=dependencies,
        )


def _approve_sdd_baseline(
    requirement: IsolatedRequirementDatabase,
    *,
    requirement_id: str,
) -> None:
    dependencies = _requirement_dependencies()
    with requirement.runtime.connect() as db:
        preparing = get_requirement(
            db,
            requirement_id=requirement_id,
            dependencies=dependencies,
        )
    assert preparing.requirement.state is RequirementState.PREPARING
    with requirement.runtime.begin() as db:
        baseline = register_sdd_baseline(
            db,
            requirement_id=requirement_id,
            artifact_id="sdd-1",
            artifact_version="version-1",
            expected_revision=preparing.requirement.revision,
            actor=Actor("employee-source-control-1"),
            idempotency_key="task10-baseline-register",
            dependencies=dependencies,
        )
    with requirement.runtime.begin() as db:
        confirmation = submit_baseline_confirmation(
            db,
            requirement_id=requirement_id,
            sdd_baseline_id=baseline.baseline.id,
            expected_revision=baseline.requirement.revision,
            actor=Actor("employee-source-control-1"),
            idempotency_key="task10-baseline-confirm",
            dependencies=dependencies,
        )
    with requirement.runtime.begin() as db:
        decide_baseline(
            db,
            requirement_id=requirement_id,
            gate_id=confirmation.gate.id,
            outcome=DecisionOutcome.APPROVED,
            reason="The SDD is executable.",
            expected_revision=confirmation.requirement.revision,
            actor=Actor("reviewer-1"),
            idempotency_key="task10-baseline-decide",
            dependencies=dependencies,
        )


def _delivery_client(
    requirement: IsolatedRequirementDatabase,
    dependencies: RequirementDependencies,
    *,
    actor_id: str,
) -> TestClient:
    def principal() -> Actor:
        return Actor(actor_id)

    def capability_guard(
        _principal: Any,
        capability: str,
        workspace_id: str | None,
    ) -> None:
        allowed = (
            {"merge_request.merge"} if actor_id == "merge-operator-1" else {"work_item.execute"}
        )
        if workspace_id != WORKSPACE_ID or capability not in allowed:
            raise HTTPException(status_code=403, detail="Forbidden")

    app = FastAPI()
    app.middleware("http")(request_id_middleware)
    register_problem_handlers(app)
    app.include_router(
        create_requirement_delivery_router(
            lambda: RequirementHttpRuntime(
                engine=requirement.runtime,
                dependencies=dependencies,
            ),
            principal,
            capability_guard,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def _versioned_headers(key: str, revision: int) -> dict[str, str]:
    return {
        **SAME_ORIGIN,
        "Idempotency-Key": key,
        "If-Match": f'"v{revision}"',
    }


def _requirement_callback_facts(
    requirement: IsolatedRequirementDatabase,
    *,
    work_item_id: str,
) -> tuple[tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]]:
    with requirement.owner.connect() as db:
        event_rows = db.execute(
            text(
                "SELECT action, target_id, correlation_id FROM audit.audit_event "
                "WHERE target_id=:work_item_id "
                "AND action IN ('requirement.integration_delivery.mr_ready', "
                "'requirement.integration_delivery.merged', "
                "'requirement.repository_binding.recorded') ORDER BY action"
            ),
            {"work_item_id": work_item_id},
        ).all()
        events = tuple(
            (str(row.action), str(row.target_id), str(row.correlation_id)) for row in event_rows
        )
        record_rows = db.execute(
            text(
                "SELECT operation, idempotency_key, state "
                "FROM requirement.idempotency_record "
                "WHERE operation IN ('requirement_record_integration_mr_ready', "
                "'requirement_record_integration_merged') ORDER BY operation"
            )
        ).all()
        records = tuple(
            (str(row.operation), str(row.idempotency_key), str(row.state)) for row in record_rows
        )
    return events, records


def test_human_integration_mr_flow_converges_through_only_public_batch_facades(
    isolated_source_control_database: IsolatedSourceControlDatabase,
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    requirement_dependencies = _requirement_dependencies()
    created = create_assigned_requirement(isolated_requirement_database)
    owner_id = created.work_item.human_owner_id
    assert owner_id is not None
    clock = MutableClock()
    gitlab = StatefulFakeGitLab(clock)
    dependencies = _source_control_dependencies(
        isolated_source_control_database,
        isolated_requirement_database,
        gitlab,
        clock,
    )
    _register_repository(isolated_source_control_database, dependencies)

    binding_relay = relay_due_source_control_requests(limit=2, dependencies=dependencies)
    binding_process = process_due_source_control_inboxes(limit=3, dependencies=dependencies)
    _approve_sdd_baseline(
        isolated_requirement_database,
        requirement_id=created.requirement.id,
    )
    with isolated_requirement_database.runtime.connect() as db:
        ready = get_requirement(
            db,
            requirement_id=created.requirement.id,
            dependencies=requirement_dependencies,
        )
    task_branch = ready.work_items[0].task_branch
    assert task_branch is not None
    assert gitlab.source_branch == task_branch
    assert gitlab.branches[task_branch] == "a" * 40
    gitlab.branches[task_branch] = HEAD_SHA
    client = _delivery_client(
        isolated_requirement_database,
        requirement_dependencies,
        actor_id=owner_id,
    )
    delivery_url = (
        f"/api/v1/requirements/{ready.requirement.id}/work-items/{ready.work_items[0].id}"
    )
    started = client.post(
        f"{delivery_url}:start",
        headers=_versioned_headers("task10-start", ready.requirement.revision),
    )
    assert started.status_code == 200, started.text
    requested_mr = client.post(
        f"{delivery_url}:request-integration-mr",
        headers=_versioned_headers(
            "task10-request-mr",
            started.json()["requirement"]["revision"],
        ),
    )
    assert requested_mr.status_code == 202, requested_mr.text

    first_relay = relay_due_source_control_requests(limit=2, dependencies=dependencies)
    first_process = process_due_source_control_inboxes(limit=3, dependencies=dependencies)
    with isolated_requirement_database.runtime.connect() as db:
        mr_ready = get_requirement(
            db,
            requirement_id=ready.requirement.id,
            dependencies=requirement_dependencies,
        )
    clock.advance(timedelta(minutes=1))
    merge_actor_id = "merge-operator-1"
    merge_client = _delivery_client(
        isolated_requirement_database,
        requirement_dependencies,
        actor_id=merge_actor_id,
    )
    requested_merge = merge_client.post(
        f"{delivery_url}:request-integration-merge",
        headers=_versioned_headers(
            "task10-request-merge",
            mr_ready.requirement.revision,
        ),
    )
    assert requested_merge.status_code == 202, requested_merge.text
    second_relay = relay_due_source_control_requests(limit=2, dependencies=dependencies)
    second_process = process_due_source_control_inboxes(limit=3, dependencies=dependencies)

    with isolated_requirement_database.runtime.connect() as db:
        converged = get_requirement(
            db,
            requirement_id=created.requirement.id,
            dependencies=requirement_dependencies,
        )
    callback_facts_before = _requirement_callback_facts(
        isolated_requirement_database,
        work_item_id=created.work_item.id,
    )
    same_key_replay = merge_client.post(
        f"{delivery_url}:request-integration-merge",
        headers=_versioned_headers(
            "task10-request-merge",
            mr_ready.requirement.revision,
        ),
    )
    assert same_key_replay.status_code == 202, same_key_replay.text
    duplicate_relay = relay_due_source_control_requests(limit=2, dependencies=dependencies)
    duplicate_process = process_due_source_control_inboxes(limit=3, dependencies=dependencies)
    with isolated_requirement_database.runtime.connect() as db:
        final = get_requirement(
            db,
            requirement_id=created.requirement.id,
            dependencies=requirement_dependencies,
        )
    callback_facts_after = _requirement_callback_facts(
        isolated_requirement_database,
        work_item_id=created.work_item.id,
    )

    with isolated_source_control_database.owner.connect() as db:
        binding_count = db.execute(
            text("SELECT count(*) FROM source_control.merge_request_binding")
        ).scalar_one()
        observations = db.execute(
            text(
                "SELECT state, observed_at FROM source_control.merge_request_observation "
                "ORDER BY observed_at"
            )
        ).all()
        effects = db.execute(
            text(
                "SELECT operation, id::text, state, requirement_callback_state "
                "FROM source_control.source_control_effect ORDER BY operation"
            )
        ).all()
        effect_target_ids = [str(row.id) for row in effects]
        audit_rows = tuple(
            db.execute(
                text(
                    "SELECT action, target_id, correlation_id FROM audit.audit_event "
                    "WHERE target_type='source_control_effect' "
                    "AND target_id=ANY(CAST(:effect_ids AS TEXT[]))"
                ),
                {"effect_ids": effect_target_ids},
            ).all()
        )

    assert (binding_relay.claimed, binding_process.claimed) == (1, 1)
    assert (first_relay.claimed, first_process.claimed) == (1, 1)
    assert (second_relay.claimed, second_process.claimed) == (1, 1)
    assert duplicate_relay.claimed == duplicate_process.claimed == 0
    assert same_key_replay.json() == requested_merge.json()
    assert final.requirement.revision == converged.requirement.revision
    assert final.work_items[0].revision == converged.work_items[0].revision
    assert callback_facts_after == callback_facts_before
    assert isinstance(dependencies.eligibility, EligibleActor)
    assert dependencies.eligibility.seen[-1].actor_id == merge_actor_id
    assert dependencies.eligibility.seen[-1].required_capabilities == ("merge_request.merge",)
    assert final.requirement.state is RequirementState.VERIFYING
    assert final.work_items[0].state is WorkItemState.VERIFYING
    assert final.work_items[0].integration_delivery_state is IntegrationDeliveryState.INTEGRATED
    assert binding_count == 1
    assert [row.state for row in observations] == ["OPEN", "MERGED"]
    assert observations[0].observed_at < observations[1].observed_at
    assert gitlab.calls.count("create_branch") == 1
    assert gitlab.calls.count("create_mr") == 1
    assert gitlab.calls.count("merge_mr") == 1
    assert gitlab.branches[task_branch] == HEAD_SHA

    assert len(effects) == 3
    effects_by_operation_count = Counter(row.operation for row in effects)
    assert effects_by_operation_count == Counter(
        {
            EffectOperation.CREATE_TASK_BRANCH.value: 1,
            EffectOperation.CREATE_INTEGRATION_MR.value: 1,
            EffectOperation.MERGE_INTEGRATION_MR.value: 1,
        }
    )
    effects_by_operation = {row.operation: row for row in effects}
    assert all(
        (row.state, row.requirement_callback_state) == ("SUCCEEDED", "ACKED") for row in effects
    )
    create_effect_id = effects_by_operation[EffectOperation.CREATE_INTEGRATION_MR.value].id
    merge_effect_id = effects_by_operation[EffectOperation.MERGE_INTEGRATION_MR.value].id
    branch_effect_id = effects_by_operation[EffectOperation.CREATE_TASK_BRANCH.value].id
    expected_lifecycle_audit = {
        (
            "source_control.effect.planned",
            branch_effect_id,
            f"source-control:work-item:{created.work_item.id}",
        ),
        (
            "source_control.effect.in_flight",
            branch_effect_id,
            f"source-control:effect:{branch_effect_id}",
        ),
        (
            "source_control.effect.succeeded",
            branch_effect_id,
            f"source-control:effect:{branch_effect_id}",
        ),
        (
            "source_control.requirement_callback.acked",
            branch_effect_id,
            f"source-control:effect:{branch_effect_id}",
        ),
    }
    for prefix, effect_id in (
        ("source_control.integration_mr", create_effect_id),
        ("source_control.integration_merge", merge_effect_id),
    ):
        expected_lifecycle_audit.update(
            {
                (f"{prefix}.planned", effect_id, f"source-control:effect:{effect_id}"),
                (f"{prefix}.in_flight", effect_id, f"source-control:effect:{effect_id}"),
                (f"{prefix}.succeeded", effect_id, f"source-control:effect:{effect_id}"),
            }
        )
    assert len(audit_rows) == 10
    assert Counter(audit_rows) == Counter(expected_lifecycle_audit)

    callback_events, callback_records = callback_facts_after
    assert [(action, target_id) for action, target_id, _correlation in callback_events] == [
        ("requirement.integration_delivery.merged", created.work_item.id),
        ("requirement.integration_delivery.mr_ready", created.work_item.id),
        ("requirement.repository_binding.recorded", created.work_item.id),
    ]
    assert {action: correlation for action, _target_id, correlation in callback_events} == {
        "requirement.integration_delivery.merged": f"source-control:effect:{merge_effect_id}",
        "requirement.integration_delivery.mr_ready": f"source-control:effect:{create_effect_id}",
        "requirement.repository_binding.recorded": f"source-control:effect:{branch_effect_id}",
    }
    assert callback_records == (
        (
            "requirement_record_integration_merged",
            f"source-control:integration-merged:{merge_effect_id}",
            "COMPLETED",
        ),
        (
            "requirement_record_integration_mr_ready",
            f"source-control:mr-ready:{create_effect_id}",
            "COMPLETED",
        ),
    )


def test_source_control_role_cannot_insert_requirement_row(
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    forbidden_id = "40000000-0000-0000-0000-000000001099"
    statement = text(
        "INSERT INTO requirement.requirement "
        "(id, workspace_id, type, title, description, acceptance_criteria, created_by, "
        "initial_repository_id, route_snapshot_version, route_snapshot_hash, state, "
        "record_state, requirement_version, required_work_item_set_version, "
        "required_work_item_set_hash, revision, created_at, updated_at) VALUES "
        "(CAST(:id AS UUID), CAST(:workspace_id AS UUID), 'feat', 'Forbidden insert', "
        "'Cross-schema role denial proof', CAST('[\"denied\"]' AS JSONB), 'attacker', "
        "'repository-denied', 1, 'sha256:denied', 'CREATED', 'ACTIVE', 1, 1, "
        "'sha256:denied-set', 1, :now, :now)"
    )

    with pytest.raises(DBAPIError) as denied:
        with isolated_source_control_database.runtime.begin() as db:
            db.execute(
                statement,
                {"id": forbidden_id, "workspace_id": WORKSPACE_ID, "now": NOW},
            )

    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
    with isolated_source_control_database.owner.connect() as db:
        count = db.execute(
            text("SELECT count(*) FROM requirement.requirement WHERE id=CAST(:id AS UUID)"),
            {"id": forbidden_id},
        ).scalar_one()
    assert count == 0


def test_requirement_role_cannot_insert_source_control_row(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    forbidden_id = "repository-forbidden-task10"
    statement = text(
        "INSERT INTO source_control.workspace_repository "
        "(id, workspace_id, provider, project_id, project_path, default_branch, "
        "connection_ref, credential_secret_ref, webhook_signing_secret_ref, status, "
        "revision, created_at, updated_at) VALUES "
        "(:id, CAST(:workspace_id AS UUID), 'GITLAB', 'forbidden-101', "
        "'forbidden/backend', 'main', 'forbidden-connection', 'secret-ref:forbidden', "
        "'secret-ref:forbidden-webhook', 'AUTHORIZED', 1, :now, :now)"
    )

    with pytest.raises(DBAPIError) as denied:
        with isolated_requirement_database.runtime.begin() as db:
            db.execute(
                statement,
                {"id": forbidden_id, "workspace_id": WORKSPACE_ID, "now": NOW},
            )

    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
    with isolated_requirement_database.owner.connect() as db:
        count = db.execute(
            text("SELECT count(*) FROM source_control.workspace_repository WHERE id=:id"),
            {"id": forbidden_id},
        ).scalar_one()
    assert count == 0
