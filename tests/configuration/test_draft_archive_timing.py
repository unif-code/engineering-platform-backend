from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from control_plane.app.modules.configuration import (
    DraftNotFound,
    PolicyLifecycle,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.application.drafts import _content_hash
from tests.configuration.test_draft_directory import directory_state


def timing_state(namespace: str = "identity") -> Any:
    h = directory_state(namespace)
    h.row = deepcopy(h.rows[0])
    h.interval = timedelta(days=30)
    h.now = h.row["last_meaningful_activity_at"] + h.interval
    h.clock = Mock()
    h.clock.now.side_effect = lambda: h.now
    h.dependencies = SimpleNamespace(clock=h.clock)
    h.owner.read_archive_settings.side_effect = lambda *_: (h.current, h.interval)
    h.owner.draft_summary.side_effect = lambda *_: deepcopy(h.row)
    h.lifecycle = PolicyLifecycle(h.db, h.owner, h.dependencies)
    return h


def read(h: Any, **values: Any) -> Any:
    assert hasattr(h.lifecycle, "archive_timing"), "archive timing query is missing"
    return h.lifecycle.archive_timing(
        **(
            dict(namespace=h.namespace, draft_id=h.row["id"], expected_revision=h.row["revision"])
            | values
        )
    )


def readonly(h: Any) -> None:
    assert {call[0] for call in h.owner.mock_calls} <= {"read_archive_settings", "draft_summary"}
    assert all(call.kwargs.get("for_update") is not True for call in h.owner.mock_calls)
    assert h.db.mock_calls == []


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("offset", [0, 330, -480])
@pytest.mark.parametrize("difference,elapsed", [(-1, False), (0, True), (1, True)])
def test_timing_uses_current_period_one_clock_and_exact_microsecond_boundary(
    namespace: str, offset: int, difference: int, elapsed: bool
) -> None:
    h = timing_state(namespace)
    activity = datetime(2026, 10, 1, 8, 10, 20, 123456, tzinfo=timezone(timedelta(minutes=offset)))
    h.row["last_meaningful_activity_at"] = activity
    h.row["base_version"] = 1
    expected = activity + timedelta(days=30)
    h.now = (expected + timedelta(microseconds=difference)).astimezone(timezone(timedelta(hours=9)))
    before = deepcopy((h.current, h.row))
    result = read(h)
    assert result.draft.base_behind is True and result.current == h.current
    assert result.archive_after_days == 30 and result.expected_archive_at == expected
    assert result.expected_archive_at.isoformat() == expected.isoformat()
    assert (
        result.observed_at.isoformat() == h.now.isoformat() and result.inactivity_elapsed is elapsed
    )
    assert h.owner.read_archive_settings.call_count == h.owner.draft_summary.call_count == 2
    h.clock.now.assert_called_once_with()
    assert (h.current, h.row) == before
    readonly(h)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("month,day,expected_hour", [(3, 7, 13), (10, 31, 11)])
def test_one_day_is_24_hours_across_dst_not_a_calendar_day(
    namespace: str, month: int, day: int, expected_hour: int
) -> None:
    h = timing_state(namespace)
    h.interval = timedelta(days=1)
    activity = datetime(2026, month, day, 12, 0, 0, 654321, tzinfo=ZoneInfo("America/New_York"))
    h.row["last_meaningful_activity_at"] = activity
    h.now = activity.astimezone(UTC) + timedelta(days=1)
    result = read(h)
    assert result.expected_archive_at.hour == expected_hour
    assert result.expected_archive_at.microsecond == 654321
    assert result.expected_archive_at.astimezone(UTC) - activity.astimezone(UTC) == timedelta(
        hours=24
    )
    assert result.inactivity_elapsed is True
    readonly(h)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_archived_has_required_null_calculations_even_when_future_addition_would_overflow(
    namespace: str,
) -> None:
    h = timing_state(namespace)
    h.row.update(
        status="ARCHIVED",
        last_meaningful_activity_at=datetime(9999, 12, 1, tzinfo=UTC),
        archived_at=datetime(9999, 12, 31, tzinfo=UTC),
    )
    result = read(h)
    assert result.expected_archive_at is None and result.inactivity_elapsed is None
    assert result.draft.archived_at == h.row["archived_at"] and result.archive_after_days == 30
    h.clock.now.assert_called_once_with()
    readonly(h)


@pytest.mark.parametrize(
    "case,error",
    [
        ("missing", DraftNotFound),
        ("namespace", DraftNotFound),
        ("scope", DraftNotFound),
        ("id", DraftNotFound),
        ("revision", StaleDraftRevision),
        ("changed-revision", StaleDraftRevision),
        ("changed-owner", StaleDraftRevision),
        ("changed-activity", StaleDraftRevision),
        ("changed-archive", StaleDraftRevision),
        ("changed-hash", StaleDraftRevision),
        ("changed-base", StaleDraftRevision),
        ("changed-rollback", StaleDraftRevision),
        ("disappeared", StaleDraftRevision),
    ],
)
def test_first_target_and_all_changed_metadata_are_checked(
    case: str, error: type[Exception]
) -> None:
    h = timing_state()
    if case == "missing":
        h.owner.draft_summary.side_effect = lambda *_: None
    elif case in {"namespace", "scope", "id"}:
        h.owner.draft_summary.side_effect = lambda *_: {**h.row, case: "wrong"}
    elif case == "revision":
        h.owner.draft_summary.side_effect = lambda *_: {**h.row, "revision": 2}
    else:
        updates: dict[str, dict[str, Any]] = {
            "changed-revision": {"revision": 2},
            "changed-owner": {"owner_id": "other-current-owner"},
            "changed-activity": {
                "last_meaningful_activity_at": h.row["last_meaningful_activity_at"]
                + timedelta(microseconds=1)
            },
            "changed-archive": {"status": "ARCHIVED", "archived_at": h.now},
            "changed-hash": {"content_hash": "a" * 64},
            "changed-base": {"base_version": 1},
            "changed-rollback": {"rollback_from_version": 1},
        }
        h.owner.draft_summary.side_effect = [
            h.row,
            None if case == "disappeared" else {**h.row, **updates[case]},
        ]
    with pytest.raises(error):
        read(h)
    h.clock.now.assert_not_called()
    readonly(h)


def test_publication_is_409_before_future_base_validation() -> None:
    h = timing_state()
    h.row["base_version"] = 3
    h.owner.read_archive_settings.side_effect = [
        (h.current, h.interval),
        (h.current.model_copy(update={"version": 3}), h.interval),
    ]
    with pytest.raises(StaleDraftRevision):
        read(h)
    h.clock.now.assert_not_called()
    readonly(h)


@pytest.mark.parametrize(
    "case",
    [
        "schema",
        "hash",
        "namespace",
        "scope",
        "version",
        "missing",
        "same-version-hash",
        "same-version-period",
        "dependency",
    ],
)
def test_bad_policy_or_same_version_facts_are_unavailable(case: str) -> None:
    h = timing_state()
    if case == "missing":
        h.owner.read_archive_settings.side_effect = lambda *_: (None, h.interval)
    elif case == "dependency":
        h.owner.draft_summary.side_effect = RuntimeError("private SELECT")
    elif case == "same-version-period":
        h.owner.read_archive_settings.side_effect = [
            (h.current, h.interval),
            (h.current, timedelta(days=1)),
        ]
    elif case == "same-version-hash":
        values = {**h.current.values, "identity.session_cap": 6}
        changed = h.current.model_copy(
            update={"values": values, "snapshot_hash": _content_hash(values)}
        )
        h.owner.read_archive_settings.side_effect = [(h.current, h.interval), (changed, h.interval)]
    else:
        key, value = {
            "schema": ("schema_revision", 2),
            "hash": ("snapshot_hash", "a" * 64),
            "namespace": ("namespace", "wrong"),
            "scope": ("scope", "WORKSPACE"),
            "version": ("version", 0),
        }[case]
        h.current = h.current.model_copy(update={key: value})
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    h.clock.now.assert_not_called()
    readonly(h)


@pytest.mark.parametrize(
    "interval",
    [
        None,
        1,
        True,
        timedelta(0),
        timedelta(days=-1),
        timedelta(seconds=1),
        timedelta(days=1, microseconds=1),
    ],
)
def test_period_must_be_positive_whole_days(interval: Any) -> None:
    h = timing_state()
    h.interval = interval
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    h.clock.now.assert_not_called()
    readonly(h)


@pytest.mark.parametrize(
    "case",
    [
        "naive-clock",
        "bad-clock",
        "naive-activity",
        "overflow",
        "utc-overflow",
        "clock-utc-overflow",
        "bad-schema",
        "bad-lifecycle",
    ],
)
def test_unrepresentable_or_corrupt_time_fails_without_a_partial_answer(case: str) -> None:
    h = timing_state()
    if case == "naive-clock":
        h.now = h.now.replace(tzinfo=None)
    if case == "bad-clock":
        h.now = "2026-10-08T00:00:00Z"
    if case == "naive-activity":
        h.row["last_meaningful_activity_at"] = h.now.replace(tzinfo=None)
    if case == "overflow":
        h.row["last_meaningful_activity_at"] = datetime(9999, 12, 31, tzinfo=UTC)
    if case == "utc-overflow":
        h.row["last_meaningful_activity_at"] = datetime(
            1, 1, 1, tzinfo=timezone(timedelta(hours=8))
        )
    if case == "clock-utc-overflow":
        h.now = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=8)))
    if case == "bad-schema":
        h.row["schema_revision"] = 2
    if case == "bad-lifecycle":
        h.row["archived_at"] = h.now
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    readonly(h)


@pytest.mark.parametrize("field", ["activity", "archived", "clock"])
@pytest.mark.parametrize("offset", [timedelta(seconds=30), timedelta(microseconds=1)])
def test_subminute_offsets_cannot_be_silently_truncated_in_json(
    field: str, offset: timedelta
) -> None:
    h = timing_state()
    if field == "activity":
        h.row["last_meaningful_activity_at"] = h.row["last_meaningful_activity_at"].astimezone(
            timezone(offset)
        )
    elif field == "archived":
        h.row.update(status="ARCHIVED", archived_at=h.now.astimezone(timezone(offset)))
    else:
        h.now = h.now.astimezone(timezone(offset))
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    readonly(h)
