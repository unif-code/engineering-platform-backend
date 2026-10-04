import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from control_plane.app.modules.configuration import (
    ConfigurationDependencies,
    DraftArchived,
    DraftNotFound,
    DraftOwnerRequired,
    InvalidPolicyValue,
    PolicyLifecycle,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.application.base_comparison import compare_draft_base
from control_plane.app.modules.configuration.application.drafts import _content_hash
from tests.configuration.test_draft_base_comparison import NOW, comparison_state


def request_for(h: Any, choice: str = "CURRENT", value: Any = 6) -> dict[str, Any]:
    observation = compare_draft_base(
        h.owner, namespace=h.namespace, draft_id=h.draft.id, expected_revision=h.draft.revision
    )
    result = {
        key: getattr(observation, field)
        for key, field in {
            "baseVersion": "base_version",
            "currentVersion": "current_version",
            "schemaRevision": "schema_revision",
            "baseSnapshotHash": "base_snapshot_hash",
            "currentSnapshotHash": "current_snapshot_hash",
            "draftContentHash": "draft_content_hash",
        }.items()
    }
    result["resolutions"] = {
        item.key: {"choice": choice, **({"value": value} if choice == "CUSTOM" else {})}
        for item in observation.items
        if item.change == "CONFLICT"
    }
    h.owner.reset_mock()
    return result


def rebase_state(namespace: str = "identity") -> Any:
    h = comparison_state(namespace)
    h.events = []
    h.authorization = Mock()
    h.authorization.check.side_effect = lambda **_kwargs: h.events.append("authorization")
    h.audit = Mock()
    h.audit.append_in_transaction.side_effect = lambda *_args: h.events.append("audit")
    h.dependencies = ConfigurationDependencies(
        clock=SimpleNamespace(now=lambda: NOW + timedelta(seconds=1)),
        random=SimpleNamespace(uuid4=uuid4),
        audit=h.audit,
    )
    h.owner.locked_active_snapshot.side_effect = lambda *_args: (
        h.events.append("active"),
        h.current,
    )[1]
    h.owner.draft.side_effect = lambda *_args, **_kwargs: (
        h.events.append("draft"),
        h.owner.draft.return_value,
    )[1]

    def update(draft_id: str, **values: Any) -> Any:
        h.events.append("update")
        assert draft_id == h.draft.id
        assert h.events.index("authorization") < h.events.index("update")
        return h.draft.model_copy(
            update={
                "content": deepcopy(values["content"]),
                "content_hash": values["content_hash"],
                "base_version": values["base_version"],
                "revision": h.draft.revision + 1,
                "stale": False,
                "last_meaningful_activity_at": values["now"],
                "validation_evidence": None,
                "preview_evidence": None,
            }
        )

    h.owner.rebase_draft.side_effect = update
    h.owner.record_rebase.side_effect = lambda **_kwargs: h.events.append("history")
    h.lifecycle = PolicyLifecycle(h.db, h.owner, h.dependencies)
    h.request = request_for(h)
    h.events.clear()
    return h


def execute(h: Any, **overrides: Any) -> Any:
    assert hasattr(h.lifecycle, "apply_rebase"), "explicit rebase is missing"
    return h.lifecycle.apply_rebase(
        **{
            "namespace": h.namespace,
            "draft_id": h.draft.id,
            "actor_id": h.draft.owner_id,
            "expected_revision": h.draft.revision,
            "request": h.request,
            "raw_session": "session-must-never-be-recorded",
            "authorization": h.authorization,
            **overrides,
        }
    )


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("choice", ["CURRENT", "DRAFT", "CUSTOM"])
def test_rebase_locks_active_then_draft_normalizes_full_candidate_and_records_actual_sources(
    namespace: str, choice: str
) -> None:
    h = rebase_state(namespace)
    h.request = request_for(h, choice, 6 if namespace == "identity" else 15)
    before = deepcopy((h.draft, h.base, h.current, h.request))
    h.events.clear()
    result = execute(h)
    assert h.events[:2] == ["active", "draft"]
    assert h.events[-4:] == ["authorization", "update", "history", "audit"]
    assert h.owner.draft.call_args_list[0].kwargs == {"for_update": True}
    h.owner.locked_active_snapshot.assert_called_once_with(namespace)
    h.owner.active_snapshot.assert_not_called()
    assert result.id == h.draft.id and result.owner_id == h.draft.owner_id
    assert result.base_version == 2 and result.revision == 8 and result.stale is False
    assert result.last_meaningful_activity_at == NOW + timedelta(seconds=1)
    assert result.validation_evidence is None and result.preview_evidence is None
    assert (
        result.schema_revision == h.draft.schema_revision
        and result.rollback_from_version == h.draft.rollback_from_version
    )
    conflict_key = "identity.session_cap" if namespace == "identity" else "draft_archive_after_days"
    expected = (
        h.current.values[conflict_key]
        if choice == "CURRENT"
        else h.draft.content[conflict_key]
        if choice == "DRAFT"
        else 6
        if namespace == "identity"
        else 15
    )
    assert result.content[conflict_key] == expected and result.content_hash == _content_hash(
        result.content
    )
    if namespace == "identity":
        assert result.content["identity.temp_credential_ttl"] == 48
        assert result.content["identity.session_idle_timeout"] == 45
        assert result.content["identity.password_max_age"] == 90
    record = h.owner.record_rebase.call_args.kwargs
    assert record["before_content"] == h.draft.content and record["after_content"] == result.content
    assert record["before_revision"] == 7 and record["after_revision"] == 8
    assert record["base_version"] == 1 and record["current_version"] == 2
    assert set(record["selections"]) == set(result.content)
    assert record["selections"][conflict_key]["source"] == choice
    assert (
        record["selections"][conflict_key]["resolution"] == h.request["resolutions"][conflict_key]
    )
    h.authorization.check.assert_called_once_with(
        raw_session="session-must-never-be-recorded", actor_id=h.draft.owner_id
    )
    assert "session-must-never-be-recorded" not in str(record)
    envelope = h.audit.append_in_transaction.call_args.args[1]
    assert envelope.action == "configuration.draft.rebased"
    assert "session-must-never-be-recorded" not in envelope.reason
    audit_data = json.loads(envelope.reason)
    assert "before_content" not in audit_data and "after_content" not in audit_data
    assert audit_data["before_content_hash"] == h.draft.content_hash
    assert audit_data["after_content_hash"] == result.content_hash
    assert all(set(item) == {"change", "source"} for item in audit_data["selections"].values())
    assert (h.draft, h.base, h.current, h.request) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("baseVersion", 2),
        ("currentVersion", 3),
        ("schemaRevision", 2),
        ("baseSnapshotHash", "a" * 64),
        ("currentSnapshotHash", "b" * 64),
        ("draftContentHash", "c" * 64),
    ],
)
def test_changed_request_binding_refuses_before_authorization_and_write(
    field: str, value: Any
) -> None:
    h = rebase_state()
    h.request[field] = value
    with pytest.raises(StaleDraftRevision):
        execute(h)
    h.authorization.check.assert_not_called()
    h.owner.rebase_draft.assert_not_called()
    h.owner.record_rebase.assert_not_called()


@pytest.mark.parametrize(
    "case,error",
    [
        ("missing", DraftNotFound),
        ("namespace", DraftNotFound),
        ("revision", StaleDraftRevision),
        ("owner", DraftOwnerRequired),
        ("archived", DraftArchived),
        ("current-base", StaleDraftRevision),
    ],
)
def test_rebase_refuses_missing_old_revision_nonowner_archived_or_no_base_advance(
    case: str, error: type[Exception]
) -> None:
    h = rebase_state()
    if case == "missing":
        h.owner.draft.return_value = None
    if case == "namespace":
        h.owner.draft.return_value = h.draft.model_copy(update={"namespace": "other"})
    if case == "revision":
        h.owner.draft.return_value = h.draft.model_copy(
            update={"revision": 8, "owner_id": "another"}
        )
    if case == "owner":
        h.owner.draft.return_value = h.draft.model_copy(update={"owner_id": "another"})
    if case == "archived":
        h.owner.draft.return_value = h.draft.model_copy(
            update={"status": "ARCHIVED", "archived_at": NOW}
        )
    if case == "current-base":
        h.current = h.base
    with pytest.raises(error):
        execute(h)
    h.authorization.check.assert_not_called()
    h.owner.rebase_draft.assert_not_called()
    h.owner.record_rebase.assert_not_called()


@pytest.mark.parametrize(
    "resolutions",
    [
        {},
        {"unknown": {"choice": "CURRENT"}},
        {
            "identity.session_cap": {"choice": "CURRENT"},
            "identity.temp_credential_ttl": {"choice": "DRAFT"},
        },
        {"identity.session_cap": {"choice": "UNKNOWN"}},
        {"identity.session_cap": {"choice": "CUSTOM", "value": True}},
        {"identity.session_cap": {"choice": "CUSTOM", "value": "6"}},
    ],
)
def test_conflict_set_and_complete_owner_candidate_cannot_be_bypassed(
    resolutions: dict[str, Any],
) -> None:
    h = rebase_state()
    h.request["resolutions"] = resolutions
    with pytest.raises(InvalidPolicyValue):
        execute(h)
    h.authorization.check.assert_not_called()
    h.owner.rebase_draft.assert_not_called()


def test_object_custom_is_atomic_and_cross_field_invalid_candidate_is_refused() -> None:
    h = rebase_state()
    current, draft = deepcopy(h.current.values), deepcopy(h.draft.content)
    current["identity.login_backoff"]["initialDelaySeconds"] = 60
    draft["identity.login_backoff"]["maximumDelaySeconds"] = 1200
    h.current = h.current.model_copy(
        update={"values": current, "snapshot_hash": _content_hash(current)}
    )
    h.draft = h.draft.model_copy(update={"content": draft, "content_hash": _content_hash(draft)})
    h.owner.active_snapshot.return_value = h.current
    h.owner.draft.return_value = h.draft
    h.request = request_for(h)
    h.request["resolutions"]["identity.login_backoff"] = {
        "choice": "CUSTOM",
        "value": {
            "failureThreshold": 5,
            "initialDelaySeconds": 901,
            "maximumDelaySeconds": 900,
            "resetAfterHours": 24,
        },
    }
    with pytest.raises(InvalidPolicyValue):
        execute(h)
    h.authorization.check.assert_not_called()
    h.owner.rebase_draft.assert_not_called()


def test_missing_or_failing_current_session_check_cannot_write_any_rebase_fact() -> None:
    h = rebase_state()
    with pytest.raises(PolicySnapshotUnavailable):
        execute(h, authorization=None)
    h.owner.rebase_draft.assert_not_called()
    h.authorization.check.side_effect = PolicySnapshotUnavailable("Authorization unavailable")
    with pytest.raises(PolicySnapshotUnavailable):
        execute(h)
    h.owner.rebase_draft.assert_not_called()
    h.owner.record_rebase.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
def test_no_conflict_and_equal_content_still_advance_base_without_using_stale_flag(
    namespace: str,
) -> None:
    h = rebase_state(namespace)
    h.draft = h.draft.model_copy(
        update={
            "content": deepcopy(h.current.values),
            "content_hash": h.current.snapshot_hash,
            "stale": False,
        }
    )
    h.owner.draft.return_value = h.draft
    h.request = request_for(h)
    assert h.request["resolutions"] == {}
    updated = execute(h)
    assert updated.content == h.draft.content and updated.content_hash == h.draft.content_hash
    assert updated.base_version == 2 and updated.revision == h.draft.revision + 1
    h.owner.record_rebase.assert_called_once()


@pytest.mark.parametrize("fault", ["hash", "schema", "cas"])
def test_rebase_bad_stored_fact_or_failed_cas_cannot_emit_history_or_success_audit(
    fault: str,
) -> None:
    h = rebase_state()
    if fault == "hash":
        h.owner.draft.return_value = h.draft.model_copy(update={"content_hash": "a" * 64})
    if fault == "schema":
        h.owner.draft.return_value = h.draft.model_copy(update={"schema_revision": 2})
    if fault == "cas":
        h.owner.rebase_draft.side_effect = None
        h.owner.rebase_draft.return_value = None
    with pytest.raises(StaleDraftRevision if fault == "cas" else PolicySnapshotUnavailable):
        execute(h)
    h.owner.record_rebase.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()
