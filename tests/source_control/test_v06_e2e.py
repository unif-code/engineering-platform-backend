import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from control_plane.app.modules.requirement import (
    DecisionOutcome,
    RequirementDependencies,
    RequirementState,
    WorkItemState,
    get_requirement,
    record_integration_merged,
    record_integration_mr_ready,
    request_integration_merge,
    request_integration_merge_request,
)
from control_plane.app.modules.requirement.adapters import (
    SourceControlFacadeEvidenceAdapter,
)
from control_plane.app.modules.requirement.api import (
    RequirementHttpRuntime,
    create_requirement_v06_delivery_router,
)
from control_plane.app.modules.source_control import (
    EffectState,
    FormalReviewRoutingSnapshot,
    MergeRequestState,
    SourceControlDependencies,
    process_formal_delivery_request,
    process_integration_baseline_request,
    relay_requirement_evidence_requests,
    relay_requirement_formal_delivery_requests,
)
from control_plane.app.modules.source_control.adapters import (
    RequirementFacadeBindingAdapter,
    RequirementFacadeEvidenceAdapter,
    RequirementFacadeFormalDeliveryAdapter,
    SqlAlchemySourceControlFormalRepository,
)
from control_plane.app.modules.source_control.ports import (
    ActorEligibilityContext,
    BindingEligibility,
    BranchSnapshot,
    GitLabMergeRequestLocator,
    GitLabMergeRequestSnapshot,
    GitLabProjectDeliveryProfile,
    GitLabRepositoryProfile,
)
from tests.requirement.conftest import (
    IsolatedRequirementDatabase,
    isolated_requirement_database,
    requirement_owner_engine,
)
from tests.requirement.delivery_policy_helpers import frozen_policy, resolved_policy
from tests.requirement.test_api import SAME_ORIGIN, PrincipalHolder
from tests.requirement.test_baseline_gate import ARTIFACT_HASH_V1, _gate_dependencies
from tests.requirement.test_commands import Actor
from tests.requirement.test_v06_acceptance_commands import (
    StaticDeliveryPolicies,
    StaticDeliveryReviewerGuard,
)
from tests.requirement.test_v06_evidence_commands import _integrated_requirement
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_v06_evidence_application import (
    _dependencies as evidence_dependencies,
)

assert isolated_requirement_database and requirement_owner_engine

pytestmark = pytest.mark.integration

HEAD_SHA = "b" * 40
STALE_HEAD_SHA = "e" * 40
REWORK_HEAD_SHA_1 = "1" * 40
REWORK_HEAD_SHA_2 = "2" * 40
INTEGRATION_MERGE_SHA = "c" * 40
REWORK_INTEGRATION_MERGE_SHA_1 = "3" * 40
REWORK_INTEGRATION_MERGE_SHA_2 = "4" * 40
FORMAL_MERGE_SHA = "d" * 40
BRANCH_BINDING_ID = "70000000-0000-0000-0000-000000000606"
CREATE_BRANCH_EFFECT_ID = "60000000-0000-0000-0000-000000000606"
CREATE_INTEGRATION_EFFECT_ID = "60000000-0000-0000-0000-000000000607"
MERGED_OBSERVATION_ID = "80000000-0000-0000-0000-000000000606"
REWORK_INTEGRATION_BINDING_1 = "97000000-0000-0000-0000-000000000661"
REWORK_INTEGRATION_BINDING_2 = "97000000-0000-0000-0000-000000000662"
REWORK_INTEGRATION_EFFECT_1 = "60000000-0000-0000-0000-000000000661"
REWORK_INTEGRATION_EFFECT_2 = "60000000-0000-0000-0000-000000000662"
REWORK_INTEGRATION_OBSERVATION_1 = "80000000-0000-0000-0000-000000000661"
REWORK_INTEGRATION_OBSERVATION_2 = "80000000-0000-0000-0000-000000000662"


class DynamicFormalGitLab:
    def __init__(self, *, requirement_id: str, task_branch: str) -> None:
        self.requirement_id = requirement_id
        self.task_branch = task_branch
        self.branch_head_sha = HEAD_SHA
        self.current: GitLabMergeRequestSnapshot | None = None
        self.create_calls = 0
        self.merge_calls = 0

    def get_project_delivery_profile(
        self,
        repository: GitLabRepositoryProfile,
    ) -> GitLabProjectDeliveryProfile:
        return GitLabProjectDeliveryProfile(
            project_id=repository.project_id,
            project_path=repository.project_path,
            default_branch="main",
            merge_method="merge",
        )

    def get_branch(
        self,
        repository: GitLabRepositoryProfile,
        name: str,
    ) -> BranchSnapshot:
        del repository
        assert name == self.task_branch
        return BranchSnapshot(name=name, commit_sha=self.branch_head_sha)

    def list_merge_requests(
        self,
        repository: GitLabRepositoryProfile,
        *,
        source_branch: str,
        target_branch: str,
        state: Literal["all"] = "all",
    ) -> list[GitLabMergeRequestSnapshot]:
        del repository
        assert (source_branch, target_branch, state) == (self.task_branch, "main", "all")
        return [] if self.current is None else [self.current]

    def create_formal_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        source_branch: str,
        expected_head_sha: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestLocator:
        del title
        assert source_branch == self.task_branch
        assert expected_head_sha == self.branch_head_sha
        assert self.requirement_id in description
        self.create_calls += 1
        self.current = self._snapshot(state="opened")
        return GitLabMergeRequestLocator(
            project_id=repository.project_id,
            iid=77,
            source_branch=source_branch,
            target_branch="main",
        )

    def get_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        iid: int,
    ) -> GitLabMergeRequestSnapshot:
        del repository
        assert iid == 77
        assert self.current is not None
        return self.current

    def merge_formal_merge_request(
        self,
        repository: GitLabRepositoryProfile,
        *,
        iid: int,
        expected_head_sha: str,
    ) -> GitLabMergeRequestSnapshot:
        del repository
        assert iid == 77
        assert expected_head_sha == self.branch_head_sha
        self.merge_calls += 1
        self.current = self._snapshot(state="merged")
        return self.current

    def _snapshot(
        self,
        *,
        state: Literal["opened", "merged"],
    ) -> GitLabMergeRequestSnapshot:
        merged = state == "merged"
        return GitLabMergeRequestSnapshot(
            project_id="101",
            iid=77,
            source_branch=self.task_branch,
            target_branch="main",
            head_sha=self.branch_head_sha,
            state=state,
            detailed_merge_status="mergeable",
            has_conflicts=False,
            blocking_discussions_resolved=True,
            head_pipeline_status="success",
            merge_commit_sha=FORMAL_MERGE_SHA if merged else None,
            merge_user_id="42" if merged else None,
            merged_at=evidence_dependencies_clock() if merged else None,
        )


def evidence_dependencies_clock() -> datetime:
    from tests.source_control.test_v06_evidence_application import NOW

    return NOW


class OwnerFormalReviewRouting:
    def resolve(
        self,
        *,
        workspace_id: str,
        repository_id: str,
        work_item_id: str,
        human_owner_id: str,
    ) -> FormalReviewRoutingSnapshot:
        return FormalReviewRoutingSnapshot(
            default_reviewer_id=human_owner_id,
            policy_code="FORMAL_REVIEW_WORK_ITEM_OWNER",
            policy_version=1,
            policy_snapshot_hash="sha256:" + resolved_policy(1).snapshot_hash,
            resolution_snapshot={
                **frozen_policy(1),
                "repositoryId": repository_id,
                "rule": "WORK_ITEM_OWNER",
                "workItemId": work_item_id,
                "workspaceId": workspace_id,
            },
        )


class EligibleCurrentOwner:
    def evaluate(self, context: ActorEligibilityContext) -> BindingEligibility:
        assert context.actor_id == "employee-1"
        assert context.required_capabilities in (("code.change",), ("merge_request.merge",))
        return BindingEligibility(eligible=True)


def _seed_source_control_graph(
    source: IsolatedSourceControlDatabase,
    requirement: IsolatedRequirementDatabase,
    *,
    requirement_id: str,
    work_item_id: str,
) -> tuple[str, str, str, str]:
    with requirement.owner.connect() as db:
        workspace_id, repository_id, task_branch, integration_binding_id = db.execute(
            text(
                "SELECT requirement.workspace_id::text, work_item.repository_id, "
                "work_item.task_branch, "
                "work_item.integration_merge_request_binding_id::text "
                "FROM requirement.requirement "
                "JOIN requirement.work_item "
                "ON work_item.requirement_id=requirement.id "
                "WHERE requirement.id=:requirement_id AND work_item.id=:work_item_id"
            ),
            {"requirement_id": requirement_id, "work_item_id": work_item_id},
        ).one()
    create_integration_payload = json.dumps(
        {"branchBindingId": BRANCH_BINDING_ID, "headSha": HEAD_SHA},
        separators=(",", ":"),
        sort_keys=True,
    )
    with source.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO source_control.workspace_repository "
                "(id, workspace_id, provider, project_id, project_path, default_branch, "
                "connection_ref, credential_secret_ref, webhook_signing_secret_ref, "
                "status, revision) VALUES (:repository_id, :workspace_id, 'GITLAB', "
                "'101', 'platform/backend', 'main', 'gitlab-dev', "
                "'secret-ref:credential', 'secret-ref:webhook', 'AUTHORIZED', 1)"
            ),
            {"repository_id": repository_id, "workspace_id": workspace_id},
        )
        db.execute(
            text(
                "INSERT INTO source_control.source_control_effect "
                "(id, effect_key, operation, subject_key, payload, work_item_id, "
                "requirement_id, repository_id, work_item_number, branch_name, "
                "base_commit_sha, request_fingerprint, attempts, state, "
                "requirement_callback_state, completed_at) VALUES "
                "(:id, :effect_key, 'CREATE_TASK_BRANCH', :subject, '{}'::jsonb, "
                ":work_item_id, :requirement_id, :repository_id, 606, :task_branch, "
                ":base_sha, 'sha256:v06-e2e-branch', 1, 'SUCCEEDED', 'ACKED', now())"
            ),
            {
                "id": CREATE_BRANCH_EFFECT_ID,
                "effect_key": f"v06-e2e:branch:{work_item_id}",
                "subject": f"work-item:{work_item_id}",
                "work_item_id": work_item_id,
                "requirement_id": requirement_id,
                "repository_id": repository_id,
                "task_branch": task_branch,
                "base_sha": "a" * 40,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.repository_branch_binding "
                "(id, work_item_id, requirement_id, workspace_id, repository_id, "
                "work_item_number, base_commit_sha, branch_name, effect_id) VALUES "
                "(:id, :work_item_id, :requirement_id, :workspace_id, :repository_id, "
                "606, :base_sha, :task_branch, :effect_id)"
            ),
            {
                "id": BRANCH_BINDING_ID,
                "work_item_id": work_item_id,
                "requirement_id": requirement_id,
                "workspace_id": workspace_id,
                "repository_id": repository_id,
                "base_sha": "a" * 40,
                "task_branch": task_branch,
                "effect_id": CREATE_BRANCH_EFFECT_ID,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.source_control_effect "
                "(id, effect_key, operation, subject_key, payload, work_item_id, "
                "requirement_id, repository_id, request_fingerprint, attempts, state, "
                "requirement_callback_state, completed_at) VALUES "
                "(:id, :effect_key, 'CREATE_INTEGRATION_MR', :subject, "
                "CAST(:payload AS JSONB), :work_item_id, :requirement_id, "
                ":repository_id, 'sha256:v06-e2e-integration', 1, 'SUCCEEDED', "
                "'ACKED', now())"
            ),
            {
                "id": CREATE_INTEGRATION_EFFECT_ID,
                "effect_key": f"v06-e2e:integration:{work_item_id}:{HEAD_SHA}",
                "subject": f"integration-work-item:{work_item_id}:{HEAD_SHA}",
                "payload": create_integration_payload,
                "work_item_id": work_item_id,
                "requirement_id": requirement_id,
                "repository_id": repository_id,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_binding "
                "(id, kind, work_item_id, requirement_id, workspace_id, repository_id, "
                "branch_binding_id, external_project_id, merge_request_iid, "
                "source_branch, target_branch, create_effect_id, head_sha, "
                "creation_origin) VALUES (:id, 'INTEGRATION', :work_item_id, "
                ":requirement_id, :workspace_id, :repository_id, :branch_binding_id, "
                "'101', 42, :task_branch, 'dev', :effect_id, :head_sha, "
                "'PLATFORM_CREATED')"
            ),
            {
                "id": integration_binding_id,
                "work_item_id": work_item_id,
                "requirement_id": requirement_id,
                "workspace_id": workspace_id,
                "repository_id": repository_id,
                "branch_binding_id": BRANCH_BINDING_ID,
                "task_branch": task_branch,
                "effect_id": CREATE_INTEGRATION_EFFECT_ID,
                "head_sha": HEAD_SHA,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_observation "
                "(id, binding_id, head_sha, state, merge_commit_sha, "
                "external_merge_user_id, merged_at, observation_digest, observed_at) "
                "VALUES (:id, :binding_id, :head_sha, 'MERGED', :merge_sha, '42', "
                "now(), 'sha256:v06-e2e-merged', now())"
            ),
            {
                "id": MERGED_OBSERVATION_ID,
                "binding_id": integration_binding_id,
                "head_sha": HEAD_SHA,
                "merge_sha": INTEGRATION_MERGE_SHA,
            },
        )
    return workspace_id, repository_id, task_branch, integration_binding_id


def _client(
    database: IsolatedRequirementDatabase,
    dependencies: RequirementDependencies,
    capability_calls: list[tuple[str, str | None]],
) -> TestClient:
    holder = PrincipalHolder(Actor("employee-1"))

    def permit(principal: object, capability: str, workspace_id: str | None) -> None:
        del principal
        capability_calls.append((capability, workspace_id))

    app = FastAPI()
    app.include_router(
        create_requirement_v06_delivery_router(
            lambda: RequirementHttpRuntime(
                engine=database.runtime,
                dependencies=dependencies,
            ),
            holder.get,
            permit,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


@dataclass(frozen=True, slots=True)
class V06Scenario:
    requirement_database: IsolatedRequirementDatabase
    source_database: IsolatedSourceControlDatabase
    requirement_id: str
    work_item_id: str
    initial_revision: int
    workspace_id: str
    repository_id: str
    task_branch: str
    integration_binding_id: str
    requirement_dependencies: RequirementDependencies
    source_dependencies: SourceControlDependencies
    gitlab: DynamicFormalGitLab
    capability_calls: list[tuple[str, str | None]]
    client: TestClient

    @property
    def base(self) -> str:
        return f"/api/v1/requirements/{self.requirement_id}"


def _scenario(
    requirement_database: IsolatedRequirementDatabase,
    source_database: IsolatedSourceControlDatabase,
    *,
    key_suffix: str,
) -> V06Scenario:
    requirement_id, work_item_id, revision, _ = _integrated_requirement(
        requirement_database,
        key_suffix=key_suffix,
    )
    workspace_id, repository_id, task_branch, integration_binding_id = _seed_source_control_graph(
        source_database,
        requirement_database,
        requirement_id=requirement_id,
        work_item_id=work_item_id,
    )
    source_evidence_dependencies = evidence_dependencies(source_database)
    requirement_dependencies = replace(
        _gate_dependencies(),
        clock=source_evidence_dependencies.clock,
        integration_evidence=SourceControlFacadeEvidenceAdapter(
            source_database.runtime,
            source_evidence_dependencies,
        ),
        delivery_gate_policies=StaticDeliveryPolicies(),
        delivery_reviewer_guard=StaticDeliveryReviewerGuard(),
    )
    gitlab = DynamicFormalGitLab(
        requirement_id=requirement_id,
        task_branch=task_branch,
    )
    source_dependencies = replace(
        source_evidence_dependencies,
        requirement=RequirementFacadeBindingAdapter(
            requirement_database.runtime,
            requirement_dependencies,
            source_evidence_dependencies.clock,
        ),
        eligibility=EligibleCurrentOwner(),
        requirement_evidence=RequirementFacadeEvidenceAdapter(
            requirement_database.runtime,
            requirement_dependencies,
        ),
        formal_repository_factory=SqlAlchemySourceControlFormalRepository,
        requirement_formal_delivery=RequirementFacadeFormalDeliveryAdapter(
            requirement_database.runtime,
            requirement_dependencies,
        ),
        gitlab_formal_merge_requests=gitlab,
        formal_review_routing=OwnerFormalReviewRouting(),
    )
    capability_calls: list[tuple[str, str | None]] = []
    return V06Scenario(
        requirement_database=requirement_database,
        source_database=source_database,
        requirement_id=requirement_id,
        work_item_id=work_item_id,
        initial_revision=revision,
        workspace_id=workspace_id,
        repository_id=repository_id,
        task_branch=task_branch,
        integration_binding_id=integration_binding_id,
        requirement_dependencies=requirement_dependencies,
        source_dependencies=source_dependencies,
        gitlab=gitlab,
        capability_calls=capability_calls,
        client=_client(
            requirement_database,
            requirement_dependencies,
            capability_calls,
        ),
    )


def _submit_external_validation(
    scenario: V06Scenario,
    *,
    idempotency_key: str,
    if_match: str,
    reference: str = "https://jenkins.example.test/job/platform/42?token=drop#log",
    notes: str = "The exact integrated commit passed manual verification.",
    target_commit_sha: str = HEAD_SHA,
    integration_merge_commit_sha: str = INTEGRATION_MERGE_SHA,
) -> Response:
    return cast(
        Response,
        scenario.client.post(
            f"{scenario.base}/work-items/{scenario.work_item_id}/external-validations",
            json={
                "targetCommitSha": target_commit_sha,
                "integrationMergeCommitSha": integration_merge_commit_sha,
                "reference": reference,
                "notes": notes,
                "artifactReferences": [
                    {
                        "artifactId": "sdd-1",
                        "artifactVersion": "version-1",
                        "artifactHash": ARTIFACT_HASH_V1,
                    }
                ],
            },
            headers={
                **SAME_ORIGIN,
                "Idempotency-Key": idempotency_key,
                "If-Match": if_match,
            },
        ),
    )


def _approve_current_evidence(
    scenario: V06Scenario,
    *,
    key_prefix: str,
    initial_revision: int | None = None,
    outcome: DecisionOutcome = DecisionOutcome.APPROVED,
    reference: str = "https://jenkins.example.test/job/platform/42?token=drop#log",
    target_commit_sha: str = HEAD_SHA,
    integration_merge_commit_sha: str = INTEGRATION_MERGE_SHA,
) -> Response:
    selected_initial_revision = (
        scenario.initial_revision if initial_revision is None else initial_revision
    )
    validation_response = _submit_external_validation(
        scenario,
        idempotency_key=f"{key_prefix}-validation",
        if_match=f'"v{selected_initial_revision}"',
        reference=reference,
        target_commit_sha=target_commit_sha,
        integration_merge_commit_sha=integration_merge_commit_sha,
    )
    assert validation_response.status_code == 200, validation_response.text
    validation = validation_response.json()
    assert "?" not in validation["submission"]["reference"]
    assert "#" not in validation["submission"]["reference"]
    assert (
        relay_requirement_evidence_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )

    baseline_response = scenario.client.post(
        f"{scenario.base}:request-integration-baseline",
        json={"expectedRequirementVersion": validation["requirement"]["requirementVersion"]},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": f"{key_prefix}-baseline",
            "If-Match": validation_response.headers["etag"],
        },
    )
    assert baseline_response.status_code == 202, baseline_response.text
    baseline = baseline_response.json()
    assert (
        relay_requirement_evidence_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )
    with scenario.source_database.runtime.connect() as db:
        evidence_message_id = str(
            db.execute(
                text(
                    "SELECT message_id FROM source_control.evidence_request_inbox "
                    "WHERE delivery_snapshot_id=:snapshot_id"
                ),
                {"snapshot_id": baseline["snapshot"]["id"]},
            ).scalar_one()
        )
    with scenario.source_database.runtime.begin() as db:
        evidence = process_integration_baseline_request(
            db,
            message_id=evidence_message_id,
            generated_by="SYSTEM:SOURCE_CONTROL",
            dependencies=scenario.source_dependencies,
        )

    selection_response = scenario.client.post(
        f"{scenario.base}/integration-baseline-selections",
        json={
            "deliverySnapshotId": baseline["snapshot"]["id"],
            "integrationBaselineId": evidence.id,
            "expectedRequirementVersion": baseline["requirement"]["requirementVersion"],
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": f"{key_prefix}-selection",
            "If-Match": baseline_response.headers["etag"],
        },
    )
    assert selection_response.status_code == 200, selection_response.text
    selection = selection_response.json()
    confirmation_response = scenario.client.post(
        f"{scenario.base}/acceptance-confirmations",
        json={"selectionId": selection["selection"]["id"]},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": f"{key_prefix}-acceptance-open",
            "If-Match": selection_response.headers["etag"],
        },
    )
    assert confirmation_response.status_code == 200, confirmation_response.text
    confirmation = confirmation_response.json()
    acceptance_response = scenario.client.post(
        f"{scenario.base}/acceptance-decisions",
        json={
            "gateId": confirmation["gate"]["id"],
            "outcome": outcome.value,
            "reason": (
                "Every exact evidence item satisfies acceptance."
                if outcome is DecisionOutcome.APPROVED
                else "The accepted subject must return through integration."
            ),
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": f"{key_prefix}-acceptance-decide",
            "If-Match": confirmation_response.headers["etag"],
        },
    )
    assert acceptance_response.status_code == 200, acceptance_response.text
    return cast(Response, acceptance_response)


def _reintegrate_requirement(
    scenario: V06Scenario,
    *,
    binding_id: str,
    key_prefix: str,
) -> int:
    with scenario.requirement_database.runtime.connect() as db:
        current = get_requirement(
            db,
            requirement_id=scenario.requirement_id,
            dependencies=scenario.requirement_dependencies,
        )
    with scenario.requirement_database.runtime.begin() as db:
        requested = request_integration_merge_request(
            db,
            requirement_id=scenario.requirement_id,
            work_item_id=scenario.work_item_id,
            expected_revision=current.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"{key_prefix}-request",
            dependencies=scenario.requirement_dependencies,
        )
    with scenario.requirement_database.runtime.begin() as db:
        ready = record_integration_mr_ready(
            db,
            work_item_id=scenario.work_item_id,
            binding_id=binding_id,
            expected_revision=requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key=f"{key_prefix}-ready",
            correlation_id=f"{key_prefix}-ready",
            dependencies=scenario.requirement_dependencies,
        )
    with scenario.requirement_database.runtime.begin() as db:
        merge_requested = request_integration_merge(
            db,
            requirement_id=scenario.requirement_id,
            work_item_id=scenario.work_item_id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key=f"{key_prefix}-merge",
            dependencies=scenario.requirement_dependencies,
        )
    with scenario.requirement_database.runtime.begin() as db:
        merged = record_integration_merged(
            db,
            work_item_id=scenario.work_item_id,
            binding_id=binding_id,
            expected_revision=merge_requested.work_item.revision,
            actor=Actor("SYSTEM:SOURCE_CONTROL"),
            idempotency_key=f"{key_prefix}-merged",
            correlation_id=f"{key_prefix}-merged",
            dependencies=scenario.requirement_dependencies,
        )
    return merged.requirement.revision


def _append_source_integration_proof(
    scenario: V06Scenario,
    *,
    binding_id: str,
    effect_id: str,
    observation_id: str,
    merge_request_iid: int,
    fingerprint_character: str,
    head_sha: str,
    merge_commit_sha: str,
) -> None:
    payload = json.dumps(
        {"branchBindingId": BRANCH_BINDING_ID, "headSha": head_sha},
        separators=(",", ":"),
        sort_keys=True,
    )
    with scenario.source_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE source_control.merge_request_binding SET superseded_at=now() "
                "WHERE work_item_id=:work_item_id AND kind='INTEGRATION' "
                "AND superseded_at IS NULL"
            ),
            {"work_item_id": scenario.work_item_id},
        )
        db.execute(
            text(
                "INSERT INTO source_control.source_control_effect "
                "(id, effect_key, operation, subject_key, payload, work_item_id, "
                "requirement_id, repository_id, request_fingerprint, attempts, state, "
                "requirement_callback_state, completed_at) VALUES "
                "(:id, :effect_key, 'CREATE_INTEGRATION_MR', :subject_key, "
                "CAST(:payload AS JSONB), :work_item_id, :requirement_id, :repository_id, "
                ":request_fingerprint, 1, 'SUCCEEDED', 'ACKED', now())"
            ),
            {
                "id": effect_id,
                "effect_key": f"v06-e2e:reintegration:{scenario.work_item_id}:{binding_id}",
                "subject_key": f"integration-work-item:{scenario.work_item_id}:{head_sha}",
                "payload": payload,
                "work_item_id": scenario.work_item_id,
                "requirement_id": scenario.requirement_id,
                "repository_id": scenario.repository_id,
                "request_fingerprint": "sha256:" + fingerprint_character * 64,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_binding "
                "(id, kind, work_item_id, requirement_id, workspace_id, repository_id, "
                "branch_binding_id, external_project_id, merge_request_iid, "
                "source_branch, target_branch, create_effect_id, head_sha, "
                "creation_origin) VALUES "
                "(:id, 'INTEGRATION', :work_item_id, :requirement_id, :workspace_id, "
                ":repository_id, :branch_binding_id, '101', :merge_request_iid, "
                ":task_branch, 'dev', :effect_id, :head_sha, 'PLATFORM_CREATED')"
            ),
            {
                "id": binding_id,
                "work_item_id": scenario.work_item_id,
                "requirement_id": scenario.requirement_id,
                "workspace_id": scenario.workspace_id,
                "repository_id": scenario.repository_id,
                "branch_binding_id": BRANCH_BINDING_ID,
                "merge_request_iid": merge_request_iid,
                "task_branch": scenario.task_branch,
                "effect_id": effect_id,
                "head_sha": head_sha,
            },
        )
        db.execute(
            text(
                "INSERT INTO source_control.merge_request_observation "
                "(id, binding_id, head_sha, state, merge_commit_sha, "
                "external_merge_user_id, merged_at, observation_digest, observed_at) "
                "VALUES (:id, :binding_id, :head_sha, 'MERGED', :merge_sha, '42', "
                "now(), :digest, now())"
            ),
            {
                "id": observation_id,
                "binding_id": binding_id,
                "head_sha": head_sha,
                "merge_sha": merge_commit_sha,
                "digest": "sha256:" + fingerprint_character * 64,
            },
        )


def _formal_message_id(
    scenario: V06Scenario,
    *,
    topic: str,
    requested_head_sha: str | None = None,
) -> str:
    head_filter = (
        "" if requested_head_sha is None else " AND requested_head_sha=:requested_head_sha"
    )
    with scenario.source_database.runtime.connect() as db:
        return str(
            db.execute(
                text(
                    "SELECT message_id FROM source_control.formal_delivery_request_inbox "
                    "WHERE work_item_id=:work_item_id AND topic=:topic" + head_filter + " "
                    "ORDER BY received_at DESC, message_id DESC LIMIT 1"
                ),
                {
                    "work_item_id": scenario.work_item_id,
                    "topic": topic,
                    "requested_head_sha": requested_head_sha,
                },
            ).scalar_one()
        )


def _assert_problem(response: Response, *, status_code: int, title: str) -> None:
    assert response.status_code == status_code, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == status_code
    assert response.json()["title"] == title


def test_v06_human_delivery_converges_over_http_and_two_postgres_databases(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="v06-cross-module-e2e",
    )
    acceptance_response = _approve_current_evidence(
        scenario,
        key_prefix="v06-e2e",
    )

    formal_request_response = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-e2e-formal-create",
            "If-Match": acceptance_response.headers["etag"],
        },
    )
    assert formal_request_response.status_code == 202, formal_request_response.text
    assert (
        relay_requirement_formal_delivery_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )
    create_message_id = _formal_message_id(
        scenario,
        topic="requirement.formal-merge-request.requested",
    )
    created = process_formal_delivery_request(
        message_id=create_message_id,
        dependencies=scenario.source_dependencies,
    )
    assert created.effect is not None
    assert created.effect.state is EffectState.SUCCEEDED
    assert created.binding is not None
    assert created.observation is not None
    assert created.observation.state is MergeRequestState.OPEN
    assert created.effect.callback_state.value == "ACKED"

    with scenario.requirement_database.owner.connect() as db:
        formal_gate_id, formal_revision = db.execute(
            text(
                "SELECT gate.id::text, requirement.revision "
                "FROM requirement.delivery_gate AS gate "
                "JOIN requirement.requirement ON requirement.id=gate.requirement_id "
                "WHERE gate.requirement_id=:requirement_id "
                "AND gate.work_item_id=:work_item_id "
                "AND gate.gate_type='FORMAL_MR_REVIEW' AND gate.state='OPEN'"
            ),
            {
                "requirement_id": scenario.requirement_id,
                "work_item_id": scenario.work_item_id,
            },
        ).one()
    review_response = scenario.client.post(
        f"{scenario.base}/formal-review-decisions",
        json={
            "gateId": formal_gate_id,
            "outcome": DecisionOutcome.APPROVED.value,
            "reason": "The exact formal MR head is approved.",
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-e2e-formal-review",
            "If-Match": f'"v{formal_revision}"',
        },
    )
    assert review_response.status_code == 200, review_response.text
    merge_request_response = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-merge",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-e2e-formal-merge",
            "If-Match": review_response.headers["etag"],
        },
    )
    assert merge_request_response.status_code == 202, merge_request_response.text
    assert (
        relay_requirement_formal_delivery_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )
    merge_message_id = _formal_message_id(
        scenario,
        topic="requirement.formal-merge.requested",
    )
    merged = process_formal_delivery_request(
        message_id=merge_message_id,
        dependencies=scenario.source_dependencies,
    )
    assert merged.effect is not None
    assert merged.effect.state is EffectState.SUCCEEDED
    assert merged.observation is not None
    assert merged.observation.state is MergeRequestState.MERGED
    assert merged.observation.merge_commit_sha == FORMAL_MERGE_SHA
    assert merged.effect.callback_state.value == "ACKED"

    with scenario.requirement_database.runtime.connect() as db:
        final = get_requirement(
            db,
            requirement_id=scenario.requirement_id,
            dependencies=scenario.requirement_dependencies,
        )
    assert final.requirement.state is RequirementState.COMPLETED
    assert final.work_items[0].state is WorkItemState.COMPLETED
    assert final.work_items[0].integration_delivery_state.value == "INTEGRATED"
    assert final.work_items[0].formal_delivery_state.value == "MERGED"
    assert final.work_items[0].formal_merge_request_binding_id == created.binding.id
    assert scenario.gitlab.create_calls == 1
    assert scenario.gitlab.merge_calls == 1

    with scenario.source_database.owner.connect() as db:
        bindings = [
            tuple(row)
            for row in db.execute(
                text(
                    "SELECT kind, target_branch FROM source_control.merge_request_binding "
                    "WHERE work_item_id=:work_item_id ORDER BY kind"
                ),
                {"work_item_id": scenario.work_item_id},
            )
        ]
        stored_reference = db.execute(
            text(
                "SELECT reference FROM source_control.external_validation_reference "
                "WHERE work_item_id=:work_item_id"
            ),
            {"work_item_id": scenario.work_item_id},
        ).scalar_one()
    assert bindings == [("FORMAL", "main"), ("INTEGRATION", "dev")]
    assert stored_reference == "https://jenkins.example.test/job/platform/42"
    assert scenario.integration_binding_id != created.binding.id
    assert scenario.capability_calls == [
        ("work_item.validation.submit", scenario.workspace_id),
        ("requirement.evidence.request", scenario.workspace_id),
        ("requirement.evidence.select", scenario.workspace_id),
        ("requirement.acceptance.submit", scenario.workspace_id),
        ("requirement.acceptance.decide", scenario.workspace_id),
        ("formal_merge_request.request", scenario.workspace_id),
        ("merge_request.review", scenario.workspace_id),
        ("merge_request.merge", scenario.workspace_id),
    ]
    assert scenario.repository_id == final.work_items[0].repository_id


def test_repeated_rework_reuses_formal_binding_with_new_acceptance_assignment(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="v06-repeated-rework-e2e",
    )
    first_acceptance = _approve_current_evidence(
        scenario,
        key_prefix="v06-repeated-rework-first",
    )
    first_request = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-repeated-rework-formal-first",
            "If-Match": first_acceptance.headers["etag"],
        },
    )
    assert first_request.status_code == 202, first_request.text
    assert (
        relay_requirement_formal_delivery_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )
    first_message_id = _formal_message_id(
        scenario,
        topic="requirement.formal-merge-request.requested",
    )
    created = process_formal_delivery_request(
        message_id=first_message_id,
        dependencies=scenario.source_dependencies,
    )
    assert created.binding is not None
    assert created.assignment is not None
    assert created.effect is not None

    with scenario.requirement_database.owner.connect() as db:
        first_gate_id, first_review_revision = db.execute(
            text(
                "SELECT gate.id::text, requirement.revision "
                "FROM requirement.delivery_gate AS gate "
                "JOIN requirement.requirement ON requirement.id=gate.requirement_id "
                "WHERE gate.requirement_id=:requirement_id "
                "AND gate.work_item_id=:work_item_id "
                "AND gate.gate_type='FORMAL_MR_REVIEW' AND gate.state='OPEN'"
            ),
            {
                "requirement_id": scenario.requirement_id,
                "work_item_id": scenario.work_item_id,
            },
        ).one()
    negative_review = scenario.client.post(
        f"{scenario.base}/formal-review-decisions",
        json={
            "gateId": first_gate_id,
            "outcome": DecisionOutcome.CHANGES_REQUESTED.value,
            "reason": "The exact formal head needs rework.",
        },
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-repeated-rework-negative-review",
            "If-Match": f'"v{first_review_revision}"',
        },
    )
    assert negative_review.status_code == 200, negative_review.text
    with scenario.requirement_database.runtime.connect() as db:
        after_negative_review = get_requirement(
            db,
            requirement_id=scenario.requirement_id,
            dependencies=scenario.requirement_dependencies,
        )
    assert after_negative_review.requirement.state is RequirementState.IN_PROGRESS
    assert after_negative_review.work_items[0].formal_delivery_state.value == "MR_OPEN"
    assert after_negative_review.work_items[0].formal_merge_request_binding_id == created.binding.id

    scenario.gitlab.branch_head_sha = REWORK_HEAD_SHA_1
    scenario.gitlab.current = scenario.gitlab._snapshot(state="opened")
    first_reintegration_revision = _reintegrate_requirement(
        scenario,
        binding_id=REWORK_INTEGRATION_BINDING_1,
        key_prefix="v06-repeated-rework-integration-one",
    )
    _append_source_integration_proof(
        scenario,
        binding_id=REWORK_INTEGRATION_BINDING_1,
        effect_id=REWORK_INTEGRATION_EFFECT_1,
        observation_id=REWORK_INTEGRATION_OBSERVATION_1,
        merge_request_iid=43,
        fingerprint_character="8",
        head_sha=REWORK_HEAD_SHA_1,
        merge_commit_sha=REWORK_INTEGRATION_MERGE_SHA_1,
    )
    rejected_acceptance = _approve_current_evidence(
        scenario,
        key_prefix="v06-repeated-rework-rejected",
        initial_revision=first_reintegration_revision,
        outcome=DecisionOutcome.REJECTED,
        reference="https://jenkins.example.test/job/platform/43?token=drop#log",
        target_commit_sha=REWORK_HEAD_SHA_1,
        integration_merge_commit_sha=REWORK_INTEGRATION_MERGE_SHA_1,
    )
    assert rejected_acceptance.json()["requirement"]["state"] == "IN_PROGRESS"
    with scenario.requirement_database.runtime.connect() as db:
        after_rejected_acceptance = get_requirement(
            db,
            requirement_id=scenario.requirement_id,
            dependencies=scenario.requirement_dependencies,
        )
    assert after_rejected_acceptance.work_items[0].formal_delivery_state.value == "MR_OPEN"
    assert (
        after_rejected_acceptance.work_items[0].formal_merge_request_binding_id
        == created.binding.id
    )

    scenario.gitlab.branch_head_sha = REWORK_HEAD_SHA_2
    scenario.gitlab.current = scenario.gitlab._snapshot(state="opened")
    second_reintegration_revision = _reintegrate_requirement(
        scenario,
        binding_id=REWORK_INTEGRATION_BINDING_2,
        key_prefix="v06-repeated-rework-integration-two",
    )
    _append_source_integration_proof(
        scenario,
        binding_id=REWORK_INTEGRATION_BINDING_2,
        effect_id=REWORK_INTEGRATION_EFFECT_2,
        observation_id=REWORK_INTEGRATION_OBSERVATION_2,
        merge_request_iid=44,
        fingerprint_character="9",
        head_sha=REWORK_HEAD_SHA_2,
        merge_commit_sha=REWORK_INTEGRATION_MERGE_SHA_2,
    )
    third_acceptance = _approve_current_evidence(
        scenario,
        key_prefix="v06-repeated-rework-third",
        initial_revision=second_reintegration_revision,
        reference="https://jenkins.example.test/job/platform/44?token=drop#log",
        target_commit_sha=REWORK_HEAD_SHA_2,
        integration_merge_commit_sha=REWORK_INTEGRATION_MERGE_SHA_2,
    )
    third_acceptance_id = third_acceptance.json()["decision"]["id"]
    refreshed_request = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-repeated-rework-formal-third",
            "If-Match": third_acceptance.headers["etag"],
        },
    )
    assert refreshed_request.status_code == 202, refreshed_request.text
    assert (
        relay_requirement_formal_delivery_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )
    refreshed_message_id = _formal_message_id(
        scenario,
        topic="requirement.formal-merge-request.requested",
        requested_head_sha=REWORK_HEAD_SHA_2,
    )
    refreshed = process_formal_delivery_request(
        message_id=refreshed_message_id,
        dependencies=scenario.source_dependencies,
    )

    assert refreshed.binding is not None
    assert refreshed.assignment is not None
    assert refreshed.effect is not None
    assert refreshed.binding.id == created.binding.id
    assert refreshed.effect.id != created.effect.id
    assert refreshed.assignment.id != created.assignment.id
    assert refreshed.assignment.acceptance_decision_id == third_acceptance_id
    assert scenario.gitlab.create_calls == 1
    with scenario.source_database.owner.connect() as db:
        assignments = db.execute(
            text(
                "SELECT acceptance_decision_id::text, superseded_at IS NULL "
                "FROM source_control.formal_review_assignment "
                "WHERE binding_id=:binding_id ORDER BY revision"
            ),
            {"binding_id": created.binding.id},
        ).all()
        formal_counts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM source_control.merge_request_binding "
                "WHERE work_item_id=:work_item_id AND kind='FORMAL'), "
                "(SELECT count(*) FROM source_control.source_control_effect "
                "WHERE work_item_id=:work_item_id AND operation='CREATE_FORMAL_MR')"
            ),
            {"work_item_id": scenario.work_item_id},
        ).one()
    assert [tuple(row) for row in assignments] == [
        (created.assignment.acceptance_decision_id, False),
        (third_acceptance_id, True),
    ]
    assert tuple(formal_counts) == (1, 2)


def test_changed_validation_makes_accepted_evidence_stale_without_formal_side_effects(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="v06-stale-evidence-e2e",
    )
    acceptance_response = _approve_current_evidence(
        scenario,
        key_prefix="v06-stale-evidence",
    )

    changed_validation = _submit_external_validation(
        scenario,
        idempotency_key="v06-stale-evidence-changed-validation",
        if_match=acceptance_response.headers["etag"],
        reference="https://jenkins.example.test/job/platform/43?token=drop#log",
        notes="A newer exact validation supersedes the accepted evidence.",
    )
    assert changed_validation.status_code == 200, changed_validation.text
    assert changed_validation.json()["requirement"]["state"] == "VERIFYING"

    rejected = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-stale-evidence-formal-create",
            "If-Match": changed_validation.headers["etag"],
        },
    )
    _assert_problem(
        cast(Response, rejected),
        status_code=409,
        title="Requirement acceptance stale",
    )

    with scenario.requirement_database.owner.connect() as db:
        requirement_state = db.execute(
            text(
                "SELECT requirement.state, "
                "requirement.current_integration_baseline_selection_id, "
                "requirement.current_acceptance_gate_id, work_item.formal_delivery_state "
                "FROM requirement.requirement JOIN requirement.work_item "
                "ON work_item.requirement_id=requirement.id "
                "WHERE requirement.id=:requirement_id AND work_item.id=:work_item_id"
            ),
            {
                "requirement_id": scenario.requirement_id,
                "work_item_id": scenario.work_item_id,
            },
        ).one()
        formal_outbox_count = db.execute(
            text(
                "SELECT count(*) FROM requirement.outbox_message "
                "WHERE aggregate_id=:requirement_id "
                "AND topic='requirement.formal-merge-request.requested'"
            ),
            {"requirement_id": scenario.requirement_id},
        ).scalar_one()
    with scenario.source_database.owner.connect() as db:
        formal_facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM source_control.source_control_effect "
                " WHERE work_item_id=:work_item_id "
                " AND operation IN ('CREATE_FORMAL_MR', 'MERGE_FORMAL_MR')), "
                "(SELECT count(*) FROM source_control.merge_request_binding "
                " WHERE work_item_id=:work_item_id AND kind='FORMAL')"
            ),
            {"work_item_id": scenario.work_item_id},
        ).one()

    assert tuple(requirement_state) == ("VERIFYING", None, None, "NOT_STARTED")
    assert formal_outbox_count == 0
    assert tuple(formal_facts) == (0, 0)
    assert scenario.gitlab.create_calls == 0
    assert scenario.gitlab.merge_calls == 0


def test_replayed_formal_request_has_one_outbox_effect_binding_and_provider_call(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="v06-replay-e2e",
    )
    acceptance_response = _approve_current_evidence(
        scenario,
        key_prefix="v06-replay",
    )
    request_headers = {
        **SAME_ORIGIN,
        "Idempotency-Key": "v06-replay-formal-create",
        "If-Match": acceptance_response.headers["etag"],
    }

    first = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers=request_headers,
    )
    replay = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers=request_headers,
    )
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"]

    conflicting_replay = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-replay-formal-create",
            "If-Match": first.headers["etag"],
        },
    )
    _assert_problem(
        cast(Response, conflicting_replay),
        status_code=409,
        title="Idempotency conflict",
    )

    with scenario.requirement_database.owner.connect() as db:
        outbox_count = db.execute(
            text(
                "SELECT count(*) FROM requirement.outbox_message "
                "WHERE aggregate_id=:requirement_id "
                "AND topic='requirement.formal-merge-request.requested'"
            ),
            {"requirement_id": scenario.requirement_id},
        ).scalar_one()
    assert outbox_count == 1
    assert (
        relay_requirement_formal_delivery_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )
    assert (
        relay_requirement_formal_delivery_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 0
    )
    message_id = _formal_message_id(
        scenario,
        topic="requirement.formal-merge-request.requested",
    )
    created = process_formal_delivery_request(
        message_id=message_id,
        dependencies=scenario.source_dependencies,
    )
    replayed = process_formal_delivery_request(
        message_id=message_id,
        dependencies=scenario.source_dependencies,
    )
    assert created.effect is not None
    assert replayed.effect is not None
    assert created.effect.id == replayed.effect.id
    assert created.effect.state is replayed.effect.state is EffectState.SUCCEEDED

    with scenario.source_database.owner.connect() as db:
        source_counts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM source_control.formal_delivery_request_inbox "
                " WHERE work_item_id=:work_item_id "
                " AND topic='requirement.formal-merge-request.requested'), "
                "(SELECT count(*) FROM source_control.source_control_effect "
                " WHERE work_item_id=:work_item_id AND operation='CREATE_FORMAL_MR'), "
                "(SELECT count(*) FROM source_control.merge_request_binding "
                " WHERE work_item_id=:work_item_id AND kind='FORMAL'), "
                "(SELECT count(*) FROM source_control.merge_request_observation AS observation "
                " JOIN source_control.merge_request_binding AS binding "
                " ON binding.id=observation.binding_id "
                " WHERE binding.work_item_id=:work_item_id AND binding.kind='FORMAL')"
            ),
            {"work_item_id": scenario.work_item_id},
        ).one()
    assert tuple(source_counts) == (1, 1, 1, 1)
    assert scenario.gitlab.create_calls == 1
    assert scenario.gitlab.merge_calls == 0


def test_stale_provider_head_persists_terminal_effect_without_provider_write(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="v06-stale-head-e2e",
    )
    acceptance_response = _approve_current_evidence(
        scenario,
        key_prefix="v06-stale-head",
    )
    requested = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-stale-head-formal-create",
            "If-Match": acceptance_response.headers["etag"],
        },
    )
    assert requested.status_code == 202, requested.text
    assert (
        relay_requirement_formal_delivery_requests(
            limit=1,
            dependencies=scenario.source_dependencies,
        ).accepted
        == 1
    )
    message_id = _formal_message_id(
        scenario,
        topic="requirement.formal-merge-request.requested",
    )
    scenario.gitlab.branch_head_sha = STALE_HEAD_SHA

    blocked = process_formal_delivery_request(
        message_id=message_id,
        dependencies=scenario.source_dependencies,
    )
    assert blocked.effect is not None
    assert blocked.effect.state is EffectState.BLOCKED
    assert blocked.effect.callback_state.value == "ACKED"
    assert blocked.blocked_reason == "HEAD_SHA_CHANGED"

    rejected_retry = scenario.client.post(
        f"{scenario.base}/work-items/{scenario.work_item_id}:request-formal-mr",
        json={},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "v06-stale-head-formal-create-retry",
            "If-Match": requested.headers["etag"],
        },
    )
    _assert_problem(
        cast(Response, rejected_retry),
        status_code=409,
        title="Requirement snapshot conflict",
    )

    with scenario.requirement_database.owner.connect() as db:
        outbox_count = db.execute(
            text(
                "SELECT count(*) FROM requirement.outbox_message "
                "WHERE aggregate_id=:requirement_id "
                "AND topic='requirement.formal-merge-request.requested'"
            ),
            {"requirement_id": scenario.requirement_id},
        ).scalar_one()
    with scenario.source_database.owner.connect() as db:
        formal_facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM source_control.source_control_effect "
                " WHERE work_item_id=:work_item_id AND operation='CREATE_FORMAL_MR'), "
                "(SELECT count(*) FROM source_control.merge_request_binding "
                " WHERE work_item_id=:work_item_id AND kind='FORMAL'), "
                "(SELECT count(*) FROM source_control.merge_request_observation AS observation "
                " JOIN source_control.merge_request_binding AS binding "
                " ON binding.id=observation.binding_id "
                " WHERE binding.work_item_id=:work_item_id AND binding.kind='FORMAL')"
            ),
            {"work_item_id": scenario.work_item_id},
        ).one()

    assert outbox_count == 1
    assert tuple(formal_facts) == (1, 0, 0)
    assert scenario.gitlab.create_calls == 0
    assert scenario.gitlab.merge_calls == 0
