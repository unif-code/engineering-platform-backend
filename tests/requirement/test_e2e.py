from dataclasses import replace
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from control_plane.app.modules.audit.adapters.sqlalchemy_repository import (
    SqlAlchemyAuditEventRepository,
)
from control_plane.app.modules.requirement import (
    DecisionOutcome,
    RepositoryBindingBlockedReason,
    RequirementDependencies,
    RequirementState,
    record_repository_binding,
    record_repository_binding_blocked,
    start_requirement_preparation,
)
from control_plane.app.modules.requirement.adapters import SqlAlchemySddArtifactReader
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_api import SAME_ORIGIN, PrincipalHolder, _client, _create_via_api
from tests.requirement.test_baseline_gate import _gate_dependencies
from tests.requirement.test_commands import Actor


def _versioned_headers(key: str, etag: str, *, request_id: str) -> dict[str, str]:
    return {
        **SAME_ORIGIN,
        "Idempotency-Key": key,
        "If-Match": etag,
        "X-Request-ID": request_id,
    }


def _prepare(
    client: TestClient,
    holder: PrincipalHolder,
    database: IsolatedRequirementDatabase,
    dependencies: RequirementDependencies,
    *,
    key: str,
) -> tuple[str, str, str]:
    created = _create_via_api(client, key=f"{key}-create")
    requirement_id = str(created["requirement"]["id"])
    work_item_id = str(created["workItem"]["id"])
    assert created["requirement"]["state"] == RequirementState.CREATED.value
    assert created["workItem"]["state"] == "DRAFT"
    assert created["workItem"]["repositoryState"] == "WAITING_REPOSITORY"
    with database.runtime.begin() as db:
        prepared = start_requirement_preparation(
            db,
            requirement_id=requirement_id,
            expected_revision=1,
            actor=holder.value,
            idempotency_key=f"{key}-prepare",
            dependencies=dependencies,
        )
    return requirement_id, work_item_id, f'"v{prepared.revision}"'


def _submit_gate(
    client: TestClient,
    requirement_id: str,
    etag: str,
    *,
    key: str,
) -> tuple[str, str]:
    registered = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-baselines",
        json={"artifactId": f"{key}-sdd", "artifactVersion": "version-1"},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": f"{key}-register",
            "If-Match": etag,
        },
    )
    assert registered.status_code == 201, registered.text
    confirmed = client.post(
        f"/api/v1/requirements/{requirement_id}/baseline-confirmations",
        json={"sddBaselineId": registered.json()["baseline"]["id"]},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": f"{key}-confirm",
            "If-Match": registered.headers["etag"],
        },
    )
    assert confirmed.status_code == 201, confirmed.text
    return str(confirmed.json()["gate"]["id"]), str(confirmed.headers["etag"])


def _decision(
    client: TestClient,
    requirement_id: str,
    gate_id: str,
    etag: str,
    *,
    key: str,
    outcome: DecisionOutcome,
    request_id: str | None = None,
) -> Response:
    headers = {
        **SAME_ORIGIN,
        "Idempotency-Key": f"{key}-decide",
        "If-Match": etag,
    }
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return cast(
        Response,
        client.post(
            f"/api/v1/requirements/{requirement_id}/baseline-decisions",
            json={
                "gateId": gate_id,
                "outcome": outcome.value,
                "reason": f"E2E decision: {outcome.value}",
            },
            headers=headers,
        ),
    )


def test_http_and_internal_worker_complete_the_real_postgresql_approval_path(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    dependencies = _gate_dependencies()
    client, holder, _guard, _resolved = _client(
        isolated_requirement_database,
        dependencies=dependencies,
    )
    requirement_id, work_item_id, prepared_etag = _prepare(
        client,
        holder,
        isolated_requirement_database,
        dependencies,
        key="e2e-approved",
    )
    with isolated_requirement_database.runtime.begin() as db:
        blocked = record_repository_binding_blocked(
            db,
            work_item_id=work_item_id,
            repository_id="repository-1",
            reason_code=RepositoryBindingBlockedReason.CONNECTOR_UNAVAILABLE,
            expected_revision=1,
            actor=Actor("SYSTEM"),
            idempotency_key="e2e-approved-binding-blocked",
            correlation_id=f"source-control:work-item:{work_item_id}",
            dependencies=dependencies,
        )
    blocked_details = client.get(f"/api/v1/requirements/{requirement_id}")
    assert blocked_details.status_code == 200
    assert blocked_details.json()["workItems"][0]["repositoryState"] == "BLOCKED"
    assert blocked_details.json()["workItems"][0]["repositoryBlockedReasonCode"] == (
        RepositoryBindingBlockedReason.CONNECTOR_UNAVAILABLE.value
    )
    with isolated_requirement_database.runtime.begin() as db:
        bound = record_repository_binding(
            db,
            work_item_id=work_item_id,
            repository_id="repository-1",
            base_commit_sha="d" * 40,
            task_branch="work-items/e2e-approved",
            expected_revision=blocked.revision,
            actor=Actor("SYSTEM"),
            idempotency_key="e2e-approved-binding-ready",
            correlation_id="source-control:effect:e2e-approved-binding-ready",
            dependencies=dependencies,
        )
    assert bound.state.value == "DRAFT"
    gate_id, gate_etag = _submit_gate(
        client,
        requirement_id,
        prepared_etag,
        key="e2e-approved",
    )
    holder.value = Actor("reviewer-1")

    decided = _decision(
        client,
        requirement_id,
        gate_id,
        gate_etag,
        key="e2e-approved",
        outcome=DecisionOutcome.APPROVED,
    )
    details = client.get(f"/api/v1/requirements/{requirement_id}")

    assert decided.status_code == 200, decided.text
    assert decided.json()["requirement"]["state"] == RequirementState.READY.value
    assert details.status_code == 200
    assert details.json()["requirement"]["state"] == RequirementState.READY.value
    assert len(details.json()["workItems"]) == 1
    work_item = details.json()["workItems"][0]
    assert work_item["id"] == work_item_id
    assert (
        work_item["state"],
        work_item["assignmentState"],
        work_item["repositoryState"],
        work_item["repositoryBlockedReasonCode"],
    ) == ("READY", "ASSIGNED", "BOUND", None)
    with isolated_requirement_database.owner.connect() as db:
        facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM requirement.decision WHERE gate_instance_id=:gate_id), "
                "(SELECT count(*) FROM requirement.outbox_message "
                "WHERE aggregate_id=:requirement_id AND state='PENDING')"
            ),
            {"gate_id": gate_id, "requirement_id": requirement_id},
        ).one()
    assert facts == (1, 1)


def test_v04_http_journey_preserves_versions_and_readies_only_bound_work_items(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    dependencies = replace(
        _gate_dependencies(),
        artifacts=SqlAlchemySddArtifactReader(isolated_requirement_database.runtime),
    )
    client, holder, _guard, _resolved = _client(
        isolated_requirement_database,
        dependencies=dependencies,
    )
    requirement_id, initial_work_item_id, prepared_etag = _prepare(
        client,
        holder,
        isolated_requirement_database,
        dependencies,
        key="e2e-v04",
    )
    with isolated_requirement_database.runtime.begin() as db:
        initial_bound = record_repository_binding(
            db,
            work_item_id=initial_work_item_id,
            repository_id="repository-1",
            base_commit_sha="e" * 40,
            task_branch="work-items/e2e-v04-initial",
            expected_revision=1,
            actor=Actor("SYSTEM"),
            idempotency_key="e2e-v04-initial-binding",
            correlation_id="source-control:effect:e2e-v04-initial",
            dependencies=dependencies,
        )
    assert initial_bound.state.value == "DRAFT"

    artifact_v1 = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-artifacts",
        json={"content": "# SDD v1\r\n\r\nFirst review."},
        headers=_versioned_headers(
            "e2e-v04-artifact-v1",
            prepared_etag,
            request_id="req-e2ev04artifactv1",
        ),
    )
    artifact_v1_replay = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-artifacts",
        json={"content": "# SDD v1\r\n\r\nFirst review."},
        headers=_versioned_headers(
            "e2e-v04-artifact-v1",
            prepared_etag,
            request_id="req-e2ev04artifactv1replay",
        ),
    )
    assert artifact_v1.status_code == artifact_v1_replay.status_code == 201
    assert artifact_v1_replay.json() == artifact_v1.json()
    artifact_id = artifact_v1.json()["artifact"]["artifactId"]

    added = client.post(
        f"/api/v1/requirements/{requirement_id}/work-items",
        json={"repositoryId": "repository-2"},
        headers=_versioned_headers(
            "e2e-v04-add-work-item",
            artifact_v1.headers["etag"],
            request_id="req-e2ev04workitemadd",
        ),
    )
    assert added.status_code == 201, added.text
    second_work_item_id = added.json()["workItem"]["id"]
    assigned = client.post(
        f"/api/v1/requirements/{requirement_id}/work-items/{second_work_item_id}:assign",
        json={"humanOwnerId": "employee-2", "reason": "Split the backend implementation."},
        headers=_versioned_headers(
            "e2e-v04-assign-work-item",
            '"v1"',
            request_id="req-e2ev04workitemassign",
        ),
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["workItem"]["humanOwnerId"] == "employee-2"

    baseline_v1 = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-baselines",
        json={"artifactId": artifact_id, "artifactVersion": "1"},
        headers=_versioned_headers(
            "e2e-v04-baseline-v1",
            added.headers["etag"],
            request_id="req-e2ev04baselinev1",
        ),
    )
    assert baseline_v1.status_code == 201, baseline_v1.text
    gate_v1 = client.post(
        f"/api/v1/requirements/{requirement_id}/baseline-confirmations",
        json={"sddBaselineId": baseline_v1.json()["baseline"]["id"]},
        headers=_versioned_headers(
            "e2e-v04-gate-v1",
            baseline_v1.headers["etag"],
            request_id="req-e2ev04gatev1",
        ),
    )
    assert gate_v1.status_code == 201, gate_v1.text
    holder.value = Actor("reviewer-1")
    changes_requested = _decision(
        client,
        requirement_id,
        gate_v1.json()["gate"]["id"],
        gate_v1.headers["etag"],
        key="e2e-v04-v1",
        outcome=DecisionOutcome.CHANGES_REQUESTED,
        request_id="req-e2ev04changesrequested",
    )
    assert changes_requested.status_code == 200, changes_requested.text
    assert changes_requested.json()["requirement"]["state"] == "PREPARING"

    holder.value = Actor("employee-1")
    artifact_v2 = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-artifacts",
        json={"artifactId": artifact_id, "content": "# SDD v2\n\nReview feedback fixed.\n"},
        headers=_versioned_headers(
            "e2e-v04-artifact-v2",
            changes_requested.headers["etag"],
            request_id="req-e2ev04artifactv2",
        ),
    )
    assert artifact_v2.status_code == 201, artifact_v2.text
    assert artifact_v2.json()["artifact"]["version"] == 2
    baseline_v2 = client.post(
        f"/api/v1/requirements/{requirement_id}/sdd-baselines",
        json={"artifactId": artifact_id, "artifactVersion": "2"},
        headers=_versioned_headers(
            "e2e-v04-baseline-v2",
            artifact_v2.headers["etag"],
            request_id="req-e2ev04baselinev2",
        ),
    )
    assert baseline_v2.status_code == 201, baseline_v2.text
    gate_v2 = client.post(
        f"/api/v1/requirements/{requirement_id}/baseline-confirmations",
        json={"sddBaselineId": baseline_v2.json()["baseline"]["id"]},
        headers=_versioned_headers(
            "e2e-v04-gate-v2",
            baseline_v2.headers["etag"],
            request_id="req-e2ev04gatev2",
        ),
    )
    assert gate_v2.status_code == 201, gate_v2.text

    holder.value = Actor("reviewer-1")
    reassigned = client.post(
        f"/api/v1/requirements/{requirement_id}/baseline-gates/"
        f"{gate_v2.json()['gate']['id']}:reassign",
        json={"reviewerId": "reviewer-2", "reason": "Use the second qualified reviewer."},
        headers=_versioned_headers(
            "e2e-v04-gate-reassign",
            '"v1"',
            request_id="req-e2ev04gatereassign",
        ),
    )
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.headers["etag"] == '"v2"'
    stale_reassignment = client.post(
        f"/api/v1/requirements/{requirement_id}/baseline-gates/"
        f"{gate_v2.json()['gate']['id']}:reassign",
        json={"reviewerId": "reviewer-3", "reason": "A stale overwrite must lose."},
        headers=_versioned_headers(
            "e2e-v04-gate-reassign-stale",
            '"v1"',
            request_id="req-e2ev04gatereassignstale",
        ),
    )
    assert stale_reassignment.status_code == 409

    holder.value = Actor("reviewer-2")
    approved = _decision(
        client,
        requirement_id,
        gate_v2.json()["gate"]["id"],
        gate_v2.headers["etag"],
        key="e2e-v04-v2",
        outcome=DecisionOutcome.APPROVED,
        request_id="req-e2ev04approved",
    )
    assert approved.status_code == 200, approved.text

    details = client.get(f"/api/v1/requirements/{requirement_id}")
    assert details.status_code == 200
    assert details.json()["requirement"]["state"] == "READY"
    items = {item["id"]: item for item in details.json()["workItems"]}
    assert items[initial_work_item_id]["state"] == "READY"
    assert items[initial_work_item_id]["repositoryState"] == "BOUND"
    assert items[second_work_item_id]["state"] == "DRAFT"
    assert items[second_work_item_id]["repositoryState"] == "WAITING_REPOSITORY"
    assert details.json()["currentSddBaseline"]["artifactVersion"] == "2"
    assert details.json()["currentGate"]["id"] == gate_v2.json()["gate"]["id"]
    assert details.json()["currentGateAssignment"]["currentReviewerId"] == "reviewer-2"
    assert details.json()["currentDecision"]["outcome"] == "APPROVED"

    with isolated_requirement_database.owner.connect() as db:
        immutable_facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM requirement.sdd_artifact_version "
                "WHERE requirement_id=:requirement_id), "
                "(SELECT count(*) FROM requirement.sdd_baseline "
                "WHERE requirement_id=:requirement_id), "
                "(SELECT count(*) FROM requirement.gate_instance "
                "WHERE requirement_id=:requirement_id), "
                "(SELECT count(*) FROM requirement.decision AS decision "
                "JOIN requirement.gate_instance AS gate "
                "ON gate.id=decision.gate_instance_id "
                "WHERE gate.requirement_id=:requirement_id), "
                "(SELECT string_agg(decision.outcome, ',' ORDER BY gate.artifact_version::integer) "
                "FROM requirement.decision AS decision "
                "JOIN requirement.gate_instance AS gate "
                "ON gate.id=decision.gate_instance_id "
                "WHERE gate.requirement_id=:requirement_id)"
            ),
            {"requirement_id": requirement_id},
        ).one()
        correlated_actions = db.execute(
            text(
                "SELECT request_id, action FROM audit.audit_event "
                "WHERE request_id = ANY(:request_ids) ORDER BY request_id, action"
            ),
            {
                "request_ids": [
                    "req-e2ev04artifactv1",
                    "req-e2ev04workitemadd",
                    "req-e2ev04workitemassign",
                    "req-e2ev04changesrequested",
                    "req-e2ev04gatereassign",
                    "req-e2ev04approved",
                ]
            },
        ).all()
    assert immutable_facts == (2, 2, 2, 2, "CHANGES_REQUESTED,APPROVED")
    assert {str(row.request_id) for row in correlated_actions} == {
        "req-e2ev04artifactv1",
        "req-e2ev04workitemadd",
        "req-e2ev04workitemassign",
        "req-e2ev04changesrequested",
        "req-e2ev04gatereassign",
        "req-e2ev04approved",
    }


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        (DecisionOutcome.CHANGES_REQUESTED, RequirementState.PREPARING),
        (DecisionOutcome.REJECTED, RequirementState.CANCELED),
    ],
)
def test_human_decision_outcomes_preserve_the_governed_state_machine(
    isolated_requirement_database: IsolatedRequirementDatabase,
    outcome: DecisionOutcome,
    expected_state: RequirementState,
) -> None:
    dependencies = _gate_dependencies()
    client, holder, _guard, _resolved = _client(
        isolated_requirement_database,
        dependencies=dependencies,
    )
    key = f"e2e-{outcome.value.lower()}"
    requirement_id, _work_item_id, prepared_etag = _prepare(
        client,
        holder,
        isolated_requirement_database,
        dependencies,
        key=key,
    )
    gate_id, gate_etag = _submit_gate(client, requirement_id, prepared_etag, key=key)
    holder.value = Actor("reviewer-1")

    response = _decision(
        client,
        requirement_id,
        gate_id,
        gate_etag,
        key=key,
        outcome=outcome,
    )

    assert response.status_code == 200, response.text
    assert response.json()["requirement"]["state"] == expected_state.value
    assert response.json()["gate"]["state"] == "DECIDED"


def test_reviewer_eligibility_loss_fails_closed_and_is_durably_audited(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    dependencies = replace(
        _gate_dependencies(reviewer_allowed=False),
        denial_audit=SqlAlchemyAuditEventRepository(isolated_requirement_database.owner),
    )
    client, holder, _guard, _resolved = _client(
        isolated_requirement_database,
        dependencies=dependencies,
    )
    requirement_id, _work_item_id, prepared_etag = _prepare(
        client,
        holder,
        isolated_requirement_database,
        dependencies,
        key="e2e-ineligible",
    )
    gate_id, gate_etag = _submit_gate(
        client,
        requirement_id,
        prepared_etag,
        key="e2e-ineligible",
    )
    holder.value = Actor("reviewer-1")

    denied = _decision(
        client,
        requirement_id,
        gate_id,
        gate_etag,
        key="e2e-ineligible",
        outcome=DecisionOutcome.APPROVED,
        request_id="req-e2eineligible",
    )

    assert denied.status_code == 403
    assert denied.json()["title"] == "Baseline reviewer denied"
    with isolated_requirement_database.owner.connect() as db:
        facts = db.execute(
            text(
                "SELECT "
                "(SELECT state FROM requirement.requirement WHERE id=:requirement_id), "
                "(SELECT state FROM requirement.gate_instance WHERE id=:gate_id), "
                "(SELECT count(*) FROM requirement.decision WHERE gate_instance_id=:gate_id), "
                "(SELECT count(*) FROM audit.audit_event "
                "WHERE request_id='req-e2eineligible' "
                "AND action='requirement.baseline_confirmation.decide_denied')"
            ),
            {"requirement_id": requirement_id, "gate_id": gate_id},
        ).one()
    assert facts == ("AWAITING_CONFIRMATION", "OPEN", 0, 1)


def test_stale_revision_and_tampered_subject_hash_cannot_create_a_decision(
    isolated_requirement_database: IsolatedRequirementDatabase,
) -> None:
    dependencies = replace(
        _gate_dependencies(),
        denial_audit=SqlAlchemyAuditEventRepository(isolated_requirement_database.owner),
    )
    client, holder, _guard, _resolved = _client(
        isolated_requirement_database,
        dependencies=dependencies,
    )
    requirement_id, _work_item_id, prepared_etag = _prepare(
        client,
        holder,
        isolated_requirement_database,
        dependencies,
        key="e2e-stale-subject",
    )
    gate_id, gate_etag = _submit_gate(
        client,
        requirement_id,
        prepared_etag,
        key="e2e-stale-subject",
    )
    holder.value = Actor("reviewer-1")

    stale = _decision(
        client,
        requirement_id,
        gate_id,
        '"v3"',
        key="e2e-stale-revision",
        outcome=DecisionOutcome.APPROVED,
        request_id="req-e2estalerevision",
    )
    with isolated_requirement_database.owner.begin() as db:
        db.execute(
            text(
                "UPDATE requirement.requirement SET route_snapshot_hash='sha256:tampered' "
                "WHERE id=:requirement_id"
            ),
            {"requirement_id": requirement_id},
        )
    tampered = _decision(
        client,
        requirement_id,
        gate_id,
        gate_etag,
        key="e2e-tampered-subject",
        outcome=DecisionOutcome.APPROVED,
        request_id="req-e2etamperedsubject",
    )

    assert stale.status_code == tampered.status_code == 409
    with isolated_requirement_database.owner.connect() as db:
        facts = db.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM requirement.decision WHERE gate_instance_id=:gate_id), "
                "(SELECT count(*) FROM audit.audit_event WHERE request_id = ANY(:request_ids) "
                "AND action='requirement.baseline_confirmation.decide_denied')"
            ),
            {
                "gate_id": gate_id,
                "request_ids": ["req-e2estalerevision", "req-e2etamperedsubject"],
            },
        ).one()
    assert facts == (0, 2)
