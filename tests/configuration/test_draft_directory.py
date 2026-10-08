from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest

from control_plane.app.modules.configuration import (
    InvalidPolicyValue,
    PolicyLifecycle,
    PolicySnapshotUnavailable,
    StaleDraftBase,
)
from control_plane.app.modules.configuration.application.drafts import _content_hash
from tests.configuration.test_draft_base_comparison import NOW, comparison_state

ACTOR = "20000000-0000-4000-8000-000000000001"
OTHER = "20000000-0000-4000-8000-000000000002"


def directory_state(namespace: str = "identity") -> Any:
    h = comparison_state(namespace)
    h.rows = []
    for index, (owner_id, status, base) in enumerate(
        [
            (ACTOR, "DRAFT", 2),
            (OTHER, "DRAFT", 1),
            (ACTOR, "ARCHIVED", 1),
            (ACTOR, "DRAFT", 1),
            (OTHER, "ARCHIVED", 2),
            (OTHER, "DRAFT", 2),
        ],
        1,
    ):
        h.rows.append(
            dict(
                id=f"81000000-0000-4000-8000-{index:012}",
                namespace=namespace,
                scope="PLATFORM",
                owner_id=owner_id,
                revision=index,
                status=status,
                base_version=base,
                schema_revision=1,
                content_hash=h.draft.content_hash,
                last_meaningful_activity_at=NOW - timedelta(days=index),
                archived_at=NOW if status == "ARCHIVED" else None,
                rollback_from_version=1 if index == 3 else None,
            )
        )
    h.owner.active_snapshot.side_effect = lambda *_: h.current
    h.calls = []

    def summaries(
        name: str,
        scope: str,
        *,
        view: str,
        owner_id: str | None,
        current_version: int,
        after_id: str | None,
        limit: int,
    ) -> Any:
        h.calls.append(
            dict(
                namespace=name,
                scope=scope,
                view=view,
                owner_id=owner_id,
                current_version=current_version,
                after_id=after_id,
                limit=limit,
            )
        )
        return deepcopy(
            [
                r
                for r in h.rows
                if (owner_id is None or r["owner_id"] == owner_id)
                and (after_id is None or r["id"] > after_id)
                and (
                    view == "ALL"
                    or (view == "ARCHIVED" and r["status"] == "ARCHIVED")
                    or (
                        view == "ACTIVE"
                        and r["status"] == "DRAFT"
                        and r["base_version"] == current_version
                    )
                    or (
                        view == "STALE"
                        and r["status"] == "DRAFT"
                        and r["base_version"] < current_version
                    )
                )
            ][:limit]
        )

    h.owner.list_draft_summaries.side_effect = summaries
    h.lifecycle = PolicyLifecycle(h.db, h.owner, h.dependencies)
    return h


def read(h: Any, **values: Any) -> Any:
    assert hasattr(h.lifecycle, "draft_directory"), "draft directory is missing"
    args = dict(
        namespace=h.namespace,
        actor_id=ACTOR,
        view="ALL",
        owner="ALL",
        limit=50,
        cursor=None,
        current_version=None,
    )
    return h.lifecycle.draft_directory(**(args | values))


def readonly(h: Any) -> None:
    assert {c[0] for c in h.owner.mock_calls} <= {"active_snapshot", "list_draft_summaries"}
    assert all(c.kwargs.get("for_update") is not True for c in h.owner.mock_calls)
    assert h.db.mock_calls == [] and h.dependencies.mock_calls == []


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("view", ["ALL", "ACTIVE", "STALE", "ARCHIVED"])
@pytest.mark.parametrize("owner", ["ALL", "MINE"])
def test_directory_filters_metadata_with_observed_current_and_trusted_actor(
    namespace: str, view: str, owner: str
) -> None:
    h = directory_state(namespace)
    before = deepcopy(h.rows)
    result = read(h, view=view, owner=owner)
    assert result.namespace == namespace and result.scope == "PLATFORM"
    assert result.view == view and result.owner == owner and result.current_version == 2
    assert result.next_cursor is None
    assert h.calls == [
        dict(
            namespace=namespace,
            scope="PLATFORM",
            view=view,
            owner_id=ACTOR if owner == "MINE" else None,
            current_version=2,
            after_id=None,
            limit=51,
        )
    ]
    for item in result.items:
        original = next(r for r in h.rows if r["id"] == item.id)
        assert item.model_dump(exclude={"base_behind"}) == original
        assert item.base_behind is (original["base_version"] < 2)
        if owner == "MINE":
            assert item.owner_id == ACTOR
        if view == "ACTIVE":
            assert item.status == "DRAFT" and not item.base_behind
        if view == "STALE":
            assert item.status == "DRAFT" and item.base_behind
        if view == "ARCHIVED":
            assert item.status == "ARCHIVED"
    assert h.rows == before
    assert h.owner.active_snapshot.call_count == 2
    readonly(h)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_uuid_pages_have_strict_lower_bound_and_mutable_empty_continuation_is_legal(
    namespace: str,
) -> None:
    h = directory_state(namespace)
    first = read(h, limit=1)
    assert first.next_cursor == h.rows[0]["id"] and len(first.items) == 1
    second = read(h, limit=1, cursor=first.next_cursor, current_version=2)
    assert second.items[0].id == h.rows[1]["id"] and second.next_cursor == h.rows[1]["id"]
    h.rows.clear()
    empty = read(h, limit=1, cursor=second.next_cursor, current_version=2)
    assert empty.items == [] and empty.next_cursor is None
    readonly(h)


@pytest.mark.parametrize(
    "values",
    [
        {"view": "active"},
        {"view": "OTHER"},
        {"owner": "other-owner"},
        {"owner": "mine"},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 1.5},
        {"cursor": ""},
        {"cursor": "81000000-0000-4000-8000-000000000001"},
        {"cursor": "81000000-0000-4000-8AAA-000000000001", "current_version": 2},
        {"cursor": "81000000000040008000000000000001", "current_version": 2},
        {"cursor": " 81000000-0000-4000-8000-000000000001", "current_version": 2},
        {"cursor": "not-uuid", "current_version": 2},
        {"current_version": 0},
        {"current_version": True},
        {"current_version": 1.5},
    ],
)
def test_bad_query_fails_before_owner_reads(values: Any) -> None:
    h = directory_state()
    with pytest.raises(InvalidPolicyValue):
        read(h, **values)
    assert h.owner.mock_calls == []


def test_old_expected_current_refuses_before_summary_scan() -> None:
    h = directory_state()
    with pytest.raises(StaleDraftBase):
        read(h, current_version=1)
    h.owner.list_draft_summaries.assert_not_called()
    readonly(h)


def test_publication_during_read_is_checked_before_future_base_rows() -> None:
    h = directory_state()
    h.rows[0]["base_version"] = 3
    h.owner.active_snapshot.side_effect = [h.current, h.current.model_copy(update={"version": 3})]
    with pytest.raises(StaleDraftBase):
        read(h)
    readonly(h)


@pytest.mark.parametrize(
    "case",
    ["hash", "namespace", "scope", "schema", "version", "missing", "changed-hash", "dependency"],
)
def test_untrusted_current_fails_closed(case: str) -> None:
    h = directory_state()
    if case == "missing":
        h.owner.active_snapshot.side_effect = lambda *_: None
    elif case == "dependency":
        h.owner.list_draft_summaries.side_effect = RuntimeError("private SQL")
    elif case == "changed-hash":
        content = {**h.current.values, "identity.session_cap": 6}
        h.owner.active_snapshot.side_effect = [
            h.current,
            h.current.model_copy(
                update={"values": content, "snapshot_hash": _content_hash(content)}
            ),
        ]
    else:
        fields = {
            "hash": ("snapshot_hash", "bad"),
            "namespace": ("namespace", "elsewhere"),
            "scope": ("scope", "WORKSPACE"),
            "schema": ("schema_revision", 2),
            "version": ("version", 0),
        }
        key, value = fields[case]
        h.current = h.current.model_copy(update={key: value})
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    readonly(h)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "wrong"),
        ("id", "81000000-0000-4000-8AAA-000000000001"),
        ("namespace", "wrong"),
        ("scope", "WORKSPACE"),
        ("owner_id", ""),
        ("revision", 0),
        ("revision", True),
        ("revision", "1"),
        ("status", "OTHER"),
        ("base_version", 3),
        ("base_version", 0),
        ("schema_revision", 2),
        ("content_hash", "F" * 64),
        ("last_meaningful_activity_at", NOW.replace(tzinfo=None)),
        ("archived_at", NOW),
        ("rollback_from_version", 0),
        ("content", {"secret": "should-not-read"}),
    ],
)
def test_invalid_metadata_or_extraneous_body_is_whole_page_unavailable(
    field: str, value: Any
) -> None:
    h = directory_state()
    h.rows[0][field] = value
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    readonly(h)


@pytest.mark.parametrize(
    "case",
    [
        "archived-null",
        "archived-before-activity",
        "duplicate",
        "unordered",
        "lower-bound",
        "lookahead",
        "wrong-owner",
        "wrong-view",
    ],
)
def test_all_rows_including_lookahead_obey_state_filter_order_and_boundary(case: str) -> None:
    h = directory_state()
    args: dict[str, Any] = {}
    if case == "archived-null":
        h.rows[2]["archived_at"] = None
    elif case == "archived-before-activity":
        h.rows[2]["archived_at"] = NOW - timedelta(days=9)
    elif case == "lookahead":
        h.rows[1]["schema_revision"] = 2
        args = {"limit": 1}
    else:
        rows = deepcopy(h.rows[:2])
        if case == "duplicate":
            rows[1] = deepcopy(rows[0])
        if case == "unordered":
            rows.reverse()
        if case == "lower-bound":
            args = {"cursor": rows[0]["id"], "current_version": 2}
        if case == "wrong-owner":
            args = {"owner": "MINE"}
        if case == "wrong-view":
            args = {"view": "ACTIVE"}
        h.owner.list_draft_summaries.side_effect = None
        h.owner.list_draft_summaries.return_value = rows
    with pytest.raises(PolicySnapshotUnavailable):
        read(h, **args)
    readonly(h)
