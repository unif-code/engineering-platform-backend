from sqlalchemy import text

from control_plane.app.modules.requirement import get_requirement_delivery_snapshot
from control_plane.app.modules.source_control import (
    process_integration_baseline_request,
    relay_requirement_evidence_requests,
)
from tests.requirement.conftest import (
    IsolatedRequirementDatabase,
    isolated_requirement_database,
    requirement_owner_engine,
)
from tests.requirement.test_api import SAME_ORIGIN
from tests.source_control.conftest import IsolatedSourceControlDatabase
from tests.source_control.test_v06_e2e import _scenario, _submit_external_validation

assert isolated_requirement_database and requirement_owner_engine


def test_current_query_freeze_and_source_evidence_preserve_exact_set_version_and_hash(
    isolated_requirement_database: IsolatedRequirementDatabase,
    isolated_source_control_database: IsolatedSourceControlDatabase,
) -> None:
    scenario = _scenario(
        isolated_requirement_database,
        isolated_source_control_database,
        key_suffix="freeze-consistency",
    )
    validation = _submit_external_validation(
        scenario, idempotency_key="freeze-validation", if_match=f'"v{scenario.initial_revision}"'
    )
    assert validation.status_code == 200
    assert (
        relay_requirement_evidence_requests(
            limit=1, dependencies=scenario.source_dependencies
        ).accepted
        == 1
    )
    with scenario.requirement_database.runtime.connect() as db:
        current = get_requirement_delivery_snapshot(
            db,
            requirement_id=scenario.requirement_id,
            dependencies=scenario.requirement_dependencies,
        )
    response = scenario.client.post(
        f"{scenario.base}:request-integration-baseline",
        json={"expectedRequirementVersion": current.requirement_version},
        headers={
            **SAME_ORIGIN,
            "Idempotency-Key": "freeze-snapshot",
            "If-Match": validation.headers["etag"],
        },
    )
    assert response.status_code == 202, response.text
    frozen = response.json()["snapshot"]
    assert frozen["workItemIds"] == [scenario.work_item_id] == list(current.work_item_ids)
    assert frozen["requirementVersion"] == current.requirement_version
    assert frozen["requiredWorkItemSetVersion"] == current.required_work_item_set_version
    assert frozen["requiredWorkItemSetHash"] == current.required_work_item_set_hash
    assert (
        relay_requirement_evidence_requests(
            limit=1, dependencies=scenario.source_dependencies
        ).accepted
        == 1
    )
    with scenario.source_database.runtime.begin() as db:
        message_id = str(
            db.execute(
                text(
                    "SELECT message_id FROM source_control.evidence_request_inbox "
                    "WHERE delivery_snapshot_id=:id"
                ),
                {"id": frozen["id"]},
            ).scalar_one()
        )
        evidence = process_integration_baseline_request(
            db,
            message_id=message_id,
            generated_by="SYSTEM:SOURCE_CONTROL",
            dependencies=scenario.source_dependencies,
        )
    assert evidence.delivery_snapshot_id == frozen["id"]
    assert evidence.delivery_snapshot_hash == frozen["snapshotHash"]
    assert evidence.requirement_version == current.requirement_version
    assert evidence.required_work_item_set_version == current.required_work_item_set_version
    assert evidence.required_work_item_set_hash == current.required_work_item_set_hash
    assert tuple(item.work_item_id for item in evidence.items) == current.work_item_ids
