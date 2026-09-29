from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import text

from control_plane.app.modules.source_control.adapters import (
    SqlAlchemySourceControlEvidenceRepository,
)
from tests.source_control.test_v06_production_e2e import (
    HEAD_SHA,
    INTEGRATION_SHA,
    Journey,
    Subject,
    _grant,
    _merge_integration,
    _open_integration_mr,
    _ready_subject,
    _revoke,
    _write,
    journey,
    production_database,
)

assert journey and production_database
pytestmark = pytest.mark.integration
CAPABILITIES = (
    "work_item.validation.submit",
    "requirement.evidence.request",
    "requirement.evidence.select",
)


def _integrated(journey: Journey, *, count: int = 1) -> Subject:
    subject = _ready_subject(journey, count=count)
    subjects = [
        replace(subject, work_item_id=item, work=f"{subject.base}/work-items/{item}", peers=())
        for item in (subject.work_item_id, *subject.peers)
    ]
    for item in subjects:
        _open_integration_mr(journey, item)
    for item in subjects:
        _merge_integration(journey, item)
    return subject


def _validation(
    subject: Subject, *, reference: str = "urn:external-validation:1"
) -> dict[str, Any]:
    return {
        "targetCommitSha": HEAD_SHA,
        "integrationMergeCommitSha": INTEGRATION_SHA,
        "reference": reference,
        "notes": "A person recorded verification of these exact commits.",
        "artifactReferences": [
            {
                "artifactId": subject.artifact["artifactId"],
                "artifactVersion": str(subject.artifact["version"]),
                "artifactHash": subject.artifact["sha256"],
            }
        ],
    }


def _freeze(journey: Journey, subject: Subject) -> Any:
    current = journey.member.get(subject.base)
    return _write(
        journey.member,
        f"{subject.base}:request-integration-baseline",
        {"expectedRequirementVersion": current.json()["requirement"]["requirementVersion"]},
        etag=current.headers["etag"],
        status=202,
    )


def test_default_baseline_flow_preserves_sibling_validation_and_actual_scoped_authority(
    journey: Journey,
) -> None:
    subject = _integrated(journey, count=2)
    writes = list(journey.provider.writes)
    me = journey.member.get("/api/v1/me")
    navigation = journey.member.get("/api/v1/navigation")
    assert me.status_code == navigation.status_code == 200
    assert set(CAPABILITIES) <= {
        item["capability"]
        for item in me.json()["capabilities"]
        if item.get("scopeId") == journey.workspace_id
    }
    route = next(item for item in navigation.json() if item["routeKey"] == "requirements")
    assert set(CAPABILITIES) <= {item["capability"] for item in route["meta"]["actionCapabilities"]}

    other_workspace = _write(
        journey.leader,
        "/api/v1/admin/workspaces",
        {"name": "Evidence scope isolation", "ownerId": journey.leader_id, "reason": "Scope test"},
        status=201,
    ).json()["id"]
    for capability in (*CAPABILITIES, "requirement.read"):
        _revoke(journey, journey.leader_id, capability)
        _grant(journey.admin, journey.leader_id, capability, other_workspace)
    assert journey.leader.get(f"{subject.base}/delivery").status_code == 403
    assert journey.admin.get(f"{subject.base}/delivery").status_code == 403

    receipts = []
    requests = []
    for work_id in (subject.work_item_id, *subject.peers):
        current = journey.member.get(subject.base)
        body = _validation(subject, reference=f"urn:external-validation:{work_id}")
        path = f"{subject.base}/work-items/{work_id}/external-validations"
        key = f"validation-{work_id}"
        for denied in (journey.leader, journey.admin):
            _write(denied, path, body, etag=current.headers["etag"], status=403)
        receipt = _write(journey.member, path, body, etag=current.headers["etag"], key=key)
        submission = receipt.json()["submission"]
        assert submission["submittedBy"] == journey.member_id
        assert submission["submittedAt"]
        assert submission["targetCommitSha"] == HEAD_SHA
        assert submission["artifactReferences"] == body["artifactReferences"]
        receipts.append(receipt)
        requests.append((path, body, current.headers["etag"], key))
    assert (
        receipts[1].json()["requirement"]["requirementVersion"]
        > receipts[0].json()["requirement"]["requirementVersion"]
    )
    path, body, etag, key = requests[0]
    replay = _write(journey.member, path, body, etag=etag, key=key)
    assert replay.json() == receipts[0].json()
    assert replay.headers["etag"] == receipts[0].headers["etag"]
    _write(journey.member, path, body, etag=etag, status=409)

    current = journey.member.get(subject.base)
    freeze_body = {
        "expectedRequirementVersion": current.json()["requirement"]["requirementVersion"]
    }
    _write(
        journey.leader,
        f"{subject.base}:request-integration-baseline",
        freeze_body,
        etag=current.headers["etag"],
        status=403,
    )
    frozen = _freeze(journey, subject)
    snapshot = frozen.json()["snapshot"]
    evidence_url = f"{subject.base}/delivery-snapshots/{snapshot['id']}/integration-baseline"
    pending = journey.member.get(evidence_url)
    assert pending.status_code == 409
    assert pending.json()["reason"] == "EVIDENCE_UNAVAILABLE_OR_STALE"
    assert journey.worker("relay")["processed"] == 3
    assert journey.worker("process")["processed"] == 1
    response = journey.member.get(evidence_url)
    assert response.status_code == 200, response.text
    evidence = response.json()
    assert evidence["currentnessState"] == "CURRENT"
    assert evidence["currentnessReasons"] == []
    assert evidence["deliverySnapshotHash"] == snapshot["snapshotHash"]
    assert {item["workItemId"] for item in evidence["workItems"]} == set(snapshot["workItemIds"])
    assert journey.leader.get(evidence_url).status_code == 403
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM source_control.external_validation_receipt "
                    "WHERE outcome='ACCEPTED'"
                )
            ).scalar_one()
            == 2
        )

    selection_body = {
        "deliverySnapshotId": snapshot["id"],
        "integrationBaselineId": evidence["id"],
        "expectedRequirementVersion": snapshot["requirementVersion"],
    }
    selection_path = f"{subject.base}/integration-baseline-selections"
    _write(journey.leader, selection_path, selection_body, etag=frozen.headers["etag"], status=403)
    selected = _write(
        journey.member,
        selection_path,
        selection_body,
        etag=frozen.headers["etag"],
        key="baseline-selection",
    )
    replay = _write(
        journey.member,
        selection_path,
        selection_body,
        etag=frozen.headers["etag"],
        key="baseline-selection",
    )
    assert replay.json() == selected.json()
    assert (
        selected.json()["requirement"]["requirementVersion"] == snapshot["requirementVersion"] + 1
    )
    delivery = journey.member.get(f"{subject.base}/delivery")
    assert delivery.json()["currentSelection"] == selected.json()["selection"]
    assert delivery.headers["etag"] == selected.headers["etag"]
    assert journey.member.get(evidence_url).json()["currentnessState"] == "CURRENT"

    changed = _write(
        journey.member,
        f"{subject.work}/external-validations",
        _validation(subject, reference="urn:external-validation:replacement"),
        etag=selected.headers["etag"],
    )
    projection = journey.member.get(f"{subject.base}/delivery").json()
    assert projection["currentSelection"] is None
    stale = journey.member.get(evidence_url).json()
    assert stale["currentnessState"] == "STALE"
    assert "REQUIREMENT_INPUT_CHANGED" in stale["currentnessReasons"]
    _write(
        journey.member,
        selection_path,
        {
            **selection_body,
            "expectedRequirementVersion": changed.json()["requirement"]["requirementVersion"],
        },
        etag=changed.headers["etag"],
        status=409,
    )
    assert journey.provider.writes == writes


@pytest.mark.parametrize("timing", ["before", "during"])
def test_changed_frozen_input_cannot_publish_mixed_version_evidence(
    journey: Journey, timing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = _integrated(journey)
    _write(
        journey.member,
        f"{subject.work}/external-validations",
        _validation(subject),
        etag=journey.member.get(subject.base).headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    frozen = _freeze(journey, subject)
    assert journey.worker("relay")["processed"] == 1
    changed = False

    def change_input() -> None:
        nonlocal changed
        if not changed:
            changed = True
            _write(
                journey.member,
                f"{subject.work}/external-validations",
                _validation(subject, reference="urn:external-validation:changed"),
                etag=journey.member.get(subject.base).headers["etag"],
            )

    if timing == "before":
        change_input()
    else:
        original = SqlAlchemySourceControlEvidenceRepository.integration_evidence_context

        def read_then_change(repository: Any, work_item_id: str) -> Any:
            result = original(repository, work_item_id)
            change_input()
            return result

        monkeypatch.setattr(
            SqlAlchemySourceControlEvidenceRepository,
            "integration_evidence_context",
            read_then_change,
        )
    report = journey.worker("process", errors=("EVIDENCE_STALE",))
    assert changed
    assert (report["claimed"], report["processed"], report["released"]) == (1, 0, 1)
    snapshot_id = frozen.json()["snapshot"]["id"]
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM source_control.integration_baseline_evidence "
                    "WHERE delivery_snapshot_id=:id"
                ),
                {"id": snapshot_id},
            ).scalar_one()
            == 0
        )
    assert (
        journey.member.get(
            f"{subject.base}/delivery-snapshots/{snapshot_id}/integration-baseline"
        ).status_code
        == 409
    )


def test_invalid_artifacts_and_partial_validation_never_become_available_evidence(
    journey: Journey,
) -> None:
    subject = _integrated(journey, count=2)
    other = _write(
        journey.member,
        "/api/v1/requirements",
        {
            "workspaceId": journey.workspace_id,
            "type": "feat",
            "title": "Artifact ownership boundary",
            "description": "An exact Artifact still belongs to its Requirement",
            "acceptanceCriteria": ["Foreign references are rejected"],
            "initialRepositoryId": journey.repository_id,
        },
        status=201,
    )
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process")["processed"] == 1
    other_base = f"/api/v1/requirements/{other.json()['requirement']['id']}"
    foreign_artifact = _write(
        journey.member,
        f"{other_base}/sdd-artifacts",
        {"content": "# An explicitly referenced Artifact owned by another Requirement"},
        etag=journey.member.get(other_base).headers["etag"],
        status=201,
    ).json()["artifact"]
    current = journey.member.get(subject.base)
    body = _validation(subject)
    path = f"{subject.work}/external-validations"
    for invalid, status in (
        (
            {
                **body,
                "artifactReferences": [
                    {
                        "artifactId": foreign_artifact["artifactId"],
                        "artifactVersion": str(foreign_artifact["version"]),
                        "artifactHash": foreign_artifact["sha256"],
                    }
                ],
            },
            409,
        ),
        ({**body, "artifactReferences": []}, 422),
        ({**body, "artifactReferences": body["artifactReferences"] * 2}, 422),
        (
            {
                **body,
                "artifactReferences": [{**body["artifactReferences"][0], "artifactVersion": "99"}],
            },
            409,
        ),
        (
            {
                **body,
                "artifactReferences": [
                    {**body["artifactReferences"][0], "artifactHash": "sha256:" + "0" * 64}
                ],
            },
            409,
        ),
    ):
        _write(journey.member, path, invalid, etag=current.headers["etag"], status=status)
    assert journey.member.get(subject.base).headers["etag"] == current.headers["etag"]
    registered = _write(
        journey.member,
        path,
        {**body, "targetCommitSha": "e" * 40},
        etag=current.headers["etag"],
    )
    assert journey.worker("relay")["processed"] == 1
    with journey.database.owner.connect() as db:
        assert db.execute(
            text(
                "SELECT outcome, rejection_reason_code "
                "FROM source_control.external_validation_receipt"
            )
        ).one() == ("REJECTED", "EVIDENCE_STALE")
    _write(journey.member, path, body, etag=registered.headers["etag"])
    frozen = _freeze(journey, subject)
    assert journey.worker("relay")["processed"] == 2
    report = journey.worker("process", errors=("EVIDENCE_UNAVAILABLE",))
    assert (report["claimed"], report["processed"], report["released"]) == (1, 0, 1)
    assert (
        journey.member.get(
            f"{subject.base}/delivery-snapshots/{frozen.json()['snapshot']['id']}/integration-baseline"
        ).status_code
        == 409
    )
    with journey.database.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM source_control.integration_baseline_evidence")
            ).scalar_one()
            == 0
        )
