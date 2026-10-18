import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from control_plane.app.modules import configuration
from tests.configuration.test_draft_archive_timing import timing_state


def record(revision: int = 1, **changes: Any) -> dict[str, Any]:
    content = '{"identity.login_backoff":null,"large":9007199254740993}'
    row = {
        "draft_id": "81000000-0000-4000-8000-000000000001",
        "namespace": "identity",
        "scope": "PLATFORM",
        "revision": revision,
        "operation": "CREATE" if revision == 1 else "UPDATE",
        "actor_id": "actor",
        "recorded_at": datetime(2026, 10, 18, 8, 0, 0, 123456, tzinfo=UTC),
        "owner_id": "actor",
        "base_version": 1,
        "schema_revision": 1,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "content_json": content,
        "content_hash_matches_content": True,
        "rollback_from_version": None,
        "validation": None,
        "takeover": None,
        "governance_record_id": None,
        **changes,
    }
    envelope = {
        key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:]): value
        for key, value in row.items()
    }
    if row["takeover"] is not None:
        envelope["takeover"] = {
            "previousOwnerId": row["takeover"]["previous_owner_id"],
            "reason": row["takeover"]["reason"],
        }
    envelope["recordedAt"] = (
        row["recorded_at"].astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    row["revision_snapshot_hash"] = hashlib.sha256(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return row


def state(rows: list[dict[str, Any]], through: int = 3) -> Any:
    h = timing_state()
    target = {
        "id": record()["draft_id"],
        "namespace": "identity",
        "scope": "PLATFORM",
        "revision": through,
    }
    h.owner.draft_revision_target.return_value = target
    h.owner.draft_revision_rows.return_value = deepcopy(rows)
    revisions = [row["revision"] for row in rows]
    h.owner.draft_revision_stats.return_value = {
        "recorded_count": len(rows),
        "first_recorded_revision": min(revisions) if rows else None,
        "last_recorded_revision": max(revisions) if rows else None,
    }
    h.target = target
    return h


def read(h: Any, **changes: Any) -> Any:
    assert hasattr(h.lifecycle, "draft_revisions"), "revision history is not implemented"
    return h.lifecycle.draft_revisions(
        namespace="identity",
        draft_id=h.target["id"],
        expected_revision=h.target["revision"],
        **changes,
    )


@pytest.mark.parametrize(
    "revisions,coverage", [([], "NONE"), ([3, 2], "PARTIAL"), ([3, 2, 1], "COMPLETE")]
)
def test_coverage_describes_captured_revisions_without_loading_current_content(
    revisions: list[int], coverage: str
) -> None:
    h = state([record(r) for r in revisions])
    value = read(h)
    assert value.coverage == coverage
    assert [item.revision for item in value.items] == revisions
    assert value.recorded_count == len(revisions)
    assert value.next_cursor is None
    assert h.owner.draft_revision_target.call_count == 2
    assert {call[0] for call in h.owner.mock_calls} <= {
        "draft_revision_target",
        "draft_revision_rows",
        "draft_revision_stats",
    }
    assert h.db.mock_calls == []
    h.clock.now.assert_not_called()


def test_legacy_hash_mismatch_is_a_valid_revision_fact() -> None:
    h = state(
        [
            record(
                3,
                operation="VALIDATE",
                content_hash="f" * 64,
                content_hash_matches_content=False,
                validation={
                    "valid": False,
                    "issues": [{"key": "large", "code": "INVALID", "message": "actual issue"}],
                },
            )
        ]
    )
    value = read(h)
    assert value.items[0].content_hash == "f" * 64
    assert not value.items[0].content_hash_matches_content
    assert "9007199254740993" in value.items[0].content_json
    assert not value.items[0].validation.valid


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_json", "{}"),
        ("content_hash_matches_content", False),
        ("revision_snapshot_hash", "0" * 64),
    ],
)
def test_corrupt_record_is_not_treated_as_missing_history(field: str, value: Any) -> None:
    h = state([record(3)])
    h.owner.draft_revision_rows.return_value[0][field] = value
    with pytest.raises(configuration.PolicySnapshotUnavailable):
        read(h)


def test_revision_race_wins_over_corrupt_record() -> None:
    h = state([record(3)])
    h.owner.draft_revision_rows.return_value[0]["content_json"] = "bad"
    h.owner.draft_revision_target.side_effect = [h.target, {**h.target, "revision": 4}]
    with pytest.raises(configuration.StaleDraftRevision):
        read(h)


def test_record_builder_preserves_invalid_candidate_and_business_time() -> None:
    assert hasattr(configuration, "draft_revision_record"), "revision capture is not implemented"
    candidate = {"large": 9007199254740993, "invalid": None, "text": "  中 / e\u0301 / 🙂 / \n\t  "}
    draft = {
        "id": record()["draft_id"],
        "namespace": "identity",
        "scope": "PLATFORM",
        "revision": 2,
        "owner_id": "new-owner",
        "base_version": 1,
        "schema_revision": 1,
        "content": candidate,
        "content_hash": "0" * 64,
        "rollback_from_version": None,
        "last_meaningful_activity_at": datetime(
            2026, 10, 18, 8, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8))
        ),
    }
    result = configuration.draft_revision_record(
        draft,
        operation="TAKEOVER",
        actor_id="new-owner",
        takeover={"previous_owner_id": "old-owner", "reason": "  keep whitespace  "},
    )
    assert result["content_hash"] == "0" * 64
    assert result["content_hash_matches_content"] is False
    assert json.loads(result["content_json"]) == candidate
    assert result["recorded_at"] == draft["last_meaningful_activity_at"]
    expected = record(
        2, **{k: v for k, v in result.items() if k not in {"revision", "revision_snapshot_hash"}}
    )
    assert result["revision_snapshot_hash"] == expected["revision_snapshot_hash"]
