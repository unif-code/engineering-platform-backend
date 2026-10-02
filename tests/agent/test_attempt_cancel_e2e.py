import importlib
import json
from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.bootstrap.app import _DEFAULT_NAVIGATION_ACTION_CAPABILITIES
from control_plane.app.modules.agent import accept_workflow_event
from control_plane.app.modules.agent.adapters.sqlalchemy import (
    SqlAlchemyAgentRepository,
    SqlAlchemyAgentUnitOfWork,
)
from control_plane.app.modules.agent.domain import ExecutionBindingSource
from control_plane.app.modules.authorization.api.routes import _published_navigation_meta
from tests.agent.test_events import event
from tests.agent.test_repository import DEFINITION
from tests.agent.test_run_queries import seed_run
from tests.source_control.test_v06_production_e2e import Journey, _grant, _revoke
from tests.source_control.test_v06_production_e2e import journey as journey
from tests.source_control.test_v06_production_e2e import production_database as production_database
from tests.test_e2e_access_governance import SAME_ORIGIN


def test_default_navigation_publishes_existing_control_without_execution_actions() -> None:
    control = {"capability": "agent.run.control", "scopeType": "WORKSPACE"}
    result = _published_navigation_meta(
        {
            "actionCapabilities": [
                control,
                {"capability": "agent.run.execute", "scopeType": "WORKSPACE"},
            ]
        },
        published_action_capabilities=_DEFAULT_NAVIGATION_ACTION_CAPABILITIES,
    )
    assert result["actionCapabilities"] == [control]


def _cancel(
    client: TestClient, run_id: str, attempt_id: str, revision: int, key: str
) -> httpx.Response:
    return cast(
        httpx.Response,
        client.post(
            f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/cancel",
            json={},
            headers={**SAME_ORIGIN, "If-Match": f'"v{revision}"', "Idempotency-Key": key},
        ),
    )


def _facts(journey: Journey) -> dict[str, Sequence[Any]]:
    with journey.database.owner.connect() as db:
        values = {
            name: db.execute(text(f"SELECT to_jsonb(t) FROM agent.{name} t ORDER BY 1"))
            .scalars()
            .all()
            for name in (
                "agent_run",
                "agent_attempt",
                "execution_binding",
                "canonical_event",
                "workflow_command",
                "idempotency_key",
            )
        }
        values["cancel_audit"] = (
            db.execute(
                text(
                    "SELECT to_jsonb(t) FROM audit.audit_event t "
                    "WHERE action='agent.attempt.cancel' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
    return values


@pytest.mark.integration
@pytest.mark.parametrize("source", list(ExecutionBindingSource))
def test_default_session_finalizing_cancel_scope_replay_and_authoritative_readback(
    journey: Journey, source: ExecutionBindingSource
) -> None:
    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        run, attempt, binding = seed_run(repository, 300, journey.workspace_id, source=source)
        _, unrelated, _ = seed_run(repository, 400, journey.workspace_id)
        foreign, foreign_attempt, _ = seed_run(repository, 500, str(UUID(int=999)))
    runtime = bootstrap.agent_http_runtime()
    external = Mock(side_effect=AssertionError("cancel must only persist its WorkflowCommand"))
    guarded = replace(
        runtime,
        dependencies=replace(
            runtime.dependencies,
            binding_policy=SimpleNamespace(resolve=external),
            workflow_orchestrator=SimpleNamespace(
                start=external, cancel=external, resume=external, lookup=external
            ),
        ),
    )
    for sequence, kind in enumerate(
        ("ATTEMPT_PROVISIONING", "ATTEMPT_RUNNING", "ATTEMPT_FINALIZING"), 1
    ):
        attempt = accept_workflow_event(
            None,
            event=event(
                attempt.id,
                event_id=str(uuid4()),
                event_type=kind,
                sequence=sequence,
            ),
            dependencies=guarded.dependencies,
        ).attempt
    _grant(journey.admin, journey.member_id, "agent.run.read", journey.workspace_id)
    with patch.object(bootstrap, "agent_http_runtime", return_value=guarded):
        with TestClient(bootstrap.create_app(), base_url="https://testserver") as client:
            client.cookies.update(journey.member.cookies)
            route = next(
                row
                for row in client.get("/api/v1/navigation").json()
                if row["routeKey"] == "agent-runs"
            )
            assert route["capability"] == "agent.run.read"
            assert route["meta"]["actionCapabilities"] == [
                {"capability": "agent.run.control", "scopeType": "WORKSPACE"}
            ]
            assert not any(
                cap["capability"] == "agent.run.control"
                for cap in client.get("/api/v1/me").json()["capabilities"]
            )
            before = _facts(journey)
            assert (
                _cancel(client, run.id, attempt.id, attempt.revision, "readonly-cancel").status_code
                == 403
            )
            assert _facts(journey) == before
            _grant(journey.admin, journey.member_id, "agent.run.control", journey.workspace_id)
            stale = _cancel(
                client, run.id, attempt.id, attempt.revision - 1, "old-attempt-revision"
            )
            assert stale.status_code == 409 and stale.json()["code"] == "ATTEMPT_REVISION_CONFLICT"
            assert (
                _cancel(
                    client, run.id, unrelated.id, unrelated.revision, "cross-run-cancel"
                ).status_code
                == 404
            )
            assert (
                _cancel(
                    client,
                    foreign.id,
                    foreign_attempt.id,
                    foreign_attempt.revision,
                    "cross-workspace-cancel",
                ).status_code
                == 403
            )
            assert _facts(journey) == before
            key = "finalizing-cancel-http"
            with patch.object(
                SqlAlchemyAgentUnitOfWork,
                "append_audit_event",
                side_effect=SQLAlchemyError("PRIVATE_AUDIT_FAILURE"),
            ):
                failed = _cancel(client, run.id, attempt.id, attempt.revision, key)
            assert failed.status_code == 503 and "PRIVATE_AUDIT_FAILURE" not in failed.text
            assert _facts(journey) == before
            accepted = _cancel(client, run.id, attempt.id, attempt.revision, key)
            assert accepted.status_code == 202, accepted.text
            receipt = accepted.json()["attempt"]
            assert receipt["id"] == attempt.id and receipt["runId"] == run.id
            assert (
                receipt["bindingId"] == binding.id
                and receipt["runnerGeneration"] == attempt.runner_generation
            )
            assert receipt["state"] == "CANCELING" and receipt["revision"] == attempt.revision + 1
            assert accepted.headers["etag"] == f'"v{receipt["revision"]}"'
            detail = client.get(f"/api/v1/agent-runs/{run.id}").json()
            assert detail["run"]["state"] == "ACTIVE"
            assert detail["attempts"][0]["state"] == "CANCELING"
            assert detail["bindings"][0]["source"] == source.value
            assert (
                _cancel(
                    client, run.id, attempt.id, receipt["revision"], "already-canceling"
                ).status_code
                == 202
            )
            changed = _cancel(client, run.id, attempt.id, receipt["revision"], key)
            assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
            canceled = accept_workflow_event(
                None,
                event=event(
                    attempt.id,
                    event_id=str(uuid4()),
                    event_type="ATTEMPT_CANCELED",
                    sequence=attempt.event_sequence + 1,
                ),
                dependencies=guarded.dependencies,
            ).attempt
            detail = client.get(f"/api/v1/agent-runs/{run.id}").json()
            assert detail["run"]["state"] == detail["attempts"][0]["state"] == "CANCELED"
            assert detail["attempts"][0]["revision"] > receipt["revision"]
            terminal = _cancel(client, run.id, attempt.id, canceled.revision, "already-terminal")
            assert terminal.status_code == 202 and terminal.json()["attempt"]["state"] == "CANCELED"
            before_replay = _facts(journey)
            replay = _cancel(client, run.id, attempt.id, attempt.revision, key)
            assert replay.status_code == 202 and replay.content == accepted.content
            assert replay.headers["etag"] == accepted.headers["etag"]
            assert _facts(journey) == before_replay
            _revoke(journey, journey.member_id, "agent.run.control")
            assert _cancel(client, run.id, attempt.id, attempt.revision, key).status_code == 403
            assert _facts(journey) == before_replay
            assert client.get(f"/api/v1/agent-runs/{run.id}").status_code == 200
            _grant(journey.admin, journey.member_id, "agent.run.control", journey.workspace_id)
            assert (
                _cancel(client, run.id, attempt.id, attempt.revision, key).content
                == accepted.content
            )
            assert _facts(journey) == before_replay
    external.assert_not_called()
    assert len(before_replay["workflow_command"]) == 1
    assert before_replay["workflow_command"][0]["kind"] == "CANCEL"
    assert len(before_replay["cancel_audit"]) == len(before_replay["idempotency_key"]) == 3


@pytest.mark.integration
@pytest.mark.parametrize(
    ("pending", "status", "code"),
    [
        (True, 409, "IDEMPOTENCY_IN_PROGRESS"),
        (False, 503, "AGENT_REPLAY_UNAVAILABLE"),
    ],
)
def test_default_session_pending_or_unverifiable_cancel_never_reexecutes(
    journey: Journey, pending: bool, status: int, code: str
) -> None:
    with journey.database.engines["agent"].begin() as db:
        repository = SqlAlchemyAgentRepository(db)
        repository.insert_definition(DEFINITION)
        run, attempt, _ = seed_run(repository, 300, journey.workspace_id)
    _grant(journey.admin, journey.member_id, "agent.run.control", journey.workspace_id)
    key = "cancel-replay-evidence"
    assert _cancel(journey.member, run.id, attempt.id, attempt.revision, key).status_code == 202
    with journey.database.owner.begin() as db:
        if pending:
            db.execute(
                text(
                    "UPDATE agent.idempotency_key SET state='IN_PROGRESS', http_status=NULL, "
                    "result_metadata=NULL, sealed_response=NULL, completed_at=NULL WHERE key=:key"
                ),
                {"key": key},
            )
        else:
            db.execute(
                text("UPDATE agent.idempotency_key SET sealed_response=:sealed WHERE key=:key"),
                {"sealed": b"unverifiable", "key": key},
            )
    before = _facts(journey)
    response = _cancel(journey.member, run.id, attempt.id, attempt.revision, key)
    assert response.status_code == status and response.json()["code"] == code
    assert _facts(journey) == before


@pytest.mark.integration
def test_control_navigation_migration_preserves_grants_and_operator_metadata(
    journey: Journey,
) -> None:
    migration = importlib.import_module("migrations.authorization.0012_authorization_agent_control")
    route = text('SELECT meta FROM "authorization".route_registry WHERE route_key=:route_key')
    update = text(
        'UPDATE "authorization".route_registry SET meta=CAST(:meta AS JSONB) '
        "WHERE route_key=:route_key"
    )
    grants_query = text('SELECT to_jsonb(t) FROM "authorization"."grant" t ORDER BY 1')
    params = {"route_key": "agent-runs"}
    with journey.database.owner.connect() as db:
        transaction = db.begin()
        try:
            with patch.object(migration, "op", Operations(MigrationContext.configure(db))):
                grants = db.execute(grants_query).scalars().all()
                original = db.execute(route, params).scalar_one()
                migration.downgrade()
                expected = {
                    **original,
                    "operatorNote": "preserve",
                    "actionCapabilities": [
                        {"capability": "agent.run.control", "scopeType": "WORKSPACE"}
                    ],
                }
                old = {key: value for key, value in expected.items() if key != "actionCapabilities"}
                db.execute(update, {**params, "meta": json.dumps(old)})
                migration.upgrade()
                migration.upgrade()
                assert db.execute(route, params).scalar_one() == expected
                conflicting = {
                    **expected,
                    "actionCapabilities": [
                        {"capability": "operator.custom", "scopeType": "WORKSPACE"}
                    ],
                }
                db.execute(update, {**params, "meta": json.dumps(conflicting)})
                with pytest.raises(DBAPIError, match="conflicting Agent control action"):
                    with db.begin_nested():
                        migration.upgrade()
                migration.downgrade()
                assert db.execute(route, params).scalar_one() == conflicting
                assert db.execute(grants_query).scalars().all() == grants
        finally:
            transaction.rollback()
