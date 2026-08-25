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
    RequirementDependencies,
    RequirementState,
    start_requirement_preparation,
)
from tests.requirement.conftest import IsolatedRequirementDatabase
from tests.requirement.test_api import SAME_ORIGIN, PrincipalHolder, _client, _create_via_api
from tests.requirement.test_baseline_gate import _gate_dependencies
from tests.requirement.test_commands import Actor


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
    ) == ("DRAFT", "ASSIGNED", "WAITING_REPOSITORY")
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
