from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from control_plane.app.modules.configuration.api import routes
from control_plane.app.modules.configuration.application import drafts
from control_plane.app.modules.configuration.application.lifecycle import PolicyLifecycle
from control_plane.app.modules.configuration.application.preview import preview
from control_plane.app.modules.configuration.domain import (
    ConfigurationError,
    Draft,
    DraftArchived,
    DraftNotFound,
    DraftOwnerRequired,
    StaleDraftRevision,
)
from tests.configuration.test_api import _Secrets
from tests.shared.test_idempotency_contract import MemoryRepository

NOW = datetime(2026, 10, 4, 1, tzinfo=UTC)


@pytest.fixture
def takeover(monkeypatch: pytest.MonkeyPatch) -> Any:
    draft = Draft(
        id="draft-known",
        namespace="identity",
        scope="PLATFORM",
        content={"identity.session_cap": 4, "private": "candidate-must-not-enter-audit"},
        base_version=1,
        owner_id="original-admin",
        revision=3,
        status="DRAFT",
        stale=False,
        last_meaningful_activity_at=NOW - timedelta(days=1),
        archived_at=None,
        schema_revision=1,
        content_hash="a" * 64,
        rollback_from_version=2,
        validation_evidence={"valid": True, "content_hash": "a" * 64},
        preview_evidence={"revision": 3, "content_hash": "a" * 64},
    )
    owner = Mock()
    owner.draft.return_value = draft
    owner.active_snapshot.side_effect = AssertionError("takeover must not acquire Active or rebase")
    owner.takeover_draft.side_effect = lambda _id, expected_revision, owner_id, now: (
        owner.draft.return_value.model_copy(
            update={
                "owner_id": owner_id,
                "revision": expected_revision + 1,
                "last_meaningful_activity_at": now,
                "validation_evidence": None,
                "preview_evidence": None,
            }
        )
    )
    audit = Mock()
    monkeypatch.setattr(drafts, "_audit", audit)
    return SimpleNamespace(
        owner=owner,
        draft=draft,
        audit=audit,
        dependencies=SimpleNamespace(clock=SimpleNamespace(now=lambda: NOW)),
    )


def execute(h: Any, **overrides: Any) -> Draft:
    assert hasattr(drafts, "takeover_draft"), "shared lifecycle takeover is missing"
    values = dict(
        namespace="identity",
        draft_id=h.draft.id,
        actor_id="new-admin",
        expected_revision=3,
        reason="  接管原原因\n保留原文  ",
        dependencies=h.dependencies,
    )
    values.update(overrides)
    return drafts.takeover_draft(Mock(), h.owner, **values)


@pytest.mark.parametrize("stale", [False, True])
def test_takeover_preserves_candidate_and_base_clears_evidence_without_active_lookup(
    takeover: Any, stale: bool
) -> None:
    h = takeover
    h.owner.draft.return_value = h.draft.model_copy(update={"stale": stale})
    result = execute(h)
    assert result.owner_id == "new-admin" and result.revision == 4
    assert result.last_meaningful_activity_at == NOW
    assert result.validation_evidence is None and result.preview_evidence is None
    for name in (
        "id",
        "namespace",
        "scope",
        "content",
        "content_hash",
        "base_version",
        "schema_revision",
        "archived_at",
        "rollback_from_version",
    ):
        assert getattr(result, name) == getattr(h.draft, name)
    assert result.stale is stale
    h.owner.draft.assert_called_once_with(h.draft.id, for_update=True)
    h.owner.takeover_draft.assert_called_once_with(
        h.draft.id, expected_revision=3, owner_id="new-admin", now=NOW
    )
    h.owner.active_snapshot.assert_not_called()
    audit = h.audit.call_args.kwargs
    assert audit["actor_id"] == "new-admin" and audit["draft_id"] == h.draft.id
    for value in (
        "original-admin",
        "new-admin",
        "previousRevision=3",
        "revision=4",
        "namespace=identity",
        "  接管原原因\n保留原文  ",
    ):
        assert value in audit["reason"]
    assert "candidate-must-not-enter-audit" not in audit["reason"]


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing", DraftNotFound),
        ("namespace", DraftNotFound),
        ("archived", DraftArchived),
        ("same-owner", ConfigurationError),
        ("revision", StaleDraftRevision),
    ],
)
def test_takeover_refuses_without_touching_owner_activity_or_evidence(
    takeover: Any, case: str, expected: type[Exception]
) -> None:
    h = takeover
    if case == "missing":
        h.owner.draft.return_value = None
    if case == "namespace":
        h.owner.draft.return_value = h.draft.model_copy(update={"namespace": "requirement.gate"})
    if case == "archived":
        h.owner.draft.return_value = h.draft.model_copy(
            update={"status": "ARCHIVED", "archived_at": NOW}
        )
    if case == "same-owner":
        h.owner.draft.return_value = h.draft.model_copy(update={"owner_id": "new-admin"})
    if case == "revision":
        h.owner.draft.return_value = h.draft.model_copy(update={"revision": 4})
    with pytest.raises(expected):
        execute(h)
    h.owner.takeover_draft.assert_not_called()
    h.owner.active_snapshot.assert_not_called()


def test_takeover_cas_loss_never_records_success(takeover: Any) -> None:
    h = takeover
    h.owner.takeover_draft.side_effect = None
    h.owner.takeover_draft.return_value = None
    with pytest.raises(StaleDraftRevision):
        execute(h)
    assert not any(call.kwargs.get("result") == "SUCCESS" for call in h.audit.call_args_list)


@pytest.mark.parametrize("operation", ["update", "validate", "preview"])
@pytest.mark.parametrize("revision,expected", [(3, StaleDraftRevision), (4, DraftOwnerRequired)])
def test_old_owner_writes_check_revision_before_owner(
    takeover: Any, operation: str, revision: int, expected: type[Exception]
) -> None:
    h = takeover
    h.owner.draft.return_value = h.draft.model_copy(update={"owner_id": "new-admin", "revision": 4})
    command: Any = {
        "update": drafts.update_draft,
        "validate": drafts.validate_draft,
        "preview": preview,
    }[operation]
    values = dict(
        namespace="identity",
        draft_id=h.draft.id,
        actor_id="original-admin",
        expected_revision=revision,
        dependencies=h.dependencies,
    )
    if operation == "update":
        values["values"] = {}
    with pytest.raises(expected):
        command(Mock(), h.owner, **values)
    h.owner.active_snapshot.assert_not_called()


@pytest.fixture
def takeover_api(takeover: Any) -> Any:
    h = takeover
    h.dependencies.random = SimpleNamespace(uuid4=uuid4)
    memory = MemoryRepository()
    for name in ("claim_idempotency", "idempotency_by_scope", "complete_idempotency"):
        setattr(h.owner, name, getattr(memory, name))
    persist = h.owner.takeover_draft.side_effect

    def apply(*args: Any, **kwargs: Any) -> Draft:
        updated: Draft = persist(*args, **kwargs)
        h.owner.draft.return_value = updated
        return updated

    h.owner.takeover_draft.side_effect = apply
    h.owner.active_snapshot.side_effect = None

    @contextmanager
    def transaction() -> Any:
        yield PolicyLifecycle(Mock(), h.owner, h.dependencies)

    owner_runtime = SimpleNamespace(transaction=transaction)
    owners = Mock()
    owners.resolve.return_value = owner_runtime
    runtime = routes.ConfigurationHttpRuntime(
        owners=owners, dependencies=h.dependencies, secret_manager=_Secrets()
    )
    guard = Mock()
    app = FastAPI()
    app.include_router(
        routes.create_configuration_router(
            lambda: runtime, lambda: SimpleNamespace(account_id="new-admin"), guard
        )
    )
    with TestClient(app, base_url="https://testserver") as client:
        yield SimpleNamespace(**vars(h), client=client, guard=guard, owners=owners)


def post_takeover(
    h: Any,
    *,
    body: Any = None,
    key: str = "takeover-command",
    etag: str = '"v3"',
    origin: str = "https://testserver",
) -> Any:
    return h.client.post(
        "/api/v1/admin/policies/identity/drafts/draft-known/takeover",
        json={"reason": "  原始接管理由\n  "} if body is None else body,
        headers={"Origin": origin, "Idempotency-Key": key, "If-Match": etag},
    )


def test_other_owner_read_is_authorized_read_only_and_takeover_replay_stays_historical(
    takeover_api: Any,
) -> None:
    h = takeover_api
    read = h.client.get("/api/v1/admin/policies/identity/drafts/draft-known")
    assert read.status_code == 200 and read.json()["ownerId"] == "original-admin"
    assert read.headers["etag"] == '"v3"' and read.headers["cache-control"] == "no-store"
    h.owner.takeover_draft.assert_not_called()
    h.audit.assert_not_called()
    accepted = post_takeover(h)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["ownerId"] == "new-admin" and accepted.headers["etag"] == '"v4"'
    h.owner.draft.return_value = h.owner.draft.return_value.model_copy(
        update={
            "owner_id": "third-admin",
            "revision": 9,
            "status": "ARCHIVED",
            "stale": True,
            "base_version": 8,
        }
    )
    replay = post_takeover(h)
    assert replay.content == accepted.content and replay.headers["etag"] == accepted.headers["etag"]
    h.owner.takeover_draft.assert_called_once()
    h.audit.assert_called_once()
    assert post_takeover(h, body={"reason": "different"}).status_code == 409
    assert post_takeover(h, body={"reason": "原始接管理由"}).status_code == 409
    assert post_takeover(h, etag='"v4"').status_code == 409


@pytest.mark.parametrize("status", [401, 403])
def test_takeover_current_authorization_precedes_owner_and_historical_replay(
    takeover_api: Any, status: int
) -> None:
    h = takeover_api
    assert post_takeover(h).status_code == 200
    h.owners.reset_mock()
    h.guard.side_effect = HTTPException(status)
    assert post_takeover(h).status_code == status
    assert h.client.get("/api/v1/admin/policies/identity/drafts/draft-known").status_code == status
    h.owners.resolve.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"reason": ""},
        {"reason": "x" * 1001},
        {"reason": "ok", "ownerId": "attacker"},
        {"reason": "ok", "totpCode": "123456"},
    ],
)
def test_takeover_body_is_reason_only_with_existing_limits(takeover_api: Any, body: Any) -> None:
    assert post_takeover(takeover_api, body=body).status_code == 422
    takeover_api.owners.resolve.assert_not_called()


@pytest.mark.parametrize("case,status", [("archived", 200), ("missing", 404), ("namespace", 404)])
def test_draft_read_preserves_namespace_and_archived_boundaries(
    takeover_api: Any, case: str, status: int
) -> None:
    h = takeover_api
    if case == "archived":
        h.owner.draft.return_value = h.draft.model_copy(
            update={"status": "ARCHIVED", "archived_at": NOW}
        )
    if case == "missing":
        h.owner.draft.return_value = None
    if case == "namespace":
        h.owner.draft.return_value = h.draft.model_copy(update={"namespace": "requirement.gate"})
    response = h.client.get("/api/v1/admin/policies/identity/drafts/draft-known")
    assert response.status_code == status
    h.owner.takeover_draft.assert_not_called()
    if status == 200:
        assert response.headers["cache-control"] == "no-store"
