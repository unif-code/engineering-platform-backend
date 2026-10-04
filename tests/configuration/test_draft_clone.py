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
    DraftAuthorizationDenied,
    DraftNotFound,
    InvalidPolicyValue,
    PolicyLifecycle,
    PolicySnapshotUnavailable,
    StaleDraftRevision,
)
from control_plane.app.modules.configuration.application.drafts import _content_hash
from control_plane.app.modules.requirement.domain.gate_policy import FORMAL_KEY
from tests.configuration.test_draft_base_comparison import NOW, comparison_state


def clone_state(namespace: str = "identity") -> Any:
    h = comparison_state(namespace)
    h.events = []
    h.authorization = Mock()
    h.authorization.check.side_effect = lambda **_: h.events.append("authorization")
    h.audit = Mock()
    h.audit.append_in_transaction.side_effect = lambda *_: h.events.append("audit")
    h.dependencies = ConfigurationDependencies(
        clock=SimpleNamespace(now=lambda: NOW + timedelta(seconds=1)),
        random=SimpleNamespace(uuid4=uuid4),
        audit=h.audit,
    )
    h.owner.locked_active_snapshot.side_effect = lambda *_: (h.events.append("active"), h.current)[
        1
    ]
    h.owner.draft.side_effect = lambda *_, **__: (
        h.events.append("draft"),
        h.owner.draft.return_value,
    )[1]

    def create(**values: Any) -> Any:
        h.events.append("create")
        return h.draft.model_copy(
            update={
                **{key: deepcopy(value) for key, value in values.items() if key != "now"},
                "revision": 1,
                "status": "DRAFT",
                "last_meaningful_activity_at": values["now"],
                "archived_at": None,
                "validation_evidence": None,
                "preview_evidence": None,
            }
        )

    h.owner.create_draft.side_effect = create
    h.owner.record_clone.side_effect = lambda **_: h.events.append("source")
    h.lifecycle = PolicyLifecycle(h.db, h.owner, h.dependencies)
    return h


def execute(h: Any, **overrides: Any) -> Any:
    assert hasattr(h.lifecycle, "clone_draft"), "explicit clone is missing"
    return h.lifecycle.clone_draft(
        **{
            "namespace": h.namespace,
            "draft_id": h.draft.id,
            "actor_id": "current-admin",
            "expected_revision": 7,
            "raw_session": "never-record-this-session",
            "authorization": h.authorization,
            **overrides,
        }
    )


@pytest.mark.parametrize("namespace", ["identity", "requirement.gate"])
@pytest.mark.parametrize("source_kind", ["self", "other", "archived", "rollback"])
@pytest.mark.parametrize("base_current", [False, True])
def test_clone_retains_complete_source_base_and_provenance_without_mutating_source(
    namespace: str, source_kind: str, base_current: bool
) -> None:
    h = clone_state(namespace)
    h.draft = h.draft.model_copy(
        update={
            "stale": False,
            "owner_id": "current-admin" if source_kind == "self" else "other-admin",
            "status": "ARCHIVED" if source_kind == "archived" else "DRAFT",
            "archived_at": NOW if source_kind == "archived" else None,
            "rollback_from_version": 1 if source_kind == "rollback" else None,
        }
    )
    if base_current:
        h.current = h.base
    h.owner.draft.return_value = h.draft
    before = deepcopy((h.draft, h.base, h.current))
    result = execute(h)
    copied = result.draft
    assert h.events == ["active", "draft", "authorization", "create", "source", "audit"]
    h.owner.draft.assert_called_once_with(h.draft.id, for_update=True)
    h.owner.locked_active_snapshot.assert_called_once_with(namespace)
    h.owner.active_snapshot.assert_not_called()
    h.authorization.check.assert_called_once_with(
        raw_session="never-record-this-session", actor_id="current-admin"
    )
    assert copied.id != h.draft.id and copied.owner_id == "current-admin"
    assert copied.revision == 1 and copied.status == "DRAFT"
    assert copied.namespace == namespace and copied.scope == "PLATFORM"
    assert copied.base_version == 1 and copied.schema_revision == 1
    assert copied.content == h.draft.content and copied.content is not h.draft.content
    assert copied.content_hash == _content_hash(copied.content)
    assert copied.stale is (not base_current)
    assert copied.rollback_from_version == h.draft.rollback_from_version
    assert copied.last_meaningful_activity_at == NOW + timedelta(seconds=1)
    assert copied.archived_at is None
    assert copied.validation_evidence is None and copied.preview_evidence is None
    archived_source = h.draft.id if source_kind == "archived" else None
    assert result.source.model_dump() == {
        "draft_id": h.draft.id,
        "revision": 7,
        "owner_id": h.draft.owner_id,
        "status": h.draft.status,
        "base_version": 1,
        "schema_revision": 1,
        "content_hash": h.draft.content_hash,
        "rollback_from_version": h.draft.rollback_from_version,
        "cloned_from_archived_draft_id": archived_source,
    }
    assert result.current_version_at_clone == h.current.version
    record = h.owner.record_clone.call_args.kwargs
    assert record["draft_id"] == copied.id and record["source_draft_id"] == h.draft.id
    assert record["source_revision"] == 7 and record["source_owner_id"] == h.draft.owner_id
    assert record["source_status"] == h.draft.status
    assert record["source_content"] == h.draft.content
    assert record["source_content"] is not h.draft.content
    assert record["source_content_hash"] == h.draft.content_hash
    assert record["content_hash"] == copied.content_hash
    assert record["base_snapshot_hash"] == h.base.snapshot_hash
    assert record["current_version"] == h.current.version
    assert record["current_snapshot_hash"] == h.current.snapshot_hash
    assert record["rollback_from_version"] == h.draft.rollback_from_version
    assert record["cloned_from_archived_draft_id"] == archived_source
    envelope = h.audit.append_in_transaction.call_args.args[1]
    assert envelope.action == "configuration.draft.cloned" and envelope.target_id == copied.id
    summary = json.loads(envelope.reason)
    assert summary["source_draft_id"] == h.draft.id and summary["draft_id"] == copied.id
    assert "source_content" not in summary
    assert "never-record-this-session" not in str(record) + envelope.reason
    assert (h.draft, h.base, h.current) == before
    if namespace == "identity":
        copied.content["identity.login_backoff"]["failureThreshold"] = 99
    else:
        copied.content[FORMAL_KEY].clear()
    assert (h.draft, h.base, h.current) == before
    assert record["source_content"] == h.draft.content
    h.owner.update_draft.assert_not_called()
    h.owner.rebase_draft.assert_not_called()
    h.owner.takeover_draft.assert_not_called()


@pytest.mark.parametrize(
    "case,error",
    [
        ("missing", DraftNotFound),
        ("namespace", DraftNotFound),
        ("id", DraftNotFound),
        ("revision", StaleDraftRevision),
        ("scope", PolicySnapshotUnavailable),
        ("status", PolicySnapshotUnavailable),
        ("schema", PolicySnapshotUnavailable),
        ("hash", PolicySnapshotUnavailable),
        ("base-missing", PolicySnapshotUnavailable),
        ("base-hash", PolicySnapshotUnavailable),
        ("current-hash", PolicySnapshotUnavailable),
        ("base-future", PolicySnapshotUnavailable),
        ("invalid-candidate", InvalidPolicyValue),
    ],
)
def test_clone_rejects_untrusted_source_before_authorization_or_write(
    case: str, error: type[Exception]
) -> None:
    h = clone_state()
    updates = {
        "namespace": {"namespace": "elsewhere"},
        "id": {"id": "elsewhere"},
        "revision": {"revision": 8, "status": "ARCHIVED"},
        "scope": {"scope": "WORKSPACE"},
        "status": {"status": "UNKNOWN"},
        "schema": {"schema_revision": 2},
        "hash": {"content_hash": "bad"},
        "base-future": {"base_version": 3},
    }
    if case in updates:
        h.owner.draft.return_value = h.draft.model_copy(update=updates[case])
    if case == "missing":
        h.owner.draft.return_value = None
    if case == "base-missing":
        h.owner.version_snapshot.return_value = None
    if case == "base-hash":
        h.owner.version_snapshot.return_value = h.base.model_copy(update={"snapshot_hash": "bad"})
    if case == "current-hash":
        h.current = h.current.model_copy(update={"snapshot_hash": "bad"})
    if case == "invalid-candidate":
        content = {**h.draft.content, "identity.session_cap": "5"}
        h.owner.draft.return_value = h.draft.model_copy(
            update={
                "content": content,
                "content_hash": _content_hash(content),
            }
        )
    with pytest.raises(error):
        execute(h)
    h.authorization.check.assert_not_called()
    h.owner.create_draft.assert_not_called()
    h.owner.record_clone.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        DraftAuthorizationDenied(401),
        DraftAuthorizationDenied(403),
        RuntimeError("unavailable"),
    ],
)
def test_clone_current_session_denial_or_missing_check_produces_no_copy(failure: Any) -> None:
    h = clone_state()
    h.authorization.check.side_effect = failure
    with pytest.raises(
        DraftAuthorizationDenied
        if isinstance(failure, DraftAuthorizationDenied)
        else PolicySnapshotUnavailable
    ):
        execute(h, authorization=None if failure is None else h.authorization)
    h.owner.create_draft.assert_not_called()
    h.owner.record_clone.assert_not_called()
    h.audit.append_in_transaction.assert_not_called()


@pytest.mark.parametrize("failure", ["create", "source", "audit"])
def test_clone_write_failures_propagate_to_the_owning_transaction(failure: str) -> None:
    h = clone_state()
    target = {
        "create": h.owner.create_draft,
        "source": h.owner.record_clone,
        "audit": h.audit.append_in_transaction,
    }[failure]
    target.side_effect = RuntimeError("controlled write failure")
    with pytest.raises(RuntimeError, match="controlled write failure"):
        execute(h)
    if failure == "create":
        h.owner.record_clone.assert_not_called()
    if failure != "audit":
        h.audit.append_in_transaction.assert_not_called()
