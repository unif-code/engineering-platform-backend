"""Real PostgreSQL regression for hashes derived from the stored candidate representation."""

import hashlib
import json
from typing import Any

import pytest

import control_plane.app.bootstrap.app as bootstrap
from tests.configuration.test_draft_takeover_e2e import actors as actors
from tests.configuration.test_draft_takeover_e2e import journey as journey
from tests.configuration.test_draft_takeover_e2e import production_database as production_database
from tests.source_control.test_v06_production_e2e import _write

pytestmark = pytest.mark.integration


def assert_saved_hash(state: Any, namespace: str, draft_id: str) -> None:
    runtime = bootstrap.configuration_http_runtime().owners.resolve(namespace)
    with runtime.transaction() as lifecycle:
        draft = lifecycle.owner.draft(draft_id)
        assert draft is not None
        canonical = json.dumps(
            draft.content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert hashlib.sha256(canonical.encode()).hexdigest() == draft.content_hash
    response = state.a.client.get(
        f"/api/v1/admin/policies/{namespace}/drafts/{draft_id}/revisions",
        headers={"If-Match": f'"v{draft.revision}"'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["coverage"] == "COMPLETE" and body["recordedCount"] == draft.revision
    assert all(item["contentHashMatchesContent"] for item in body["items"])
    assert body["items"][0]["contentJson"] == canonical
    assert body["items"][0]["contentHash"] == draft.content_hash


def sequence(state: Any, namespace: str, values: dict[str, Any]) -> None:
    created = _write(
        state.a.client, f"/api/v1/admin/policies/{namespace}/drafts", {"values": values}, status=201
    )
    draft_id = created.json()["id"]
    path = f"/api/v1/admin/policies/{namespace}/drafts/{draft_id}"
    assert_saved_hash(state, namespace, draft_id)
    latest = created
    steps: list[tuple[Any, str, dict[str, Any], str]] = [
        (state.a, "/validate", {}, "POST"),
        (state.b, "/takeover", {"reason": "Representation regression"}, "POST"),
        (state.b, "", {"values": {}}, "PATCH"),
    ]
    for actor, suffix, body, method in steps:
        latest = _write(
            actor.client, path + suffix, body, method=method, etag=latest.headers["etag"]
        )
        assert_saved_hash(state, namespace, draft_id)
    assert latest.json()["revision"] == 4


def test_identity_hashes_match_actual_jsonb_after_every_revision(actors: Any) -> None:
    cases: list[dict[str, Any]] = [
        {"identity.session_cap": value}
        for value in (
            3,
            9007199254740993,
            10**40 + 123456789,
            1e20,
            1e21,
            -0.0,
            0.0,
            1.0,
            1.5,
            1e-7,
        )
    ] + [
        {"identity.login_backoff": {}},
        {"identity.login_backoff": None},
        {"identity.password_max_age": '合成🧪é/é "引号" \\反斜线\n换行\t制表'},
        {"identity.login_backoff": {"备注": ["合成", "\\\n\t"], "a": None}},
    ]
    for values in cases:
        sequence(actors, "identity", values)


def test_requirement_hashes_preserve_existing_candidate_admission(actors: Any) -> None:
    sequence(actors, "requirement.gate", {"draft_archive_after_days": 7})
    invalid: list[dict[str, Any]] = [
        {"draft_archive_after_days": 1.0},
        {"draft_archive_after_days": 1e20},
        {"acceptance.additional_required_capabilities": None},
    ]
    for values in invalid:
        _write(
            actors.a.client,
            "/api/v1/admin/policies/requirement.gate/drafts",
            {"values": values},
            status=422,
        )
