from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from control_plane.app.modules.identity.adapters.runtime import SystemClock
from control_plane.app.modules.requirement.adapters import SqlAlchemySddArtifactReader
from control_plane.app.modules.requirement.ports import ArtifactState
from tests.source_control.test_external_validation_baseline_e2e import _integrated
from tests.source_control.test_v06_production_e2e import (
    FORMAL_SHA,
    HEAD_SHA,
    GitLabTransport,
    Journey,
    _accept,
    _finish,
    _formal_review,
    _open_acceptance,
    _revoke,
    _select_baseline,
    _write,
    journey,
    production_database,
)

assert journey and production_database
pytestmark = pytest.mark.integration


def test_default_acceptance_formal_roles_gate_etags_and_history(journey: Journey) -> None:
    subject = _integrated(journey)
    selected = _select_baseline(journey, subject)
    capabilities = {
        "requirement.acceptance.submit",
        "requirement.acceptance.decide",
        "requirement.delivery_gate.assign",
        "formal_merge_request.request",
        "merge_request.review",
        "merge_request.merge",
    }
    me = journey.member.get("/api/v1/me")
    navigation = journey.member.get("/api/v1/navigation")
    assert me.status_code == navigation.status_code == 200
    assert capabilities <= {
        item["capability"]
        for item in me.json()["capabilities"]
        if item.get("scopeId") == journey.workspace_id
    }
    route = next(item for item in navigation.json() if item["routeKey"] == "requirements")
    assert capabilities <= {item["capability"] for item in route["meta"]["actionCapabilities"]}
    body = {"selectionId": selected.json()["selection"]["id"]}
    _write(
        journey.admin,
        f"{subject.base}/acceptance-confirmations",
        body,
        etag=selected.headers["etag"],
        status=403,
    )
    acceptance = _write(
        journey.member,
        f"{subject.base}/acceptance-confirmations",
        body,
        etag=selected.headers["etag"],
    )
    gate, assignment = acceptance.json()["gate"], acceptance.json()["assignment"]
    assert gate["state"] == "OPEN"
    assert (
        gate["integrationBaselineHash"] == selected.json()["selection"]["integrationBaselineHash"]
    )
    assert assignment["defaultReviewerId"] == assignment["currentReviewerId"] == journey.member_id
    projection = journey.member.get(f"{subject.base}/delivery").json()
    assert projection["currentAcceptance"]["decision"] is None
    path = f"{subject.base}/delivery-gates/{gate['id']}:reassign"
    body = {"candidateId": journey.leader_id, "reason": "The default reviewer delegates acceptance"}
    _write(journey.leader, path, body, etag=f'"v{gate["revision"]}"', status=403)
    _write(journey.member, path, body, etag=acceptance.headers["etag"], status=409)
    delegated = _write(
        journey.member, path, body, etag=f'"v{gate["revision"]}"', key="delegate-acceptance"
    )
    assert delegated.headers["etag"] == f'"v{gate["revision"] + 1}"'
    current = journey.member.get(subject.base)
    assert (
        current.json()["requirement"]["revision"]
        == acceptance.json()["requirement"]["revision"] + 1
    )
    replay = _write(
        journey.member, path, body, etag=f'"v{gate["revision"]}"', key="delegate-acceptance"
    )
    assert replay.json() == delegated.json()
    assert journey.member.get(subject.base).headers["etag"] == current.headers["etag"]
    _write(
        journey.leader,
        path,
        {"candidateId": journey.member_id, "reason": "Assignee cannot delegate again"},
        etag=delegated.headers["etag"],
        status=403,
    )
    decision = {
        "gateId": gate["id"],
        "outcome": "APPROVED",
        "reason": "Approve the exact selection",
    }
    _write(
        journey.member,
        f"{subject.base}/acceptance-decisions",
        decision,
        etag=current.headers["etag"],
        status=403,
    )
    accepted = _write(
        journey.leader,
        f"{subject.base}/acceptance-decisions",
        decision,
        etag=current.headers["etag"],
    )
    assert accepted.json()["decision"]["gateAssignmentId"] == delegated.json()["assignment"]["id"]
    assert accepted.json()["requirement"]["state"] == "AWAITING_MERGE"
    assert (
        accepted.json()["requirement"]["requirementVersion"]
        == selected.json()["requirement"]["requirementVersion"]
    )
    _write(
        journey.leader,
        f"{subject.work}:request-formal-mr",
        {},
        etag=accepted.headers["etag"],
        status=409,
    )
    review = _formal_review(journey, subject)
    assert review["gate"]["subjectHeadSha"] == HEAD_SHA
    assert review["assignment"]["defaultReviewerId"] == journey.leader_id
    current = journey.member.get(subject.base)
    _write(
        journey.member,
        f"{subject.work}:request-formal-merge",
        {},
        etag=current.headers["etag"],
        status=409,
    )
    reassigned = _write(
        journey.leader,
        f"{subject.base}/delivery-gates/{review['gate']['id']}:reassign",
        {"candidateId": journey.member_id, "reason": "Explicitly assign the owner to review"},
        etag=f'"v{review["gate"]["revision"]}"',
    )
    _revoke(journey, journey.leader_id, "merge_request.review")
    _revoke(journey, journey.leader_id, "code.change")
    _revoke(journey, journey.member_id, "merge_request.merge")
    current = journey.member.get(subject.base)
    review_body = {
        "gateId": review["gate"]["id"],
        "outcome": "APPROVED",
        "reason": "Assigned reviewer approves this exact head",
    }
    _write(
        journey.leader,
        f"{subject.base}/formal-review-decisions",
        review_body,
        etag=current.headers["etag"],
        status=403,
    )
    reviewed = _write(
        journey.member,
        f"{subject.base}/formal-review-decisions",
        review_body,
        etag=current.headers["etag"],
    )
    assert reviewed.json()["decision"]["gateAssignmentId"] == reassigned.json()["assignment"]["id"]
    assert reviewed.json()["decision"]["subjectHeadSha"] == HEAD_SHA
    _write(
        journey.member,
        f"{subject.work}:request-formal-merge",
        {},
        etag=reviewed.headers["etag"],
        status=403,
    )
    writes = list(journey.provider.writes)
    requested = _write(
        journey.leader,
        f"{subject.work}:request-formal-merge",
        {},
        etag=reviewed.headers["etag"],
        status=202,
    )
    assert requested.json()["workItem"]["formalDeliveryState"] == "MERGE_PENDING"
    assert journey.provider.writes == writes
    assert journey.worker("relay")["processed"] == journey.worker("process")["processed"] == 1
    delivery = journey.member.get(f"{subject.base}/delivery").json()
    assert delivery["requirement"]["state"] == "COMPLETED"
    assert delivery["workItems"][0]["workItem"]["formalDeliveryState"] == "MERGED"
    assert journey.provider.branches["main"] == FORMAL_SHA
    history: list[dict[str, Any]] = []
    cursors: set[str] = set()
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = journey.member.get(f"{subject.base}/delivery/history", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["etag"] == f'"v{delivery["requirement"]["revision"]}"'
        history.extend(response.json()["items"])
        cursor = response.json()["nextCursor"]
        if cursor is None:
            break
        assert cursor not in cursors
        cursors.add(cursor)
    assert len({(item["factType"], item["fact"]["id"]) for item in history}) == len(history)
    decisions = [item["fact"] for item in history if item["factType"] == "DELIVERY_DECISION"]
    assert {item["reviewerId"] for item in decisions} == {journey.member_id, journey.leader_id}
    assert all(item["outcome"] == "APPROVED" for item in decisions)
    assert any(
        item["fact"]["supersededAt"] is not None
        for item in history
        if item["factType"] == "DELIVERY_GATE_ASSIGNMENT"
    )


@pytest.mark.parametrize("phase", ["acceptance", "formal"])
def test_assignment_round_trip_rejects_a_delayed_decision_from_the_old_assignment(
    journey: Journey, phase: str
) -> None:
    subject = _integrated(journey)
    acceptance = _open_acceptance(journey, subject)
    if phase == "acceptance":
        gate = acceptance.json()["gate"]
        original_assignment = acceptance.json()["assignment"]
        client, original_actor, candidate = journey.member, journey.member_id, journey.leader_id
        decision_path = f"{subject.base}/acceptance-decisions"
    else:
        _accept(journey, subject, acceptance)
        review = _formal_review(journey, subject)
        gate, original_assignment = review["gate"], review["assignment"]
        client, original_actor, candidate = journey.leader, journey.leader_id, journey.member_id
        decision_path = f"{subject.base}/formal-review-decisions"
    before = journey.member.get(subject.base)
    path = f"{subject.base}/delivery-gates/{gate['id']}:reassign"
    etag = f'"v{gate["revision"]}"'
    for actor in (candidate, original_actor):
        reassigned = _write(
            client,
            path,
            {"candidateId": actor, "reason": "An explicit assignment change"},
            etag=etag,
        )
        etag = reassigned.headers["etag"]
    assert reassigned.json()["assignment"]["id"] != original_assignment["id"]
    assert reassigned.json()["assignment"]["currentReviewerId"] == original_actor
    current = journey.member.get(subject.base)
    assert current.json()["requirement"]["revision"] == before.json()["requirement"]["revision"] + 2
    assert (
        current.json()["requirement"]["requirementVersion"]
        == before.json()["requirement"]["requirementVersion"]
    )
    body = {
        "gateId": gate["id"],
        "outcome": "APPROVED",
        "reason": "Decision on the read assignment",
    }
    _write(
        client,
        decision_path,
        body,
        etag=before.headers["etag"],
        status=409,
        key="delayed-old-assignment",
    )
    decided = _write(client, decision_path, body, etag=current.headers["etag"])
    assert decided.json()["decision"]["gateAssignmentId"] == reassigned.json()["assignment"]["id"]


def test_artifact_proof_is_rechecked_by_acceptance_review_and_formal_worker(
    journey: Journey, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = _integrated(journey)
    selected = _select_baseline(journey, subject)
    available = True
    original = SqlAlchemySddArtifactReader.get_snapshot

    def snapshot(reader: Any, *args: Any) -> Any:
        result = original(reader, *args)
        return (
            result if available else result.model_copy(update={"state": ArtifactState.UNAVAILABLE})
        )

    monkeypatch.setattr(SqlAlchemySddArtifactReader, "get_snapshot", snapshot)
    available = False
    _write(
        journey.member,
        f"{subject.base}/acceptance-confirmations",
        {"selectionId": selected.json()["selection"]["id"]},
        etag=selected.headers["etag"],
        status=409,
    )
    available = True
    acceptance = _write(
        journey.member,
        f"{subject.base}/acceptance-confirmations",
        {"selectionId": selected.json()["selection"]["id"]},
        etag=selected.headers["etag"],
    )
    available = False
    _write(
        journey.member,
        f"{subject.base}/acceptance-decisions",
        {
            "gateId": acceptance.json()["gate"]["id"],
            "outcome": "APPROVED",
            "reason": "Proof must still be available",
        },
        etag=acceptance.headers["etag"],
        status=409,
    )
    available = True
    accepted = _accept(journey, subject, acceptance)
    available = False
    _write(
        journey.member,
        f"{subject.work}:request-formal-mr",
        {},
        etag=accepted.headers["etag"],
        status=409,
    )
    available = True
    review = _formal_review(journey, subject)
    current = journey.member.get(subject.base)
    body = {
        "gateId": review["gate"]["id"],
        "outcome": "APPROVED",
        "reason": "Review requires the selected artifact",
    }
    available = False
    _write(
        journey.leader,
        f"{subject.base}/formal-review-decisions",
        body,
        etag=current.headers["etag"],
        status=409,
    )
    available = True
    reviewed = _write(
        journey.leader,
        f"{subject.base}/formal-review-decisions",
        body,
        etag=current.headers["etag"],
    )
    available = False
    _write(
        journey.leader,
        f"{subject.work}:request-formal-merge",
        {},
        etag=reviewed.headers["etag"],
        status=409,
    )
    available = True
    _write(
        journey.leader,
        f"{subject.work}:request-formal-merge",
        {},
        etag=reviewed.headers["etag"],
        status=202,
    )
    assert journey.worker("relay")["processed"] == 1
    writes = list(journey.provider.writes)
    available = False
    report = journey.worker("process", errors=("CONNECTOR_UNAVAILABLE",))
    assert (report["claimed"], report["processed"], report["released"]) == (1, 0, 1)
    assert journey.provider.writes == writes
    available = True
    future = datetime.now(UTC) + timedelta(minutes=3)
    monkeypatch.setattr(SystemClock, "now", lambda _: future)
    assert journey.worker("process")["processed"] == 1
    assert (
        journey.member.get(subject.base).json()["workItems"][0]["formalDeliveryState"] == "MERGED"
    )


def test_unknown_formal_merge_adopts_proven_result_after_source_deletion_and_revoke(
    journey: Journey, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = _integrated(journey)
    _accept(journey, subject, _open_acceptance(journey, subject))
    review = _formal_review(journey, subject)
    reviewed = _write(
        journey.leader,
        f"{subject.base}/formal-review-decisions",
        {
            "gateId": review["gate"]["id"],
            "outcome": "APPROVED",
            "reason": "Approve exact head before merge",
        },
        etag=journey.member.get(subject.base).headers["etag"],
    )
    original = GitLabTransport.__call__
    lost = False

    def lose_ack(provider: GitLabTransport, request: httpx.Request) -> httpx.Response:
        nonlocal lost
        response = original(provider, request)
        if request.method == "PUT" and request.url.path.endswith("/merge") and not lost:
            lost = True
            mr = next(item for item in provider.mrs.values() if item["target_branch"] == "main")
            del provider.branches[mr["source_branch"]]
            raise httpx.ReadTimeout("Merge completed but acknowledgement was lost", request=request)
        return response

    monkeypatch.setattr(GitLabTransport, "__call__", lose_ack)
    path = f"{subject.work}:request-formal-merge"
    accepted = _write(
        journey.leader,
        path,
        {},
        etag=reviewed.headers["etag"],
        status=202,
        key="formal-merge-unknown",
    )
    assert journey.worker("relay")["processed"] == 1
    assert journey.worker("process", errors=("EXTERNAL_RESULT_UNKNOWN",))["processed"] == 1
    assert lost
    assert (
        journey.member.get(subject.base).json()["workItems"][0]["formalDeliveryState"]
        == "RECONCILIATION_PENDING"
    )
    replay = _write(
        journey.leader,
        path,
        {},
        etag=reviewed.headers["etag"],
        status=202,
        key="formal-merge-unknown",
    )
    assert replay.json() == accepted.json()
    _revoke(journey, journey.leader_id, "merge_request.merge")
    writes = list(journey.provider.writes)
    future = datetime.now(UTC) + timedelta(minutes=3)
    monkeypatch.setattr(SystemClock, "now", lambda _: future)
    assert journey.worker("reconcile")["processed"] == 1
    final = journey.member.get(subject.base).json()
    assert final["requirement"]["state"] == "COMPLETED"
    assert final["workItems"][0]["formalDeliveryState"] == "MERGED"
    assert final["workItems"][0]["taskBranch"] not in journey.provider.branches
    assert journey.provider.writes == writes
    assert journey.worker("process")["processed"] == journey.worker("reconcile")["processed"] == 0
    assert journey.member.get(subject.base).json() == final


def test_confirmed_merge_survives_later_provider_read_denial(
    journey: Journey, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = _integrated(journey)
    _accept(journey, subject, _open_acceptance(journey, subject))
    review = _formal_review(journey, subject)
    original = GitLabTransport.__call__

    def provider_with_later_read_denial(
        provider: GitLabTransport, request: httpx.Request
    ) -> httpx.Response:
        if request.method == "GET" and "/merge_requests/" in request.url.path:
            iid = int(request.url.path.rsplit("/", 1)[1])
            mr = provider.mrs[iid]
            if mr["target_branch"] == "main" and mr["state"] == "merged":
                return httpx.Response(403)
        response = original(provider, request)
        if request.method == "PUT" and request.url.path.endswith("/merge"):
            mr = next(item for item in provider.mrs.values() if item["target_branch"] == "main")
            del provider.branches[mr["source_branch"]]
        return response

    monkeypatch.setattr(GitLabTransport, "__call__", provider_with_later_read_denial)
    _finish(journey, subject, review)
    before = journey.member.get(subject.base).json()
    writes = list(journey.provider.writes)
    assert before["workItems"][0]["taskBranch"] not in journey.provider.branches
    assert journey.worker("process")["processed"] == journey.worker("reconcile")["processed"] == 0
    assert journey.provider.writes == writes
    assert journey.member.get(subject.base).json() == before


def test_sibling_head_failure_keeps_the_already_merged_work_item(journey: Journey) -> None:
    subject = _integrated(journey, count=2)
    peer_id = subject.peers[0]
    peer = replace(
        subject, work_item_id=peer_id, work=f"{subject.base}/work-items/{peer_id}", peers=()
    )
    _accept(journey, subject, _open_acceptance(journey, subject))
    first_review = _formal_review(journey, subject)
    second_review = _formal_review(journey, peer)
    _finish(journey, subject, first_review, expected_state="AWAITING_MERGE")
    reviewed = _write(
        journey.leader,
        f"{subject.base}/formal-review-decisions",
        {
            "gateId": second_review["gate"]["id"],
            "outcome": "APPROVED",
            "reason": "Sibling completion does not invalidate this head",
        },
        etag=journey.member.get(subject.base).headers["etag"],
    )
    _write(
        journey.leader,
        f"{peer.work}:request-formal-merge",
        {},
        etag=reviewed.headers["etag"],
        status=202,
    )
    assert journey.worker("relay")["processed"] == 1
    current = journey.member.get(subject.base).json()
    work = next(item for item in current["workItems"] if item["id"] == peer_id)
    journey.provider.branches[work["taskBranch"]] = "e" * 40
    writes = list(journey.provider.writes)
    assert journey.worker("process", errors=("HEAD_SHA_CHANGED",))["processed"] == 1
    final = journey.member.get(subject.base).json()
    first = next(item for item in final["workItems"] if item["id"] == subject.work_item_id)
    second = next(item for item in final["workItems"] if item["id"] == peer_id)
    assert (first["state"], first["formalDeliveryState"]) == ("COMPLETED", "MERGED")
    assert second["formalBlockedReasonCode"] == "HEAD_SHA_CHANGED"
    assert final["requirement"]["state"] != "COMPLETED"
    assert journey.worker("process")["processed"] == journey.worker("reconcile")["processed"] == 0
    assert journey.provider.writes == writes
