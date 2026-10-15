import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from control_plane.app.modules.configuration import (
    DraftArchived,
    DraftAuthorizationDenied,
    DraftNotFound,
    DraftOwnerRequired,
    PolicyLifecycle,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from tests.configuration.test_draft_clone import clone_state


def archive_state(namespace: str = "identity") -> Any:
    h = clone_state(namespace)
    h.draft = h.draft.model_copy(update={"owner_id": "current-admin"})
    h.clock = Mock()
    h.now = h.draft.last_meaningful_activity_at + timedelta(seconds=1)
    h.clock.now.side_effect = lambda: h.now
    h.dependencies = type(h.dependencies)(
        clock=h.clock, random=h.dependencies.random, audit=h.audit
    )
    h.owner.draft.side_effect = lambda *_, **__: (h.events.append("draft"), h.draft)[1]

    def archive(**values: Any) -> bool:
        h.events.append("archive")
        if (
            h.draft.status != "DRAFT"
            or h.draft.revision != values["expected_revision"]
            or h.draft.owner_id != values["expected_owner_id"]
            or h.draft.last_meaningful_activity_at != values["expected_activity"]
        ):
            return False
        h.draft = h.draft.model_copy(
            update={
                "status": "ARCHIVED",
                "archived_at": values["archived_at"],
                "validation_evidence": None,
                "preview_evidence": None,
            }
        )
        return True

    h.owner.archive_draft.side_effect = archive
    h.lifecycle = PolicyLifecycle(h.db, h.owner, h.dependencies)
    return h


def execute(h: Any, **values: Any) -> Any:
    assert hasattr(h.lifecycle, "archive_draft"), "manual archive command is missing"
    return h.lifecycle.archive_draft(
        **{
            "namespace": h.namespace,
            "draft_id": "known-draft",
            "actor_id": "current-admin",
            "expected_revision": 7,
            "raw_session": "private-original-session",
            "authorization": h.authorization,
            **values,
        }
    )


def no_other_owner_operations(h: Any) -> None:
    assert {call[0] for call in h.owner.mock_calls} <= {"draft", "archive_draft"}


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("kind", ["saved", "stale", "rollback", "invalid-candidate"])
def test_owner_archive_uses_one_time_and_existing_cas_without_active_or_candidate_gates(
    namespace: str, kind: str
) -> None:
    h = archive_state(namespace)
    if kind == "stale":
        h.draft = h.draft.model_copy(update={"stale": True})
    if kind == "rollback":
        h.draft = h.draft.model_copy(update={"rollback_from_version": 1})
    if kind == "invalid-candidate":
        h.draft = h.draft.model_copy(update={"content": {"private-invalid-candidate": True}})
    before = deepcopy(h.draft)
    result = execute(h)
    assert result.model_dump() == {
        "draft_id": before.id,
        "namespace": namespace,
        "scope": "PLATFORM",
        "draft_revision": before.revision,
        "status": "ARCHIVED",
        "archived_at": h.now,
    }
    assert h.events == ["draft", "authorization", "archive", "audit"]
    h.clock.now.assert_called_once_with()
    h.owner.draft.assert_called_once_with(before.id, for_update=True)
    h.authorization.check.assert_called_once_with(
        raw_session="private-original-session", actor_id="current-admin"
    )
    changed = {"status", "archived_at", "validation_evidence", "preview_evidence"}
    assert h.draft.model_dump(exclude=changed) == before.model_dump(exclude=changed)
    assert h.draft.validation_evidence is None and h.draft.preview_evidence is None
    call = h.owner.archive_draft.call_args.kwargs
    assert {
        key: call[key]
        for key in (
            "draft_id",
            "namespace",
            "scope",
            "expected_revision",
            "expected_owner_id",
            "expected_activity",
        )
    } == {
        "draft_id": before.id,
        "namespace": namespace,
        "scope": "PLATFORM",
        "expected_revision": 7,
        "expected_owner_id": "current-admin",
        "expected_activity": before.last_meaningful_activity_at,
    }
    payload = call["outbox_payload"]
    assert payload["source"] == "MANUAL" and payload["actorId"] == "current-admin"
    assert payload["result"] == "ARCHIVED" and payload["draftRevision"] == 7
    assert "policyVersion" not in payload and "cutoff" not in payload
    envelope = h.audit.append_in_transaction.call_args.args[1]
    assert envelope.occurred_at == h.now and envelope.actor == "current-admin"
    assert envelope.actor_type == "human" and envelope.action == "configuration.draft.archived"
    assert envelope.target_id == before.id and envelope.result == "SUCCESS"
    reason = json.loads(envelope.reason)
    assert reason["source"] == "MANUAL" and reason["draftRevision"] == 7
    assert "private-" not in str(payload) + envelope.reason
    no_other_owner_operations(h)


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize(
    "case,error",
    [
        ("missing", DraftNotFound),
        ("id", DraftNotFound),
        ("namespace", DraftNotFound),
        ("revision", StaleDraftRevision),
        ("owner", DraftOwnerRequired),
        ("archived", DraftArchived),
        ("scope", PolicySnapshotUnavailable),
        ("status", PolicySnapshotUnavailable),
    ],
)
def test_locked_target_checks_reject_without_archival_or_audit(
    namespace: str, case: str, error: type[Exception]
) -> None:
    h = archive_state(namespace)
    changes: dict[str, dict[str, Any]] = {
        "id": {"id": "another-id"},
        "namespace": {"namespace": "other"},
        "revision": {"revision": 8},
        "owner": {"owner_id": "another-owner"},
        "archived": {"status": "ARCHIVED", "archived_at": h.now},
        "scope": {"scope": "WORKSPACE"},
        "status": {"status": "unknown"},
    }
    if case == "missing":
        h.owner.draft.side_effect = lambda *_, **__: None
    else:
        h.draft = h.draft.model_copy(update=changes[case])
    with pytest.raises(error):
        execute(h)
    h.owner.archive_draft.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()
    h.clock.now.assert_not_called()
    no_other_owner_operations(h)


@pytest.mark.parametrize("case", ["missing", "401", "403", "unavailable", "unexpected"])
def test_current_authorization_is_rechecked_after_target_lock_before_any_write(case: str) -> None:
    h = archive_state()
    if case == "missing":
        h.authorization = None
    else:
        h.authorization.check.side_effect = {
            "401": DraftAuthorizationDenied(401),
            "403": DraftAuthorizationDenied(403),
            "unavailable": PolicySnapshotUnavailable("private authority"),
            "unexpected": RuntimeError("private authority"),
        }[case]
    error = DraftAuthorizationDenied if case in {"401", "403"} else PolicySnapshotUnavailable
    with pytest.raises(error):
        execute(h)
    assert h.events == ([] if case == "missing" else ["draft"])
    h.owner.archive_draft.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()


@pytest.mark.parametrize(
    "field,value", [("revision", 8), ("owner_id", "other"), ("status", "ARCHIVED")]
)
def test_revoked_session_is_not_hidden_by_target_changes_during_lock_wait(
    field: str, value: Any
) -> None:
    h = archive_state()
    h.draft = h.draft.model_copy(update={field: value})
    h.authorization.check.side_effect = DraftAuthorizationDenied(401)
    with pytest.raises(DraftAuthorizationDenied) as error:
        execute(h)
    assert error.value.status_code == 401
    h.owner.archive_draft.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()


@pytest.mark.parametrize("change", ["revision", "owner", "activity", "archive"])
def test_cas_loss_does_not_emit_a_false_success_or_audit(change: str) -> None:
    h = archive_state()
    original = h.authorization.check.side_effect

    def changed(**values: Any) -> None:
        original(**values)
        changes = {
            "revision": {"revision": 8},
            "owner": {"owner_id": "other"},
            "activity": {"last_meaningful_activity_at": h.now},
            "archive": {"status": "ARCHIVED", "archived_at": h.now},
        }
        h.draft = h.draft.model_copy(update=changes[change])

    h.authorization.check.side_effect = changed
    with pytest.raises(StaleDraftRevision):
        execute(h)
    h.audit.append_in_transaction.assert_not_called()
    no_other_owner_operations(h)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-10-15T00:00:00Z",
        datetime(2026, 10, 15),
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(2026, 10, 15, tzinfo=timezone(timedelta(seconds=30))),
    ],
)
def test_invalid_or_backwards_archive_clock_cannot_change_facts(value: Any) -> None:
    h = archive_state()
    h.now = value
    with pytest.raises(PolicySnapshotUnavailable):
        execute(h)
    h.owner.archive_draft.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()
