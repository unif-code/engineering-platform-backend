import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from control_plane.app.modules.audit.adapters.transactional import (
    SqlAlchemyTransactionalAuditAppender,
)
from control_plane.app.modules.configuration import ConfigurationDependencies
from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy
from control_plane.app.modules.identity import IdentityPolicyCommandRuntime
from control_plane.app.shared.api.problem import register_problem_handlers
from control_plane.app.shared.api.request_id import request_id_middleware
from control_plane.app.shared.security import seal
from tests.identity.task5_helpers import dependencies as identity_dependencies


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


class _Random:
    def uuid4(self) -> UUID:
        return uuid4()


def _dependencies() -> ConfigurationDependencies:
    return ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )


def test_configuration_dependencies_do_not_expose_identity_runtime() -> None:
    assert "identity" not in {field.name for field in fields(ConfigurationDependencies)}


def _insert_policy_admin(
    owner_engine: Engine,
    *,
    dependencies: object,
) -> tuple[str, str]:
    actor_id = str(uuid4())
    employee_no = f"{uuid4().int % 100_000_000:08d}"
    secret = pyotp.random_base32()
    now = dependencies.clock.now()  # type: ignore[attr-defined]
    with owner_engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO identity.account ("
                "id, employee_no, display_name, status, password_hash, password_set_at, "
                "totp_sealed, totp_confirmed_at, is_super_admin, created_at, updated_at"
                ") VALUES ("
                ":id, :employee_no, 'Policy Admin', 'ENABLED', 'initialized', :now, "
                ":totp_sealed, :now, true, :now, :now)"
            ),
            {
                "id": actor_id,
                "employee_no": employee_no,
                "now": now,
                "totp_sealed": seal(secret.encode("ascii"), b"t" * 32),
            },
        )
    return actor_id, secret


def _delete_policy_admin(owner_engine: Engine, actor_id: str) -> None:
    with owner_engine.begin() as db:
        db.execute(
            text("DELETE FROM audit.audit_event WHERE actor=:actor_id"),
            {"actor_id": actor_id},
        )
        db.execute(
            text("DELETE FROM identity.auth_challenge WHERE account_id=:actor_id"),
            {"actor_id": actor_id},
        )
        db.execute(
            text("DELETE FROM identity.account WHERE id=:actor_id"),
            {"actor_id": actor_id},
        )


@pytest.mark.integration
def test_configuration_runtime_can_insert_but_cannot_mutate_published_versions(
    configuration_rw_engine: Engine,
    configuration_seed: None,
) -> None:
    del configuration_seed
    snapshot = {"identity.session_idle_timeout": 60}
    values = {
        "snapshot": json.dumps(snapshot, separators=(",", ":")),
        "changeset": json.dumps({"items": []}, separators=(",", ":")),
        "validation": json.dumps({"valid": True}, separators=(",", ":")),
        "dependencies": json.dumps({}, separators=(",", ":")),
        "preview": json.dumps({"items": []}, separators=(",", ":")),
    }

    with configuration_rw_engine.connect() as db:
        transaction = db.begin()
        try:
            db.execute(
                text(
                    "INSERT INTO identity.version ("
                    "namespace, scope, version, snapshot, changeset, published_by, reason, "
                    "published_at, schema_revision, snapshot_hash, validation_evidence, "
                    "dependency_versions, preview_evidence"
                    ") VALUES ("
                    "'identity', 'PLATFORM', 999999, CAST(:snapshot AS JSONB), "
                    "CAST(:changeset AS JSONB), 'permission-test', 'permission test', now(), "
                    "1, repeat('a', 64), CAST(:validation AS JSONB), "
                    "CAST(:dependencies AS JSONB), CAST(:preview AS JSONB))"
                ),
                values,
            )
            nested = db.begin_nested()
            with pytest.raises(ProgrammingError):
                db.execute(
                    text(
                        "UPDATE identity.version SET reason='mutated' "
                        "WHERE namespace='identity' AND scope='PLATFORM' AND version=999999"
                    )
                )
            nested.rollback()
            nested = db.begin_nested()
            with pytest.raises(ProgrammingError):
                db.execute(
                    text(
                        "DELETE FROM identity.version WHERE namespace='identity' "
                        "AND scope='PLATFORM' AND version=999999"
                    )
                )
            nested.rollback()
        finally:
            transaction.rollback()


@pytest.mark.integration
def test_preview_reports_only_changed_keys_in_stable_order_without_refreshing_activity(
    configuration_rw_engine: Engine,
    configuration_seed: None,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft, preview

    with configuration_rw_engine.connect() as db:
        transaction = db.begin()
        try:
            draft = create_draft(
                db,
                namespace="identity",
                values={
                    "identity.session_idle_timeout": 30,
                    "identity.draft_archive_after": 45,
                },
                actor_id="preview-admin",
                dependencies=_dependencies(),
            )
            before_activity = draft.last_meaningful_activity_at
            result = preview(
                db,
                namespace="identity",
                draft_id=draft.id,
                actor_id="preview-admin",
                expected_revision=draft.revision,
                dependencies=_dependencies(),
            )
            persisted = db.execute(
                text(
                    "SELECT last_meaningful_activity_at, preview_evidence "
                    "FROM identity.draft WHERE id=:draft_id"
                ),
                {"draft_id": draft.id},
            ).one()
        finally:
            transaction.rollback()

    assert [item.key for item in result.items] == [
        "identity.draft_archive_after",
        "identity.session_idle_timeout",
    ]
    assert result.items[0].model_dump() == {
        "key": "identity.draft_archive_after",
        "before": 30,
        "after": 45,
        "effect_semantics": "NEXT_SCHEDULE",
        "impact": (
            "The next archive task run uses the new inactivity window; "
            "drafts are archived without being deleted."
        ),
    }
    assert result.items[1].model_dump() == {
        "key": "identity.session_idle_timeout",
        "before": 60,
        "after": 30,
        "effect_semantics": "IMMEDIATE",
        "impact": (
            "Authenticated API activity uses the new idle limit immediately; "
            "expired sessions are rejected on their next request."
        ),
    }
    assert persisted.last_meaningful_activity_at == before_activity
    assert persisted.preview_evidence == result.model_dump(mode="json")
    serialized = json.dumps(persisted.preview_evidence, sort_keys=True)
    assert "totp" not in serialized.lower()
    assert "password" not in serialized.lower()


@pytest.mark.integration
def test_edit_after_preview_invalidates_preview_binding(
    configuration_rw_engine: Engine,
    configuration_seed: None,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft, preview, update_draft

    with configuration_rw_engine.connect() as db:
        transaction = db.begin()
        try:
            draft = create_draft(
                db,
                namespace="identity",
                values={"identity.session_idle_timeout": 31},
                actor_id="preview-edit-admin",
                dependencies=_dependencies(),
            )
            preview(
                db,
                namespace="identity",
                draft_id=draft.id,
                actor_id="preview-edit-admin",
                expected_revision=draft.revision,
                dependencies=_dependencies(),
            )
            updated = update_draft(
                db,
                namespace="identity",
                draft_id=draft.id,
                values={"identity.session_idle_timeout": 32},
                actor_id="preview-edit-admin",
                expected_revision=draft.revision,
                dependencies=_dependencies(),
            )
        finally:
            transaction.rollback()

    assert updated.revision == 2
    assert updated.preview_evidence is None


@pytest.mark.integration
def test_identity_owner_runtime_publishes_with_reauthentication_in_one_transaction(
    configuration_rw_engine: Engine,
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft
    from control_plane.app.modules.identity import IdentityPolicyCommandRuntime

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    configuration_deps = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    with configuration_rw_engine.begin() as db:
        draft = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 31},
            actor_id=actor_id,
            dependencies=configuration_deps,
        )
    code = pyotp.TOTP(secret).at(identity_deps.clock.now())
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: identity_deps.clock.now().timestamp(),
    )
    key = f"identity-owner-publish-{uuid4()}"
    runtime = IdentityPolicyCommandRuntime(identity_rw_engine, identity_deps)
    try:
        response = runtime.publish(
            actor_id=actor_id,
            namespace="identity",
            draft_id=draft.id,
            expected_revision=1,
            reason="publish through the Identity owner transaction",
            totp_code=code,
            idempotency_key=key,
        )
        with configuration_owner_engine.connect() as db:
            challenge = db.execute(
                text(
                    "SELECT purpose, consumed_at, attempt_count FROM identity.auth_challenge "
                    "WHERE account_id=:actor_id"
                ),
                {"actor_id": actor_id},
            ).one()
            pointer = db.execute(
                text(
                    "SELECT version FROM identity.active_pointer "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            ).scalar_one()
        assert response.status_code == 201
        assert response.body["version"] == pointer == 2
        assert challenge.purpose == "POLICY_PUBLISH"
        assert challenge.consumed_at == identity_deps.clock.now()
        assert challenge.attempt_count == 0
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor"),
                {"actor": actor_id},
            )
            db.execute(
                text(
                    "UPDATE identity.active_pointer SET version=1 "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.configuration_outbox "
                    "WHERE aggregate_id='identity:PLATFORM:2'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.version WHERE namespace='identity' "
                    "AND scope='PLATFORM' AND version=2"
                )
            )
            db.execute(text("DELETE FROM identity.draft WHERE id=:id"), {"id": draft.id})
        _delete_policy_admin(configuration_owner_engine, actor_id)


@pytest.mark.integration
def test_identity_owner_runtime_creates_rollback_draft_in_one_transaction(
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configuration_seed
    from control_plane.app.modules.identity import IdentityPolicyCommandRuntime

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: identity_deps.clock.now().timestamp(),
    )
    runtime = IdentityPolicyCommandRuntime(identity_rw_engine, identity_deps)
    key = f"identity-owner-rollback-{uuid4()}"
    draft_id: str | None = None
    try:
        response = runtime.rollback(
            actor_id=actor_id,
            namespace="identity",
            scope="PLATFORM",
            to_version=1,
            expected_version=1,
            reason="prepare rollback through the Identity owner transaction",
            totp_code=pyotp.TOTP(secret).at(identity_deps.clock.now()),
            idempotency_key=key,
        )
        draft_id = str(response.body["id"])
        with configuration_owner_engine.connect() as db:
            pointer = db.execute(
                text(
                    "SELECT version FROM identity.active_pointer "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            ).scalar_one()
            stored = db.execute(
                text(
                    "SELECT base_version, rollback_from_version FROM identity.draft "
                    "WHERE id=:draft_id"
                ),
                {"draft_id": draft_id},
            ).one()
            challenge = db.execute(
                text(
                    "SELECT purpose, consumed_at FROM identity.auth_challenge "
                    "WHERE account_id=:actor_id"
                ),
                {"actor_id": actor_id},
            ).one()
        assert response.status_code == 201
        assert pointer == 1
        assert tuple(stored) == (1, 1)
        assert tuple(challenge) == ("POLICY_ROLLBACK", identity_deps.clock.now())
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor"),
                {"actor": actor_id},
            )
            if draft_id is not None:
                db.execute(
                    text("DELETE FROM identity.draft WHERE id=:draft_id"),
                    {"draft_id": draft_id},
                )
        _delete_policy_admin(configuration_owner_engine, actor_id)


@pytest.mark.integration
def test_concurrent_publish_has_one_winner_without_duplicate_totp_side_effect(
    configuration_rw_engine: Engine,
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    dependencies = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: identity_deps.clock.now().timestamp(),
    )
    with configuration_rw_engine.begin() as db:
        first = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 31},
            actor_id=actor_id,
            dependencies=dependencies,
        )
        second = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 32},
            actor_id=actor_id,
            dependencies=dependencies,
        )
    ready = Barrier(2)
    owner_runtime = IdentityPolicyCommandRuntime(identity_rw_engine, identity_deps)

    def attempt(draft_id: str, revision: int) -> tuple[str, int | None]:
        ready.wait()
        response = owner_runtime.publish(
            namespace="identity",
            draft_id=draft_id,
            actor_id=actor_id,
            expected_revision=revision,
            reason=f"concurrent publish {draft_id}",
            totp_code=pyotp.TOTP(secret).at(identity_deps.clock.now()),
            idempotency_key=f"concurrent-{draft_id}",
        )
        if response.status_code == 409:
            assert response.body["code"] == "SOURCE_STALE"
            return "SOURCE_STALE", None
        assert response.status_code == 201
        return "PUBLISHED", int(response.body["version"])

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda draft: attempt(draft.id, draft.revision),
                    (first, second),
                )
            )

        with configuration_owner_engine.connect() as db:
            pointer = db.execute(
                text(
                    "SELECT version FROM identity.active_pointer "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            ).scalar_one()
            versions = (
                db.execute(
                    text(
                        "SELECT version FROM identity.version WHERE namespace='identity' "
                        "AND scope='PLATFORM' AND version > 1 ORDER BY version"
                    )
                )
                .scalars()
                .all()
            )
            outbox_count = db.execute(
                text(
                    "SELECT count(*) FROM identity.configuration_outbox "
                    "WHERE event_type='POLICY_PUBLISHED' "
                    "AND aggregate_id='identity:PLATFORM:2'"
                )
            ).scalar_one()
            totp_challenges = db.execute(
                text(
                    "SELECT count(*) FROM identity.auth_challenge "
                    "WHERE account_id=:actor_id AND purpose='POLICY_PUBLISH'"
                ),
                {"actor_id": actor_id},
            ).scalar_one()
            audits = (
                db.execute(
                    text(
                        "SELECT action, result, reason FROM audit.audit_event "
                        "WHERE actor=:actor_id AND action IN "
                        "('configuration.policy.published', "
                        "'configuration.policy.publish_denied') ORDER BY action"
                    ),
                    {"actor_id": actor_id},
                )
                .mappings()
                .all()
            )

        assert sorted(results) == [("PUBLISHED", 2), ("SOURCE_STALE", None)]
        assert pointer == 2
        assert versions == [2]
        assert outbox_count == 1
        assert totp_challenges == 1
        assert [(row["action"], row["result"]) for row in audits] == [
            ("configuration.policy.publish_denied", "DENIED"),
            ("configuration.policy.published", "SUCCESS"),
        ]
        denial = next(
            row for row in audits if row["action"] == "configuration.policy.publish_denied"
        )
        assert "reasonCode=SOURCE_STALE" in denial["reason"]
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor"),
                {"actor": actor_id},
            )
            db.execute(
                text(
                    "UPDATE identity.active_pointer SET version=1 "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.configuration_outbox "
                    "WHERE aggregate_id='identity:PLATFORM:2'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.version WHERE namespace='identity' "
                    "AND scope='PLATFORM' AND version=2"
                )
            )
            db.execute(
                text("DELETE FROM identity.draft WHERE id IN (:first, :second)"),
                {"first": first.id, "second": second.id},
            )
        _delete_policy_admin(configuration_owner_engine, actor_id)


@pytest.mark.integration
def test_policy_http_lifecycle_replays_source_stale_and_uses_fresh_totp_once(
    configuration_rw_engine: Engine,
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft
    from control_plane.app.modules.configuration.api import (
        ConfigurationHttpRuntime,
        create_configuration_router,
    )

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    dependencies = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: identity_deps.clock.now().timestamp(),
    )
    with configuration_rw_engine.begin() as db:
        winner = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 31},
            actor_id=actor_id,
            dependencies=dependencies,
        )
        loser = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 32},
            actor_id=actor_id,
            dependencies=dependencies,
        )

    runtime = ConfigurationHttpRuntime(
        engine=configuration_rw_engine,
        dependencies=dependencies,
        secret_manager=identity_deps.secret_manager,
        policy_commands=IdentityPolicyCommandRuntime(identity_rw_engine, identity_deps),
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_configuration_router(
            lambda: runtime,
            lambda: SimpleNamespace(account_id=actor_id),
            lambda _principal, _capability, _scope: None,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    same_origin = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}
    publish_key = f"publish-{uuid4()}"
    stale_key = f"stale-{uuid4()}"
    rollback_key = f"rollback-{uuid4()}"
    rollback_draft_id: str | None = None
    try:
        preview_response = client.get(
            f"/api/v1/admin/policies/identity/drafts/{winner.id}/preview",
            headers={"If-Match": '"v1"'},
        )
        assert preview_response.status_code == 200
        assert preview_response.headers["etag"] == '"v1"'
        assert preview_response.json()["items"][0]["effectSemantics"] == "IMMEDIATE"

        publish_body = {
            "reason": "publish reviewed idle policy",
            "totpCode": pyotp.TOTP(secret).at(identity_deps.clock.now()),
        }
        published = client.post(
            f"/api/v1/admin/policies/identity/drafts/{winner.id}/publish",
            json=publish_body,
            headers={
                **same_origin,
                "Idempotency-Key": publish_key,
                "If-Match": '"v1"',
                "X-Request-ID": "req-publishfirst",
            },
        )
        published_replay = client.post(
            f"/api/v1/admin/policies/identity/drafts/{winner.id}/publish",
            json=publish_body,
            headers={
                **same_origin,
                "Idempotency-Key": publish_key,
                "If-Match": '"v1"',
                "X-Request-ID": "req-publishreplay",
            },
        )
        assert published.status_code == published_replay.status_code == 201
        assert published.headers["etag"] == published_replay.headers["etag"] == '"v2"'
        assert published.json() == published_replay.json()

        stale_body = {
            "reason": "publish stale competing draft",
            "totpCode": publish_body["totpCode"],
        }
        stale = client.post(
            f"/api/v1/admin/policies/identity/drafts/{loser.id}/publish",
            json=stale_body,
            headers={
                **same_origin,
                "Idempotency-Key": stale_key,
                "If-Match": '"v1"',
                "X-Request-ID": "req-sourcefirst",
            },
        )
        stale_replay = client.post(
            f"/api/v1/admin/policies/identity/drafts/{loser.id}/publish",
            json=stale_body,
            headers={
                **same_origin,
                "Idempotency-Key": stale_key,
                "If-Match": '"v1"',
                "X-Request-ID": "req-sourcereplay",
            },
        )
        assert stale.status_code == stale_replay.status_code == 409
        assert stale.json()["code"] == stale_replay.json()["code"] == "SOURCE_STALE"
        assert stale.json()["requestId"] == "req-sourcefirst"
        assert stale_replay.json()["requestId"] == "req-sourcereplay"

        listed = client.get("/api/v1/admin/policies/identity/versions")
        assert listed.status_code == 200
        assert [item["version"] for item in listed.json()["items"]] == [2, 1]
        assert listed.json()["nextCursor"] is None

        identity_deps.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
        rollback_body = {
            "scope": "PLATFORM",
            "toVersion": 1,
            "reason": "prepare a reviewed rollback",
            "totpCode": pyotp.TOTP(secret).at(identity_deps.clock.now()),
        }
        rolled_back = client.post(
            "/api/v1/admin/policies/identity/rollback",
            json=rollback_body,
            headers={
                **same_origin,
                "Idempotency-Key": rollback_key,
                "If-Match": '"v2"',
            },
        )
        rollback_replay = client.post(
            "/api/v1/admin/policies/identity/rollback",
            json=rollback_body,
            headers={
                **same_origin,
                "Idempotency-Key": rollback_key,
                "If-Match": '"v2"',
            },
        )
        assert rolled_back.status_code == rollback_replay.status_code == 201
        assert rolled_back.headers["etag"] == rollback_replay.headers["etag"] == '"v1"'
        assert rolled_back.json() == rollback_replay.json()
        assert rolled_back.json()["baseVersion"] == 2
        assert rolled_back.json()["rollbackFromVersion"] == 1
        rollback_draft_id = rolled_back.json()["id"]

        with configuration_owner_engine.connect() as db:
            pointer = db.execute(
                text(
                    "SELECT version FROM identity.active_pointer "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            ).scalar_one()
            challenge_count = db.execute(
                text("SELECT count(*) FROM identity.auth_challenge WHERE account_id=:actor_id"),
                {"actor_id": actor_id},
            ).scalar_one()
            denial_audits = db.execute(
                text(
                    "SELECT count(*), min(correlation_id) FROM audit.audit_event "
                    "WHERE actor=:actor_id "
                    "AND action='configuration.policy.publish_denied'"
                ),
                {"actor_id": actor_id},
            ).one()
            idempotency_count = db.execute(
                text(
                    "SELECT count(*) FROM identity.configuration_idempotency_record "
                    "WHERE actor=:actor_id"
                ),
                {"actor_id": actor_id},
            ).scalar_one()
        assert pointer == 2
        assert challenge_count == 2
        assert denial_audits == (1, "req-sourcefirst")
        assert idempotency_count == 3
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor_id"),
                {"actor_id": actor_id},
            )
            db.execute(
                text(
                    "UPDATE identity.active_pointer SET version=1 "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.configuration_outbox "
                    "WHERE aggregate_id='identity:PLATFORM:2'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.version WHERE namespace='identity' "
                    "AND scope='PLATFORM' AND version=2"
                )
            )
            draft_ids = [winner.id, loser.id]
            if rollback_draft_id is not None:
                draft_ids.append(rollback_draft_id)
            db.execute(
                text("DELETE FROM identity.draft WHERE id = ANY(:draft_ids)"),
                {"draft_ids": draft_ids},
            )
        _delete_policy_admin(configuration_owner_engine, actor_id)


@pytest.mark.integration
def test_wrong_publish_totp_replays_one_safe_denial_without_domain_facts(
    configuration_rw_engine: Engine,
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft
    from control_plane.app.modules.configuration.api import (
        ConfigurationHttpRuntime,
        create_configuration_router,
    )

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    dependencies = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: identity_deps.clock.now().timestamp(),
    )
    with configuration_rw_engine.begin() as db:
        draft = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 31},
            actor_id=actor_id,
            dependencies=dependencies,
        )
    valid_code = pyotp.TOTP(secret).at(identity_deps.clock.now())
    invalid_code = valid_code[:-1] + str((int(valid_code[-1]) + 1) % 10)
    runtime = ConfigurationHttpRuntime(
        engine=configuration_rw_engine,
        dependencies=dependencies,
        secret_manager=identity_deps.secret_manager,
        policy_commands=IdentityPolicyCommandRuntime(identity_rw_engine, identity_deps),
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_configuration_router(
            lambda: runtime,
            lambda: SimpleNamespace(account_id=actor_id),
            lambda _principal, _capability, _scope: None,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    key = f"wrong-totp-{uuid4()}"
    body = {"reason": "publish after reauthentication", "totpCode": invalid_code}
    headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "Idempotency-Key": key,
        "If-Match": '"v1"',
    }
    try:
        first = client.post(
            f"/api/v1/admin/policies/identity/drafts/{draft.id}/publish",
            json=body,
            headers={**headers, "X-Request-ID": "req-wrongtotpfirst"},
        )
        replay = client.post(
            f"/api/v1/admin/policies/identity/drafts/{draft.id}/publish",
            json=body,
            headers={**headers, "X-Request-ID": "req-wrongtotpreplay"},
        )
        with configuration_owner_engine.connect() as db:
            pointer = db.execute(
                text(
                    "SELECT version FROM identity.active_pointer "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            ).scalar_one()
            versions = db.execute(
                text(
                    "SELECT count(*) FROM identity.version "
                    "WHERE namespace='identity' AND scope='PLATFORM' AND version>1"
                )
            ).scalar_one()
            challenges = db.execute(
                text(
                    "SELECT attempt_count, consumed_at FROM identity.auth_challenge "
                    "WHERE account_id=:actor_id AND purpose='POLICY_PUBLISH'"
                ),
                {"actor_id": actor_id},
            ).all()
            denials = db.execute(
                text(
                    "SELECT reason, correlation_id FROM audit.audit_event "
                    "WHERE actor=:actor_id "
                    "AND action='configuration.policy.publish_denied'"
                ),
                {"actor_id": actor_id},
            ).all()
            persisted = db.execute(
                text(
                    "SELECT request_fingerprint, encode(sealed_response, 'hex') "
                    "FROM identity.configuration_idempotency_record "
                    "WHERE actor=:actor_id AND operation='draft_publish'"
                ),
                {"actor_id": actor_id},
            ).one()

        assert first.status_code == replay.status_code == 403
        assert first.json()["code"] == replay.json()["code"] == "REAUTHENTICATION_FAILED"
        assert first.json()["requestId"] == "req-wrongtotpfirst"
        assert replay.json()["requestId"] == "req-wrongtotpreplay"
        assert pointer == 1
        assert versions == 0
        assert [tuple(row) for row in challenges] == [(1, None)]
        assert [tuple(row) for row in denials] == [
            ("namespace=identity; reasonCode=REAUTHENTICATION_FAILED", "req-wrongtotpfirst")
        ]
        assert invalid_code not in first.text
        assert invalid_code not in replay.text
        assert invalid_code not in denials[0][0]
        assert invalid_code not in persisted[0]
        assert invalid_code not in persisted[1]
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor_id"),
                {"actor_id": actor_id},
            )
            db.execute(text("DELETE FROM identity.draft WHERE id=:id"), {"id": draft.id})
        _delete_policy_admin(configuration_owner_engine, actor_id)


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["publish", "rollback"])
def test_current_super_admin_recheck_denial_is_durable_after_guard_race(
    operation: str,
    configuration_rw_engine: Engine,
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft
    from control_plane.app.modules.configuration.api import (
        ConfigurationHttpRuntime,
        create_configuration_router,
    )

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    dependencies = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    with configuration_rw_engine.begin() as db:
        draft = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 31},
            actor_id=actor_id,
            dependencies=dependencies,
        )

    runtime = ConfigurationHttpRuntime(
        engine=configuration_rw_engine,
        dependencies=dependencies,
        secret_manager=identity_deps.secret_manager,
        policy_commands=IdentityPolicyCommandRuntime(identity_rw_engine, identity_deps),
    )
    guard_calls = 0
    runtime_calls = 0

    def capability_guard(_principal: object, _capability: str, _scope: str | None) -> None:
        nonlocal guard_calls
        guard_calls += 1

    def runtime_provider() -> ConfigurationHttpRuntime:
        nonlocal runtime_calls
        runtime_calls += 1
        assert guard_calls == runtime_calls
        if runtime_calls == 1:
            with configuration_owner_engine.begin() as db:
                changed = db.execute(
                    text(
                        "UPDATE identity.account SET status='DISABLED', updated_at=:now "
                        "WHERE id=:actor_id AND status='ENABLED'"
                    ),
                    {"actor_id": actor_id, "now": dependencies.clock.now()},
                )
                assert changed.rowcount == 1
        return runtime

    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_configuration_router(
            runtime_provider,
            lambda: SimpleNamespace(account_id=actor_id),
            capability_guard,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    key = f"current-admin-race-{operation}-{uuid4()}"
    if operation == "publish":
        path = f"/api/v1/admin/policies/identity/drafts/{draft.id}/publish"
        body: dict[str, Any] = {
            "reason": "publish only after owner transaction recheck",
            "totpCode": pyotp.TOTP(secret).at(identity_deps.clock.now()),
        }
        expected_audit = "configuration.policy.publish_denied"
        expected_idempotency_operation = "draft_publish"
    else:
        path = "/api/v1/admin/policies/identity/rollback"
        body = {
            "scope": "PLATFORM",
            "toVersion": 1,
            "reason": "rollback only after owner transaction recheck",
            "totpCode": pyotp.TOTP(secret).at(identity_deps.clock.now()),
        }
        expected_audit = "configuration.policy.rollback_denied"
        expected_idempotency_operation = "policy_rollback"
    headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "Idempotency-Key": key,
        "If-Match": '"v1"',
    }
    first_request_id = f"req-admin{operation}first"
    replay_request_id = f"req-admin{operation}replay"
    try:
        first = client.post(
            path,
            json=body,
            headers={**headers, "X-Request-ID": first_request_id},
        )
        replay = client.post(
            path,
            json=body,
            headers={**headers, "X-Request-ID": replay_request_id},
        )
        with configuration_owner_engine.connect() as db:
            facts = db.execute(
                text(
                    "SELECT "
                    "(SELECT version FROM identity.active_pointer "
                    " WHERE namespace='identity' AND scope='PLATFORM'), "
                    "(SELECT count(*) FROM identity.version "
                    " WHERE namespace='identity' AND scope='PLATFORM' AND version>1), "
                    "(SELECT count(*) FROM identity.configuration_outbox), "
                    "(SELECT count(*) FROM identity.auth_challenge "
                    " WHERE account_id=:actor_uuid AND purpose IN "
                    " ('POLICY_PUBLISH', 'POLICY_ROLLBACK'))"
                ),
                {"actor_uuid": actor_id},
            ).one()
            denials = db.execute(
                text(
                    "SELECT action, reason, correlation_id FROM audit.audit_event "
                    "WHERE actor=:actor_id AND action=:action ORDER BY occurred_at"
                ),
                {"actor_id": actor_id, "action": expected_audit},
            ).all()
            idempotency = db.execute(
                text(
                    "SELECT state, http_status FROM identity.configuration_idempotency_record "
                    "WHERE actor=:actor_id AND operation=:operation AND idempotency_key=:key"
                ),
                {
                    "actor_id": actor_id,
                    "operation": expected_idempotency_operation,
                    "key": key,
                },
            ).one()

        assert first.status_code == replay.status_code == 403
        assert first.json()["code"] == replay.json()["code"] == "REAUTHENTICATION_FAILED"
        assert first.json()["requestId"] == first_request_id
        assert replay.json()["requestId"] == replay_request_id
        assert facts == (1, 0, 0, 0)
        assert [tuple(row) for row in denials] == [
            (
                expected_audit,
                "namespace=identity; reasonCode=REAUTHENTICATION_FAILED",
                first_request_id,
            )
        ]
        assert tuple(idempotency) == ("COMPLETED", 403)
        assert guard_calls == runtime_calls == 2
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor"),
                {"actor": actor_id},
            )
            db.execute(text("DELETE FROM identity.draft WHERE id=:id"), {"id": draft.id})
        _delete_policy_admin(configuration_owner_engine, actor_id)


@pytest.mark.integration
def test_unexpected_publish_failure_rolls_back_every_fact_and_idempotency_claim(
    configuration_rw_engine: Engine,
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft
    from control_plane.app.modules.configuration.api import (
        ConfigurationHttpRuntime,
        create_configuration_router,
    )

    class _FailingAudit:
        def append_in_transaction(self, _db: object, _envelope: object) -> None:
            raise RuntimeError("credential-sentinel")

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    setup_dependencies = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    with configuration_rw_engine.begin() as db:
        draft = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 31},
            actor_id=actor_id,
            dependencies=setup_dependencies,
        )
    failing_identity_deps = replace(identity_deps, audit=_FailingAudit())
    failing_dependencies = replace(
        setup_dependencies,
        audit=_FailingAudit(),
    )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: identity_deps.clock.now().timestamp(),
    )
    runtime = ConfigurationHttpRuntime(
        engine=configuration_rw_engine,
        dependencies=failing_dependencies,
        secret_manager=identity_deps.secret_manager,
        policy_commands=IdentityPolicyCommandRuntime(
            identity_rw_engine,
            failing_identity_deps,
        ),
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(
        create_configuration_router(
            lambda: runtime,
            lambda: SimpleNamespace(account_id=actor_id),
            lambda _principal, _capability, _scope: None,
        )
    )
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    try:
        response = client.post(
            f"/api/v1/admin/policies/identity/drafts/{draft.id}/publish",
            json={
                "reason": "must roll back if final audit fails",
                "totpCode": pyotp.TOTP(secret).at(identity_deps.clock.now()),
            },
            headers={
                "Origin": "https://testserver",
                "Sec-Fetch-Site": "same-origin",
                "Idempotency-Key": f"publish-fail-{uuid4()}",
                "If-Match": '"v1"',
                "X-Request-ID": "req-publishrollback",
            },
        )
        with configuration_owner_engine.connect() as db:
            facts = db.execute(
                text(
                    "SELECT "
                    "(SELECT version FROM identity.active_pointer "
                    " WHERE namespace='identity' AND scope='PLATFORM'), "
                    "(SELECT count(*) FROM identity.version "
                    " WHERE namespace='identity' AND scope='PLATFORM' AND version>1), "
                    "(SELECT count(*) FROM identity.configuration_outbox "
                    " WHERE aggregate_id='identity:PLATFORM:2'), "
                    "(SELECT count(*) FROM identity.auth_challenge "
                    " WHERE account_id=:actor_uuid AND purpose='POLICY_PUBLISH'), "
                    "(SELECT count(*) FROM identity.configuration_idempotency_record "
                    " WHERE actor=:actor_text AND operation='draft_publish'), "
                    "(SELECT count(*) FROM audit.audit_event WHERE actor=:actor_text "
                    " AND action LIKE 'identity.super_admin.challenge.%')"
                ),
                {"actor_uuid": actor_id, "actor_text": actor_id},
            ).one()

        assert response.status_code == 500
        assert response.json() == {
            "title": "Internal server error",
            "status": 500,
            "requestId": "req-publishrollback",
        }
        assert facts == (1, 0, 0, 0, 0, 0)
        assert "credential-sentinel" not in response.text
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(text("DELETE FROM identity.draft WHERE id=:id"), {"id": draft.id})
        _delete_policy_admin(configuration_owner_engine, actor_id)


@pytest.mark.integration
def test_publish_atomically_activates_immutable_version_and_stales_other_drafts(
    configuration_rw_engine: Engine,
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configuration_seed
    from control_plane.app.modules.configuration import create_draft, preview

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    dependencies = ConfigurationDependencies(
        clock=_Clock(),
        random=_Random(),
        audit=SqlAlchemyTransactionalAuditAppender(),
    )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: identity_deps.clock.now().timestamp(),
    )
    with configuration_owner_engine.connect() as db:
        history_before = db.execute(
            text(
                "SELECT snapshot::text, snapshot_hash FROM identity.version "
                "WHERE namespace='identity' AND scope='PLATFORM' AND version=1"
            )
        ).one()
    with configuration_rw_engine.begin() as db:
        other = create_draft(
            db,
            namespace="identity",
            values={"identity.session_cap": 4},
            actor_id=actor_id,
            dependencies=dependencies,
        )
        source = create_draft(
            db,
            namespace="identity",
            values={"identity.session_idle_timeout": 30},
            actor_id=actor_id,
            dependencies=dependencies,
        )
        preview(
            db,
            namespace="identity",
            draft_id=source.id,
            actor_id=actor_id,
            expected_revision=source.revision,
            dependencies=dependencies,
        )
    owner_runtime = IdentityPolicyCommandRuntime(identity_rw_engine, identity_deps)
    try:
        published = owner_runtime.publish(
            namespace="identity",
            draft_id=source.id,
            actor_id=actor_id,
            expected_revision=source.revision,
            reason="reduce idle exposure",
            totp_code=pyotp.TOTP(secret).at(identity_deps.clock.now()),
            idempotency_key=f"atomic-publish-{uuid4()}",
        )
        with configuration_owner_engine.connect() as db:
            active_version = db.execute(
                text(
                    "SELECT version FROM identity.active_pointer "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            ).scalar_one()
            stored = (
                db.execute(
                    text(
                        "SELECT snapshot, changeset, validation_evidence, preview_evidence, "
                        "published_at, activated_at FROM identity.version "
                        "WHERE namespace='identity' AND scope='PLATFORM' AND version=2"
                    )
                )
                .mappings()
                .one()
            )
            history_after = db.execute(
                text(
                    "SELECT snapshot::text, snapshot_hash FROM identity.version "
                    "WHERE namespace='identity' AND scope='PLATFORM' AND version=1"
                )
            ).one()
            other_stale = db.execute(
                text("SELECT stale FROM identity.draft WHERE id=:draft_id"),
                {"draft_id": other.id},
            ).scalar_one()
            outbox_count = db.execute(
                text(
                    "SELECT count(*) FROM identity.configuration_outbox "
                    "WHERE event_type='POLICY_PUBLISHED' AND aggregate_id='identity:PLATFORM:2'"
                )
            ).scalar_one()

        assert published.status_code == 201
        assert published.body["version"] == active_version == 2
        assert stored["snapshot"]["identity.session_idle_timeout"] == 30
        assert stored["changeset"]["items"] == [
            {
                "key": "identity.session_idle_timeout",
                "before": 60,
                "after": 30,
            }
        ]
        assert stored["validation_evidence"]["valid"] is True
        assert stored["preview_evidence"]["items"][0]["effect_semantics"] == "IMMEDIATE"
        assert stored["published_at"] == stored["activated_at"]
        assert history_after == history_before
        assert other_stale is True
        assert outbox_count == 1
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor"),
                {"actor": actor_id},
            )
            db.execute(
                text(
                    "UPDATE identity.active_pointer SET version=1 "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.configuration_outbox "
                    "WHERE aggregate_id='identity:PLATFORM:2'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.version WHERE namespace='identity' "
                    "AND scope='PLATFORM' AND version=2"
                )
            )
            db.execute(
                text("DELETE FROM identity.draft WHERE id IN (:other, :source)"),
                {"other": other.id, "source": source.id},
            )
        _delete_policy_admin(configuration_owner_engine, actor_id)


@pytest.mark.integration
def test_rollback_creates_a_new_draft_and_republish_uses_a_higher_version(
    configuration_rw_engine: Engine,
    identity_rw_engine: Engine,
    configuration_owner_engine: Engine,
    configuration_seed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configuration_seed
    del configuration_rw_engine

    identity_deps = replace(identity_dependencies(), policy=IdentityEffectivePolicy())
    actor_id, secret = _insert_policy_admin(
        configuration_owner_engine,
        dependencies=identity_deps,
    )
    snapshot = {
        "identity.temp_credential_ttl": 24,
        "identity.password_max_age": "NEVER",
        "identity.session_cap": 3,
        "identity.session_idle_timeout": 30,
        "identity.login_backoff": {
            "failureThreshold": 5,
            "initialDelaySeconds": 30,
            "maximumDelaySeconds": 900,
            "resetAfterHours": 24,
        },
        "identity.totp_attempt_cap": 5,
        "identity.draft_archive_after": 30,
    }
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    import hashlib

    snapshot_hash = hashlib.sha256(canonical).hexdigest()
    with configuration_owner_engine.begin() as db:
        history_before = db.execute(
            text(
                "SELECT snapshot::text, snapshot_hash FROM identity.version "
                "WHERE namespace='identity' AND scope='PLATFORM' AND version=1"
            )
        ).one()
        db.execute(
            text(
                "INSERT INTO identity.version ("
                "namespace, scope, version, snapshot, changeset, published_by, reason, "
                "published_at, activated_at, schema_revision, snapshot_hash, "
                "validation_evidence, dependency_versions, preview_evidence"
                ") VALUES ("
                "'identity', 'PLATFORM', 2, CAST(:snapshot AS JSONB), "
                "CAST(:changeset AS JSONB), "
                "'test-seed', 'test current version', now(), now(), 1, :snapshot_hash, "
                "CAST(:validation AS JSONB), CAST(:dependencies AS JSONB), "
                "CAST(:preview AS JSONB))"
            ),
            {
                "snapshot": json.dumps(snapshot, separators=(",", ":")),
                "changeset": json.dumps({"items": []}),
                "snapshot_hash": snapshot_hash,
                "validation": json.dumps({"valid": True}),
                "dependencies": json.dumps({}),
                "preview": json.dumps({"items": []}),
            },
        )
        db.execute(
            text(
                "UPDATE identity.active_pointer SET version=2 "
                "WHERE namespace='identity' AND scope='PLATFORM'"
            )
        )
    monkeypatch.setattr(
        "control_plane.app.shared.security.totp.time.time",
        lambda: identity_deps.clock.now().timestamp(),
    )
    owner_runtime = IdentityPolicyCommandRuntime(identity_rw_engine, identity_deps)
    rollback_draft_id: str | None = None
    try:
        draft = owner_runtime.rollback(
            namespace="identity",
            scope="PLATFORM",
            to_version=1,
            actor_id=actor_id,
            expected_version=2,
            reason="restore prior idle policy",
            totp_code=pyotp.TOTP(secret).at(identity_deps.clock.now()),
            idempotency_key=f"rollback-history-{uuid4()}",
        )
        rollback_draft_id = str(draft.body["id"])
        with configuration_owner_engine.connect() as db:
            pointer_after_draft = db.execute(
                text(
                    "SELECT version FROM identity.active_pointer "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            ).scalar_one()
        identity_deps.clock.value += timedelta(seconds=30)  # type: ignore[attr-defined]
        published = owner_runtime.publish(
            namespace="identity",
            draft_id=rollback_draft_id,
            actor_id=actor_id,
            expected_revision=int(draft.body["revision"]),
            reason="publish reviewed rollback",
            totp_code=pyotp.TOTP(secret).at(identity_deps.clock.now()),
            idempotency_key=f"publish-rollback-{uuid4()}",
        )
        with configuration_owner_engine.connect() as db:
            history_after = db.execute(
                text(
                    "SELECT snapshot::text, snapshot_hash FROM identity.version "
                    "WHERE namespace='identity' AND scope='PLATFORM' AND version=1"
                )
            ).one()

        assert draft.body["baseVersion"] == pointer_after_draft == 2
        assert draft.body["rollbackFromVersion"] == 1
        assert published.body["version"] == 3
        assert published.body["snapshot"] == draft.body["content"]
        assert history_after == history_before
    finally:
        with configuration_owner_engine.begin() as db:
            db.execute(
                text("DELETE FROM identity.configuration_idempotency_record WHERE actor=:actor"),
                {"actor": actor_id},
            )
            db.execute(
                text(
                    "UPDATE identity.active_pointer SET version=1 "
                    "WHERE namespace='identity' AND scope='PLATFORM'"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.configuration_outbox "
                    "WHERE aggregate_id IN ('identity:PLATFORM:2', 'identity:PLATFORM:3')"
                )
            )
            db.execute(
                text(
                    "DELETE FROM identity.version WHERE namespace='identity' "
                    "AND scope='PLATFORM' AND version IN (2, 3)"
                )
            )
            if rollback_draft_id is not None:
                db.execute(
                    text("DELETE FROM identity.draft WHERE id=:draft_id"),
                    {"draft_id": rollback_draft_id},
                )
        _delete_policy_admin(configuration_owner_engine, actor_id)
