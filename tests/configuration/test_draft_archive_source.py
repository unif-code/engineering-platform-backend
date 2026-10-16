from copy import deepcopy
from datetime import timedelta, timezone
from typing import Any

import pytest

from control_plane.app.modules.configuration import (
    DraftNotFound,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from tests.configuration.test_draft_archive_timing import timing_state


def source_state(namespace: str = "identity", source: str = "MANUAL") -> Any:
    h = timing_state(namespace)
    h.row.update(
        status="ARCHIVED", archived_at=h.row["last_meaningful_activity_at"] + timedelta(days=30)
    )
    h.row["base_version"] = 99  # Historical observation must not read Current or gate on Base.
    payload = {
        "draftId": h.row["id"],
        "namespace": namespace,
        "scope": "PLATFORM",
        "result": "ARCHIVED",
    }
    if source == "MANUAL":
        payload.update(
            source="MANUAL",
            actorId=h.row["owner_id"],
            actorType="human",
            draftRevision=h.row["revision"],
        )
    else:
        payload.update(policyVersion=7, cutoff=h.row["last_meaningful_activity_at"].isoformat())
    h.records = [
        {
            "id": "83000000-0000-4000-8000-000000000001",
            "namespace": namespace,
            "scope": "PLATFORM",
            "event_type": "DRAFT_ARCHIVED",
            "aggregate_id": f"{namespace}:PLATFORM:draft:{h.row['id']}",
            "payload": payload,
            "occurred_at": h.row["archived_at"],
        }
    ]
    h.owner.archive_records.side_effect = lambda *_: deepcopy(h.records)
    h.clock.now.side_effect = AssertionError("source query has no clock")
    return h


def read(h: Any, **overrides: Any) -> Any:
    assert hasattr(h.lifecycle, "archive_source"), "archive source query is missing"
    return h.lifecycle.archive_source(
        **{
            "namespace": h.namespace,
            "draft_id": h.row["id"],
            "expected_revision": h.row["revision"],
            **overrides,
        }
    )


def readonly(h: Any) -> None:
    assert {call[0] for call in h.owner.mock_calls} <= {"draft_summary", "archive_records"}
    assert all(call.kwargs.get("for_update") is not True for call in h.owner.mock_calls)
    h.clock.now.assert_not_called()
    assert h.db.mock_calls == []


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("source", ["MANUAL", "INACTIVITY"])
@pytest.mark.parametrize("offset", [0, 330, -480])
def test_record_projects_only_real_source_fields_with_exact_instants_and_no_current(
    namespace: str, source: str, offset: int
) -> None:
    h = source_state(namespace, source)
    h.records[0]["occurred_at"] = h.row["archived_at"].astimezone(
        timezone(timedelta(minutes=offset))
    )
    before = deepcopy((h.row, h.records))
    result = read(h)
    assert result.record_status == "RECORDED" and result.status == "ARCHIVED"
    assert result.draft_id == h.row["id"] and result.draft_revision == h.row["revision"]
    assert result.archived_at == h.row["archived_at"]
    record = result.record.model_dump()
    assert record["source"] == source and record["recorded_at"] == result.archived_at
    assert record["recorded_at"].isoformat() == h.records[0]["occurred_at"].isoformat()
    if source == "MANUAL":
        assert set(record) == {
            "source",
            "id",
            "recorded_at",
            "actor_id",
            "actor_type",
            "draft_revision",
        }
        assert record["actor_id"] == h.row["owner_id"] and record["actor_type"] == "human"
    else:
        assert set(record) == {"source", "id", "recorded_at", "policy_version", "cutoff"}
        assert (
            record["policy_version"] == 7
            and record["cutoff"] == h.row["last_meaningful_activity_at"]
        )
    assert h.owner.draft_summary.call_count == 2 and h.owner.archive_records.call_count == 1
    assert (h.row, h.records) == before
    readonly(h)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("status,result", [("DRAFT", "NOT_ARCHIVED"), ("ARCHIVED", "UNKNOWN")])
def test_missing_record_is_explicit_and_never_infers_source(
    namespace: str, status: str, result: str
) -> None:
    h = source_state(namespace)
    h.records = []
    h.row.update(status=status, archived_at=None if status == "DRAFT" else h.row["archived_at"])
    value = read(h)
    assert value.record_status == result and value.record is None
    assert value.archived_at == h.row["archived_at"]
    readonly(h)


@pytest.mark.parametrize(
    "case,error",
    [
        ("missing", DraftNotFound),
        ("namespace", DraftNotFound),
        ("scope", DraftNotFound),
        ("revision", StaleDraftRevision),
    ],
)
def test_initial_target_binding_is_checked_before_event_read(
    case: str, error: type[Exception]
) -> None:
    h = source_state()
    if case == "missing":
        h.owner.draft_summary.side_effect = lambda *_: None
    elif case == "revision":
        h.owner.draft_summary.side_effect = lambda *_: {**h.row, "revision": 2}
    else:
        h.owner.draft_summary.side_effect = lambda *_: {**h.row, case: "other"}
    with pytest.raises(error):
        read(h)
    h.owner.archive_records.assert_not_called()
    readonly(h)


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 2),
        ("owner_id", "other"),
        ("base_version", 100),
        ("schema_revision", 2),
        ("content_hash", "a" * 64),
        ("rollback_from_version", 1),
        ("namespace", "other"),
        ("status", "DRAFT"),
        ("disappeared", None),
        ("activity", None),
        ("archive-time", None),
    ],
)
def test_all_metadata_changes_take_precedence_over_bad_or_duplicate_records(
    field: str, value: Any
) -> None:
    h = source_state()
    after = deepcopy(h.row)
    if field == "activity":
        after["last_meaningful_activity_at"] += timedelta(microseconds=1)
    elif field == "archive-time":
        after["archived_at"] += timedelta(microseconds=1)
    elif field != "disappeared":
        after[field] = value
    h.owner.draft_summary.side_effect = [h.row, None if field == "disappeared" else after]
    h.records = [h.records[0], {"bad": "private-invalid-event"}]
    with pytest.raises(StaleDraftRevision):
        read(h)
    readonly(h)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "oversized",
        "id",
        "namespace",
        "scope",
        "event-type",
        "aggregate",
        "extra-event",
        "payload-null",
        "extra-payload",
        "missing-target",
        "wrong-target",
        "wrong-namespace",
        "wrong-scope",
        "result",
        "manual-source",
        "manual-missing-source",
        "manual-actor",
        "manual-empty-actor",
        "manual-actor-type",
        "manual-revision",
        "manual-bool-revision",
        "manual-policy",
        "record-time",
        "naive-record-time",
        "subminute-time",
        "draft-with-record",
        "archived-no-time",
        "backwards-archive",
        "bad-metadata",
        "dependency",
    ],
)
def test_corrupt_manual_or_common_facts_fail_closed_without_partial_source(case: str) -> None:
    h = source_state()
    event = h.records[0]
    payload = event["payload"]
    if case in {"duplicate", "oversized"}:
        h.records *= 2 if case == "duplicate" else 3
    changes: dict[str, tuple[str, Any]] = {
        "id": ("id", "bad"),
        "namespace": ("namespace", "other"),
        "scope": ("scope", "WORKSPACE"),
        "event-type": ("event_type", "POLICY_PUBLISHED"),
        "aggregate": ("aggregate_id", "other"),
        "extra-event": ("delivered_at", None),
        "payload-null": ("payload", None),
    }
    if case in changes:
        key, value = changes[case]
        event[key] = value
    changes = {
        "extra-payload": ("extra", True),
        "wrong-target": ("draftId", "other"),
        "wrong-namespace": ("namespace", "other"),
        "wrong-scope": ("scope", "WORKSPACE"),
        "result": ("result", "SKIPPED"),
        "manual-source": ("source", "AUTOMATIC"),
        "manual-actor": ("actorId", "other"),
        "manual-empty-actor": ("actorId", ""),
        "manual-actor-type": ("actorType", "workload"),
        "manual-revision": ("draftRevision", 2),
        "manual-bool-revision": ("draftRevision", True),
        "manual-policy": ("policyVersion", 1),
    }
    if case in changes:
        key, value = changes[case]
        payload[key] = value
    if case == "missing-target":
        del payload["draftId"]
    if case == "manual-missing-source":
        del payload["source"]
    if case == "record-time":
        event["occurred_at"] += timedelta(microseconds=1)
    if case == "naive-record-time":
        event["occurred_at"] = event["occurred_at"].replace(tzinfo=None)
    if case == "subminute-time":
        event["occurred_at"] = event["occurred_at"].astimezone(timezone(timedelta(seconds=30)))
    if case == "draft-with-record":
        h.row.update(status="DRAFT", archived_at=None)
    if case == "archived-no-time":
        h.row["archived_at"] = None
    if case == "backwards-archive":
        h.row["archived_at"] = h.row["last_meaningful_activity_at"] - timedelta(microseconds=1)
    if case == "bad-metadata":
        h.row["content_hash"] = "bad"
    if case == "dependency":
        h.owner.archive_records.side_effect = RuntimeError("private SQL failure")
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    readonly(h)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "INACTIVITY"),
        ("actorId", "inferred-workload"),
        ("policyVersion", 0),
        ("policyVersion", True),
        ("policyVersion", "7"),
        ("policyVersion", 7.5),
        ("missing-policy", None),
        ("missing-cutoff", None),
        ("cutoff", None),
        ("cutoff", 1),
        ("cutoff", "2026-10-01T00:00:00"),
        ("cutoff", "2026-02-30T00:00:00Z"),
        ("cutoff", "2026-10-01T00:00:00.1234567Z"),
        ("cutoff", "2026-10-01T00:00:00+00:00:30"),
        ("cutoff", "0001-01-01T00:00:00+01:00"),
        ("early", None),
        ("late", None),
    ],
)
def test_inactivity_requires_complete_actual_shape_and_exact_cutoff_order(
    field: str, value: Any
) -> None:
    h = source_state(source="INACTIVITY")
    payload = h.records[0]["payload"]
    if field == "missing-policy":
        del payload["policyVersion"]
    elif field == "missing-cutoff":
        del payload["cutoff"]
    elif field == "early":
        payload["cutoff"] = (
            h.row["last_meaningful_activity_at"] - timedelta(microseconds=1)
        ).isoformat()
    elif field == "late":
        payload["cutoff"] = (h.row["archived_at"] + timedelta(microseconds=1)).isoformat()
    else:
        payload[field] = value
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    readonly(h)


def test_a_malformed_empty_event_container_is_not_unknown_history() -> None:
    h = source_state()
    h.records = {}
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    readonly(h)
