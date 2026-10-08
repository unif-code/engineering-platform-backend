from copy import deepcopy
from typing import Any

import pytest

from control_plane.app.modules.configuration import (
    DraftNotFound,
    InvalidPolicyValue,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.application.drafts import _content_hash
from tests.configuration.test_draft_clone import clone_state
from tests.configuration.test_draft_clone import execute as clone
from tests.configuration.test_draft_rebase import request_for


def governance_state(namespace: str = "identity") -> Any:
    h = clone_state(namespace)
    origin = h.draft
    receipt = clone(h)
    h.clone_record = deepcopy(h.owner.record_clone.call_args.kwargs)
    h.draft = receipt.draft
    h.versions = {1: h.base, 2: h.current}
    h.rebases = []
    h.owner.version_snapshot.side_effect = lambda _ns, _scope, version: h.versions.get(version)

    def update(draft_id: str, **values: Any) -> Any:
        assert draft_id == h.draft.id
        h.draft = h.draft.model_copy(
            update={
                "base_version": values["base_version"],
                "content": deepcopy(values["content"]),
                "content_hash": values["content_hash"],
                "revision": h.draft.revision + 1,
                "stale": False,
                "validation_evidence": None,
                "preview_evidence": None,
            }
        )
        return h.draft

    h.owner.rebase_draft.side_effect = update
    h.owner.record_rebase.side_effect = lambda **values: h.rebases.append(deepcopy(values))
    for version in (2, 3, 4):
        current = deepcopy(h.current.values)
        if version > 2:
            current[
                "identity.session_cap" if namespace == "identity" else "draft_archive_after_days"
            ] = version + 1
        h.current = h.current.model_copy(
            update={
                "version": version,
                "values": current,
                "snapshot_hash": _content_hash(current),
            }
        )
        h.versions[version] = h.current
        h.owner.active_snapshot.return_value = h.current
        h.owner.draft.return_value = h.draft
        request = request_for(h, "DRAFT")
        h.lifecycle.apply_rebase(
            namespace=namespace,
            draft_id=h.draft.id,
            actor_id=h.draft.owner_id,
            expected_revision=h.draft.revision,
            request=request,
            raw_session="synthetic",
            authorization=h.authorization,
        )
    h.rebases.reverse()
    h.owner.draft.side_effect = lambda *_a, **_k: h.draft
    h.owner.clone_record.return_value = h.clone_record

    def page(
        _ns: str,
        _scope: str,
        _id: str,
        *,
        through_revision: int,
        before_revision: int | None,
        limit: int,
    ) -> Any:
        return [
            r
            for r in h.rebases
            if r["after_revision"] <= through_revision
            and (before_revision is None or r["after_revision"] < before_revision)
        ][:limit]

    h.owner.rebase_records.side_effect = page
    h.owner.reset_mock()
    h.audit.reset_mock()
    h.db.reset_mock()
    h.authorization.reset_mock()
    h.origin = origin
    return h


def read(h: Any, **overrides: Any) -> Any:
    assert hasattr(h.lifecycle, "governance_records"), "governance records reader is missing"
    return h.lifecycle.governance_records(
        **{
            "namespace": h.namespace,
            "draft_id": h.draft.id,
            "expected_revision": h.draft.revision,
            "limit": 2,
            "cursor": None,
            **overrides,
        }
    )


def assert_read_only(h: Any) -> None:
    assert {call[0] for call in h.owner.mock_calls} <= {
        "draft",
        "clone_record",
        "rebase_records",
        "version_snapshot",
        "catalog",
        "normalize_candidate",
    }
    assert all(call.kwargs.get("for_update") is not True for call in h.owner.mock_calls)
    assert h.db.mock_calls == [] and h.audit.mock_calls == [] and h.authorization.mock_calls == []


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_real_command_records_are_read_without_current_source_or_any_write(namespace: str) -> None:
    h = governance_state(namespace)
    before = deepcopy((h.clone_record, h.rebases, h.versions))
    h.draft = h.draft.model_copy(
        update={
            "owner_id": "later-owner",
            "content": {},
            "content_hash": "changed",
            "status": "ARCHIVED",
            "archived_at": h.draft.last_meaningful_activity_at,
        }
    )
    page = read(h)
    assert [r.after_revision for r in page.rebases] == [4, 3] and page.next_cursor == "3"
    assert page.draft_id == h.draft.id and page.draft_revision == 4
    assert page.clone_record.source.draft_id == h.origin.id
    assert page.clone_record.source_content == h.origin.content
    assert page.clone_record.source.owner_id == h.origin.owner_id
    assert page.clone_record.current_snapshot_at_operation.version == 2
    assert page.rebases[0].current_snapshot_at_operation.version == 4
    assert page.rebases[0].base_snapshot.version == 3
    for record in page.rebases:
        for choice in record.selections.values():
            assert (choice.resolution is None) == (choice.change != "CONFLICT")
    last = read(h, cursor=page.next_cursor)
    assert [r.after_revision for r in last.rebases] == [2] and last.next_cursor is None
    assert last.clone_record == page.clone_record
    assert read(h, cursor="2").rebases == []
    assert read(h, cursor="2").next_cursor is None
    h.owner.rebase_records.assert_any_call(
        h.namespace, "PLATFORM", h.draft.id, through_revision=4, before_revision=None, limit=3
    )
    assert (h.clone_record, h.rebases, h.versions) == before
    assert_read_only(h)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_empty_and_clone_only_pages_do_not_invent_other_history(namespace: str) -> None:
    h = governance_state(namespace)
    h.rebases.clear()
    assert read(h).clone_record is not None and read(h).rebases == []
    h.owner.clone_record.return_value = None
    result = read(h)
    assert result.clone_record is None and result.rebases == [] and result.next_cursor is None
    assert_read_only(h)


@pytest.mark.parametrize(
    "cursor", ["", "0", "01", "+1", "-1", " 1", "1 ", "١", "１", "1.0", "5", "9" * 5000]
)
def test_invalid_cursor_is_422_before_reading_owner(cursor: str) -> None:
    h = governance_state()
    with pytest.raises(InvalidPolicyValue):
        read(h, cursor=cursor)
    assert h.owner.mock_calls == []


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_invalid_page_size_is_422_before_reading_owner(limit: Any) -> None:
    h = governance_state()
    with pytest.raises(InvalidPolicyValue):
        read(h, limit=limit)
    assert h.owner.mock_calls == []


@pytest.mark.parametrize(
    "kind,error",
    [
        ("missing", DraftNotFound),
        ("namespace", DraftNotFound),
        ("id", DraftNotFound),
        ("revision", StaleDraftRevision),
        ("changed", StaleDraftRevision),
    ],
)
def test_target_is_checked_before_records_and_again_after_reads(
    kind: str, error: type[Exception]
) -> None:
    h = governance_state()
    if kind == "missing":
        h.owner.draft.side_effect = lambda *_a, **_k: None
    elif kind == "changed":
        h.owner.draft.side_effect = [h.draft, h.draft.model_copy(update={"revision": 5})]
    else:
        h.owner.draft.side_effect = lambda *_a, **_k: h.draft.model_copy(
            update={
                {"namespace": "namespace", "id": "id", "revision": "revision"}[kind]: 9
                if kind == "revision"
                else "elsewhere"
            }
        )
    with pytest.raises(error):
        read(h)
    if kind != "changed":
        h.owner.clone_record.assert_not_called()
    assert_read_only(h)


@pytest.mark.parametrize(
    "field,value",
    [
        ("draft_id", "elsewhere"),
        ("namespace", "wrong"),
        ("scope", "WORKSPACE"),
        ("schema_revision", 2),
        ("actor_id", ""),
        ("source_draft_id", ""),
        ("source_revision", 0),
        ("source_revision", True),
        ("source_owner_id", ""),
        ("source_status", "UNKNOWN"),
        ("source_content_hash", "a" * 64),
        ("content_hash", "b" * 64),
        ("base_snapshot_hash", "c" * 64),
        ("current_snapshot_hash", "d" * 64),
        ("current_version", 0),
        ("rollback_from_version", 0),
        ("cloned_from_archived_draft_id", "unrelated"),
        ("recorded_at", "2026-01-01"),
        ("source_content", {}),
    ],
)
def test_corrupt_clone_fact_fails_the_whole_page(field: str, value: Any) -> None:
    h = governance_state()
    h.clone_record[field] = value
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    assert_read_only(h)


@pytest.mark.parametrize(
    "case",
    [
        "target",
        "hash",
        "after-hash",
        "revision",
        "schema",
        "key",
        "change",
        "automatic",
        "resolution",
        "custom",
        "result",
        "duplicate",
        "order",
        "overflow",
        "lookahead",
    ],
)
def test_corrupt_rebase_facts_or_page_order_fail_closed(case: str) -> None:
    h = governance_state()
    record = h.rebases[0]
    if case == "target":
        record["draft_id"] = "wrong"
    elif case == "hash":
        record["before_content_hash"] = "a" * 64
    elif case == "after-hash":
        record["after_content_hash"] = "a" * 64
    elif case == "revision":
        record["before_revision"] = record["after_revision"]
    elif case == "schema":
        record["schema_revision"] = 2
    elif case == "key":
        record["selections"].pop(next(iter(record["selections"])))
    elif case == "change":
        next(iter(record["selections"].values()))["change"] = "CONFLICT"
    elif case == "automatic":
        next(v for v in record["selections"].values() if v["change"] != "CONFLICT")["source"] = (
            "DRAFT"
        )
    elif case == "resolution":
        next(v for v in record["selections"].values() if v["change"] != "CONFLICT")[
            "resolution"
        ] = {"choice": "DRAFT"}
    elif case == "custom":
        record["selections"]["identity.session_cap"] = {
            "change": "CONFLICT",
            "source": "CUSTOM",
            "resolution": {"choice": "CUSTOM", "value": True},
        }
    elif case == "result":
        record["after_content"]["identity.session_cap"] = 10
        record["after_content_hash"] = _content_hash(record["after_content"])
    elif case == "lookahead":
        h.rebases[-1]["after_content_hash"] = "a" * 64
    else:
        rows = {
            "duplicate": [record, record],
            "order": list(reversed(h.rebases)),
            "overflow": [{**record, "after_revision": 5, "before_revision": 4}],
        }[case]
        h.owner.rebase_records.side_effect = None
        h.owner.rebase_records.return_value = rows
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    assert_read_only(h)


@pytest.mark.parametrize(
    "case",
    [
        "missing-version",
        "wrong-version",
        "wrong-hash",
        "wrong-schema",
        "dependency",
        "invalid-owner-values",
    ],
)
def test_reference_or_owner_failure_is_unavailable_not_user_validation(case: str) -> None:
    h = governance_state()
    if case == "missing-version":
        h.versions.pop(1)
    elif case == "wrong-version":
        h.versions[1] = h.base.model_copy(update={"version": 9})
    elif case == "wrong-hash":
        h.versions[1] = h.base.model_copy(update={"snapshot_hash": "e" * 64})
    elif case == "wrong-schema":
        h.versions[1] = h.base.model_copy(update={"schema_revision": 2})
    elif case == "dependency":
        h.owner.clone_record.side_effect = RuntimeError("private SQL failure")
    else:
        h.owner.normalize_candidate.side_effect = InvalidPolicyValue(
            "private invalid stored content"
        )
    with pytest.raises(PolicySnapshotUnavailable):
        read(h)
    assert_read_only(h)
