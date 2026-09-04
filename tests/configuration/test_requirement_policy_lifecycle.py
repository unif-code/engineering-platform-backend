from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pyotp
import pytest
from sqlalchemy import Connection, event, text

from control_plane.app.modules import requirement
from control_plane.app.modules.authorization import DecisionDependencies
from control_plane.app.modules.authorization.adapters import SqlAlchemyIdentitySessionValidator
from control_plane.app.modules.configuration import ConfigurationDependencies, Draft
from control_plane.app.modules.configuration.adapters import IdentityEffectivePolicy
from control_plane.app.modules.identity import IdentityPolicyReauthenticationRuntime
from control_plane.app.shared.idempotency import IdempotentResponse
from tests.authorization.helpers import authorization_dependencies
from tests.authorization.test_decisions import Membership
from tests.configuration.conftest import _temporary_runtime_role_engine
from tests.identity.task5_helpers import MutableClock, dependencies
from tests.identity.test_auth_flow import _initialize_account
from tests.requirement.conftest import (  # noqa: F401
    isolated_requirement_database,
    requirement_owner_engine,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("field,value", [("scope", "WORKSPACE"), ("schemaRevision", 2)])
def test_draft_request_does_not_silently_discard_owner_identity(field: str, value: object) -> None:
    from pydantic import ValidationError

    from control_plane.app.modules.configuration.api.dto import DraftValuesRequestDto

    with pytest.raises(ValidationError):
        DraftValuesRequestDto.model_validate({"values": {}, field: value})


@pytest.mark.parametrize(
    "mutation",
    [
        "owner",
        "revision",
        "hash",
        "scope",
        "schema",
        "base",
        "validation",
        "preview",
        "dependencies",
        "authorization_missing",
        "authorization_dirty",
    ],
)
def test_publication_rejects_stale_or_unqualified_owner_facts(
    ready: SimpleNamespace, mutation: str
) -> None:
    draft = prepared(ready)
    if mutation == "owner":
        ready.actor = str(uuid4())
    elif mutation == "revision":
        draft = draft.model_copy(update={"revision": draft.revision + 1})
    else:
        statement = {
            "hash": "UPDATE requirement.gate_policy_draft SET content_hash=repeat('a',64)",
            "scope": None,
            "schema": "UPDATE requirement.gate_policy_draft SET schema_revision=2",
            "base": "UPDATE requirement.gate_policy_draft SET base_version=2",
            "validation": "UPDATE requirement.gate_policy_draft SET validation_evidence=NULL",
            "preview": "UPDATE requirement.gate_policy_draft SET preview_evidence=NULL",
            "dependencies": (
                "UPDATE requirement.gate_policy_draft SET validation_evidence="
                "jsonb_set(validation_evidence,'{dependency_versions}','{\"unknown\":1}')"
            ),
            "authorization_missing": 'DELETE FROM "authorization".principal_version',
            "authorization_dirty": (
                'UPDATE "authorization".principal_version SET '
                "fence_generation=1,dirty_generation=1,dirty_reason='TEST'"
            ),
        }[mutation]
        if statement:
            with ready.database.owner.begin() as db:
                db.exec_driver_sql(statement)
    if mutation == "scope":
        result = ready.runtime.rollback(
            actor_id=ready.actor,
            namespace="requirement.gate",
            scope="WORKSPACE",
            to_version=1,
            expected_version=1,
            reason="No workspace policy",
            totp_code=pyotp.TOTP(ready.secret).at(ready.deps.clock.now()),
            raw_session=ready.token,
            idempotency_key=str(uuid4()),
        )
    else:
        result = publish(ready, draft)
    assert result.status_code in (403, 409, 422)
    assert ready.runtime.resolved_snapshot().version == 1
    with ready.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "expiry",
        "authorization_version",
        "authorization_fence",
        "account_version",
        "session_revoked",
    ],
)
def test_final_check_burns_receipt_on_current_fact_drift(
    ready: SimpleNamespace, mutation: str
) -> None:
    draft = prepared(ready)
    fired = False

    def mutate(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        nonlocal fired
        if fired or not statement.startswith("INSERT INTO identity.policy_reauth_consumption"):
            return
        fired = True
        if mutation == "expiry":
            ready.deps.clock.value += timedelta(minutes=6)
        elif mutation.startswith("authorization"):
            column = "version" if mutation == "authorization_version" else "fence_generation"
            with ready.database.owner.begin() as other:
                other.execute(
                    text(f'UPDATE "authorization".principal_version SET {column}={column}+1')
                )
        elif mutation == "account_version":
            conn.execute(text("UPDATE identity.account SET version=version+1"))
        else:
            conn.execute(text("UPDATE identity.session SET revoked_at=now(),revoke_reason='TEST'"))

    event.listen(ready.identity_engine, "after_cursor_execute", mutate)
    try:
        assert publish(ready, draft).status_code == 403
    finally:
        event.remove(ready.identity_engine, "after_cursor_execute", mutate)
    assert fired
    with ready.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 1
        )
        assert (
            db.execute(text("SELECT count(*) FROM requirement.gate_policy_receipt")).scalar_one()
            == 0
        )
    assert ready.runtime.resolved_snapshot().version == 1


def test_same_key_race_replays_one_publication_and_receipt(ready: SimpleNamespace) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    draft = prepared(ready)
    barrier = Barrier(2)
    key = str(uuid4())

    def run() -> IdempotentResponse:
        barrier.wait(timeout=5)
        return publish(ready, draft, idempotency_key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    assert results[0].status_code == 201
    with ready.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM requirement.gate_policy_receipt")).scalar_one()
            == 1
        )
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 1
        )


def test_publication_and_archive_share_pointer_before_draft_locks(
    ready: SimpleNamespace,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic

    first, second = sorted((prepared(ready), prepared(ready)), key=lambda draft: draft.id)
    publication_locked = Event()
    archive_entered = Event()
    archive_wait_observed = Event()
    archive_pid: list[int] = []

    def pause_publication_after_candidate_lock(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            publication_locked.is_set()
            or not statement.startswith("SELECT * FROM requirement.gate_policy_draft WHERE id=")
            or not statement.endswith(" FOR UPDATE")
            or parameters.get("id") != second.id
        ):
            return
        conn.execute(text("SET LOCAL statement_timeout='8s'"))
        publication_pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        publication_locked.set()
        assert archive_entered.wait(5), "archival participant did not enter its transaction"
        deadline = monotonic() + 5
        while monotonic() < deadline:
            # Before the fix archival owns A and waits for B; after the fix it
            # waits at the pointer without acquiring either draft. Do not release
            # publication until PostgreSQL confirms this actual blocking edge.
            blocked = conn.execute(
                text("SELECT :publication = ANY(pg_blocking_pids(:archival))"),
                {"publication": publication_pid, "archival": archive_pid[0]},
            ).scalar_one()
            if blocked:
                archive_wait_observed.set()
                return
            archive_wait_observed.wait(0.01)
        raise AssertionError("archival did not block on the publication transaction")

    def archive() -> int:
        assert publication_locked.wait(5), "publication did not lock candidate B"
        with ready.runtime.transaction() as lifecycle:
            lifecycle.db.execute(text("SET LOCAL statement_timeout='8s'"))
            archive_pid.append(lifecycle.db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            archive_entered.set()
            return cast(
                int,
                lifecycle.archive(
                    now=ready.deps.clock.now() + timedelta(days=31), namespace="requirement.gate"
                ),
            )

    event.listen(
        ready.database.runtime, "after_cursor_execute", pause_publication_after_candidate_lock
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            publishing = pool.submit(publish, ready, second)
            archiving = pool.submit(archive)
            response = publishing.result(timeout=15)
            archived = archiving.result(timeout=15)
    finally:
        event.remove(
            ready.database.runtime, "after_cursor_execute", pause_publication_after_candidate_lock
        )

    assert archive_wait_observed.is_set()
    assert response.status_code == 201
    assert response.body["version"] == 2
    assert archived == 2
    assert ready.runtime.resolved_snapshot().version == 2
    with ready.database.owner.connect() as db:
        assert set(
            db.execute(
                text("SELECT id::text FROM requirement.gate_policy_draft WHERE status='ARCHIVED'")
            ).scalars()
        ) == {first.id, second.id}
        assert list(
            db.execute(
                text(
                    "SELECT payload->>'policyVersion' FROM requirement.gate_policy_outbox "
                    "WHERE event_type='DRAFT_ARCHIVED'"
                )
            ).scalars()
        ) == ["2", "2"]
        assert (
            db.execute(text("SELECT count(*) FROM requirement.gate_policy_receipt")).scalar_one()
            == 1
        )
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 1
        )


def test_different_rollback_keys_cannot_consume_same_totp_twice(ready: SimpleNamespace) -> None:
    args = dict(
        actor_id=ready.actor,
        namespace="requirement.gate",
        scope="PLATFORM",
        to_version=1,
        expected_version=1,
        reason="Candidate only",
        totp_code=pyotp.TOTP(ready.secret).at(ready.deps.clock.now()),
        raw_session=ready.token,
    )
    first = ready.runtime.rollback(**args, idempotency_key=str(uuid4()))
    second = ready.runtime.rollback(**args, idempotency_key=str(uuid4()))
    assert first.status_code == 201
    assert second.status_code == 403
    assert ready.runtime.resolved_snapshot().version == 1
    with ready.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM requirement.gate_policy_draft")).scalar_one() == 1
        )


@pytest.fixture
def ready(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    database = request.getfixturevalue("isolated_requirement_database")
    with ExitStack() as stack:
        identity_engine = stack.enter_context(
            _temporary_runtime_role_engine(
                database.owner, database.owner.url, privilege_role="identity_rw"
            )
        )[0]
        auth_engine = stack.enter_context(
            _temporary_runtime_role_engine(
                database.owner, database.owner.url, privilege_role="authorization_rw"
            )
        )[0]
        deps = replace(dependencies(), policy=IdentityEffectivePolicy())
        secret, token = _initialize_account(identity_engine, deps, monkeypatch)
        with database.owner.begin() as db:
            actor = str(
                db.execute(
                    text(
                        "UPDATE identity.account SET is_super_admin=true,version=version+1 WHERE "
                        "employee_no='00000001' RETURNING id"
                    )
                ).scalar_one()
            )
            db.execute(
                text(
                    "INSERT INTO "
                    '"authorization".principal_version'
                    "(account_id,version,fence_generation,updated_at) "
                    "VALUES (:actor,1,0,now())"
                ),
                {"actor": actor},
            )
        cast(MutableClock, deps.clock).value += timedelta(seconds=30)
        authorization_type = getattr(requirement, "RequirementPolicyAuthorization", None)
        assert authorization_type is not None, "protected Requirement policy lifecycle is missing"
        authorization = authorization_type(
            auth_engine,
            replace(authorization_dependencies(), clock=deps.clock),
            DecisionDependencies(
                identity=SqlAlchemyIdentitySessionValidator(identity_engine, deps),
                workspace=Membership(),
            ),
        )
        runtime = requirement.RequirementPolicyRuntime(
            database.runtime,
            ConfigurationDependencies(clock=deps.clock, random=deps.random, audit=deps.audit),
            secret_manager=deps.secret_manager,
            reauthentication=IdentityPolicyReauthenticationRuntime(identity_engine, deps),
            authorization=authorization,
        )
        yield SimpleNamespace(
            database=database,
            runtime=runtime,
            deps=deps,
            actor=actor,
            token=token,
            secret=secret,
            identity_engine=identity_engine,
        )


def prepared(ready: SimpleNamespace) -> Draft:
    with ready.runtime.transaction() as lifecycle:
        draft = lifecycle.create_draft(
            namespace="requirement.gate",
            values={"acceptance.additional_required_capabilities": ["code.change"]},
            actor_id=ready.actor,
        )
        result = lifecycle.validate_draft(
            namespace="requirement.gate",
            draft_id=draft.id,
            actor_id=ready.actor,
            expected_revision=1,
        )
        lifecycle.preview(
            namespace="requirement.gate",
            draft_id=draft.id,
            actor_id=ready.actor,
            expected_revision=result.revision,
        )
        return cast(Draft, lifecycle.owner.draft(draft.id))


def publish(ready: SimpleNamespace, draft: Draft, **changes: Any) -> IdempotentResponse:
    return cast(
        IdempotentResponse,
        ready.runtime.publish(
            actor_id=ready.actor,
            namespace="requirement.gate",
            draft_id=draft.id,
            expected_revision=draft.revision,
            reason="Strengthen Gate",
            totp_code=pyotp.TOTP(ready.secret).at(ready.deps.clock.now()),
            raw_session=ready.token,
            idempotency_key=changes.pop("idempotency_key", str(uuid4())),
            **changes,
        ),
    )


def test_publish_replay_and_separately_authenticated_rollback(ready: SimpleNamespace) -> None:
    draft = prepared(ready)
    key = str(uuid4())
    response = publish(ready, draft, idempotency_key=key)
    assert response.status_code == 201
    assert response.body["version"] == 2
    assert publish(ready, draft, idempotency_key=key) == response
    ready.deps.clock.value += timedelta(seconds=30)
    args = dict(
        actor_id=ready.actor,
        namespace="requirement.gate",
        scope="PLATFORM",
        to_version=1,
        expected_version=2,
        reason="Revert content",
        totp_code=pyotp.TOTP(ready.secret).at(ready.deps.clock.now()),
        raw_session=ready.token,
        idempotency_key=str(uuid4()),
    )
    rollback = ready.runtime.rollback(**args)
    assert rollback.status_code == 201
    assert ready.runtime.rollback(**args) == rollback
    assert ready.runtime.resolved_snapshot().version == 2
    with ready.runtime.transaction() as lifecycle:
        candidate = lifecycle.owner.draft(rollback.body["id"])
    assert candidate.rollback_from_version == 1
    assert publish(ready, candidate).status_code == 422
    with ready.runtime.transaction() as lifecycle:
        checked = lifecycle.validate_draft(
            namespace="requirement.gate",
            draft_id=candidate.id,
            actor_id=ready.actor,
            expected_revision=1,
        )
        lifecycle.preview(
            namespace="requirement.gate",
            draft_id=candidate.id,
            actor_id=ready.actor,
            expected_revision=checked.revision,
        )
        candidate = lifecycle.owner.draft(candidate.id)
    assert publish(ready, candidate).status_code == 403
    ready.deps.clock.value += timedelta(seconds=30)
    assert publish(ready, candidate).body["version"] == 3
    with ready.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM requirement.gate_policy_receipt")).scalar_one()
            == 3
        )
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 3
        )


def test_owner_failure_burns_identity_consumption(ready: SimpleNamespace) -> None:
    draft = prepared(ready)

    def fail(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "INSERT INTO requirement.gate_policy_version" in statement:
            raise RuntimeError("injected owner failure")

    event.listen(ready.database.runtime, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            publish(ready, draft)
    finally:
        event.remove(ready.database.runtime, "before_cursor_execute", fail)
    assert ready.runtime.resolved_snapshot().version == 1
    with ready.database.owner.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM identity.policy_reauth_consumption")).scalar_one()
            == 1
        )
        assert (
            db.execute(text("SELECT count(*) FROM requirement.gate_policy_receipt")).scalar_one()
            == 0
        )
    assert publish(ready, draft).status_code == 403


def test_http_routes_dispatch_all_requirement_paths_without_identity_writes(
    ready: SimpleNamespace,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from control_plane.app.modules.configuration import IdentityPolicyRuntime, PolicyRuntimeRegistry
    from control_plane.app.modules.configuration.api import (
        ConfigurationHttpRuntime,
        create_configuration_router,
    )
    from control_plane.app.modules.identity import IdentityPolicyCommandRuntime

    runtime = ConfigurationHttpRuntime(
        owners=PolicyRuntimeRegistry(
            IdentityPolicyRuntime(
                ready.identity_engine,
                ready.runtime.dependencies,
                IdentityPolicyCommandRuntime(ready.identity_engine, ready.deps),
            ),
            ready.runtime,
        ),
        dependencies=ready.runtime.dependencies,
        secret_manager=ready.deps.secret_manager,
    )
    app = FastAPI()
    app.include_router(
        create_configuration_router(
            lambda: runtime,
            lambda: SimpleNamespace(account_id=ready.actor, is_super_admin=True),
            lambda *_: ready.runtime.authorization.check(
                raw_session=ready.token, actor_id=ready.actor
            ),
        )
    )
    with TestClient(app) as client:
        client.cookies.set("ep_session", ready.token)
        catalog = client.get("/api/v1/admin/policies?namespace=requirement.gate")
        assert catalog.status_code == 200
        assert len(catalog.json()["items"]) == 3
        assert client.get("/api/v1/admin/policies").json()["active"]["namespace"] == "identity"
        assert client.get("/api/v1/admin/policies?namespace=unknown").status_code == 503
        base = "/api/v1/admin/policies/requirement.gate"
        headers = {"Origin": "http://testserver", "Idempotency-Key": str(uuid4())}
        created = client.post(
            base + "/drafts", json={"values": {"draft_archive_after_days": 12}}, headers=headers
        )
        assert created.status_code == 201
        assert (
            client.post(
                base + "/drafts", json={"values": {"draft_archive_after_days": 12}}, headers=headers
            ).json()
            == created.json()
        )
        draft_id = created.json()["id"]
        assert client.get(base + "/drafts/" + draft_id).status_code == 200
        updated = client.patch(
            base + "/drafts/" + draft_id,
            json={"values": {"draft_archive_after_days": 13}},
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "If-Match": created.headers["etag"],
            },
        )
        assert updated.status_code == 200
        validated = client.post(
            base + "/drafts/" + draft_id + "/validate",
            json={},
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "If-Match": updated.headers["etag"],
            },
        )
        assert validated.status_code == 200
        assert (
            client.get(
                base + "/drafts/" + draft_id + "/preview",
                headers={"If-Match": validated.headers["etag"]},
            ).status_code
            == 200
        )
        published = client.post(
            base + "/drafts/" + draft_id + "/publish",
            json={
                "reason": "Archive next schedule",
                "totpCode": pyotp.TOTP(ready.secret).at(ready.deps.clock.now()),
            },
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "If-Match": validated.headers["etag"],
            },
        )
        assert published.status_code == 201
        assert client.get(base + "/active").json()["version"] == 2
        assert client.get(base + "/versions/2").json()["version"] == 2
        assert len(client.get(base + "/versions").json()["items"]) == 2
    with ready.database.owner.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity.draft")).scalar_one() == 0
        assert (
            db.execute(
                text("SELECT count(*) FROM identity.configuration_idempotency_record")
            ).scalar_one()
            == 0
        )


def test_archive_cli_runs_each_owner_transaction(ready: SimpleNamespace) -> None:
    from io import StringIO

    from control_plane.app.modules.configuration import IdentityPolicyRuntime, PolicyRuntimeRegistry
    from control_plane.tools.archive_drafts import main

    draft = prepared(ready)
    ready.deps.clock.value += timedelta(days=31)
    output = StringIO()
    registry = PolicyRuntimeRegistry(
        IdentityPolicyRuntime(ready.identity_engine, ready.runtime.dependencies), ready.runtime
    )
    assert main([], owners=registry, dependencies=ready.runtime.dependencies, stdout=output) == 0
    assert output.getvalue().strip() == '{"archivedDrafts":1}'
    with ready.runtime.transaction() as lifecycle:
        assert lifecycle.owner.draft(draft.id).status == "ARCHIVED"
