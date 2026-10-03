"""Recovery through real sessions, authorization, Requirement and isolated PostgreSQL."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import control_plane.app.bootstrap.app as bootstrap
from control_plane.app.modules.agent import accept_workflow_event, dispatch_workflow_commands
from control_plane.app.modules.agent.adapters.sqlalchemy import SqlAlchemyAgentUnitOfWork
from control_plane.app.modules.agent.application.events import StaleRunnerGeneration
from control_plane.app.modules.agent.domain import CanonicalEventInput
from control_plane.app.modules.identity import SessionKind, validate_session
from control_plane.tools import bootstrap_admin, source_control_repository
from tests.agent.conftest import IsolatedAgentDatabase
from tests.test_e2e_access_governance import _initialize, _runtime_engine

pytestmark = pytest.mark.integration
WORKSPACE_ID = "20000000-0000-0000-0000-000000000801"
REPOSITORY_ID = "10000000-0000-0000-0000-000000000801"
DEFINITION_ID = "00000000-0000-0000-0000-000000000800"
CORRELATION = "agent-e2e-private-correlation"


def headers(key: str, etag: str | None = None) -> dict[str, str]:
    result = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "Idempotency-Key": key,
        "X-Request-ID": f"req-{key}",
    }
    if etag is not None:
        result["If-Match"] = etag
    return result


def _clear_runtime_caches() -> None:
    for name in (
        "identity_dependencies",
        "organization_dependencies",
        "workspace_dependencies",
        "authorization_dependencies",
        "configuration_dependencies",
        "requirement_dependencies",
        "requirement_policy_runtime",
        "actor_qualification_runtime",
        "source_control_dependencies",
        "agent_dependencies",
        "identity_http_runtime",
        "authorization_http_runtime",
        "organization_http_runtime",
        "workspace_http_runtime",
        "configuration_http_runtime",
        "requirement_http_runtime",
        "source_control_query_runtime",
        "agent_http_runtime",
        "security_change_orchestrator",
    ):
        getattr(bootstrap, name).cache_clear()


@dataclass
class AgentE2E:
    owner: Engine
    engines: dict[str, Engine]
    admin: TestClient
    member: TestClient
    member_id: str
    requirement_id: str
    work_item_id: str

    def grant(self, capability: str, *, principal_id: str | None = None) -> httpx.Response:
        body: dict[str, object] = {
            "principalId": principal_id or self.member_id,
            "capability": capability,
            "scopeType": "PLATFORM" if capability == "agent.definition.read" else "WORKSPACE",
            "source": "MANUAL",
            "reason": "Isolated Agent recovery exact capability",
        }
        if body["scopeType"] == "WORKSPACE":
            body["scopeId"] = WORKSPACE_ID
        response = self.admin.post(
            "/api/v1/admin/grants", json=body, headers=headers(f"grant-{uuid4().hex}")
        )
        assert response.status_code == 201, response.text
        return cast(httpx.Response, response)

    def start(self) -> httpx.Response:
        result = self.member.post(
            "/api/v1/agent-runs",
            json={
                "workspaceId": WORKSPACE_ID,
                "requirementId": self.requirement_id,
                "workItemId": self.work_item_id,
                "definitionId": DEFINITION_ID,
                "definitionVersion": 1,
                "goal": "Private goal never copied into Audit",
            },
            headers=headers("agent-e2e-start"),
        )
        assert result.status_code == 202, result.text
        return cast(httpx.Response, result)

    def event(
        self, attempt_id: str, kind: str, sequence: int, generation: int = 1
    ) -> CanonicalEventInput:
        data: dict[str, object] = {}
        if kind == "WAITING_INPUT":
            data = {
                "checkpoint": {
                    "id": str(uuid4()),
                    "artifact_id": "artifact-e2e-checkpoint",
                    "artifact_version": "1",
                    "content_sha256": "sha256:" + "a" * 64,
                    "schema_version": "1",
                    "adapter_version": "1",
                    "classification": "INTERNAL",
                },
                "waitingDeadline": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "question": {"prompt": "请确认本次受控 Workflow 输入。"},
            }
        return CanonicalEventInput(
            id=str(uuid4()),
            event_type=kind,
            attempt_id=attempt_id,
            generation=generation,
            sequence=sequence,
            correlation_id=CORRELATION,
            causation_id=None,
            trace_id="trace-e2e",
            span_id=f"span-{generation}-{sequence}",
            summary=f"Platform {kind}",
            data=data,
        )

    def accept(self, event: CanonicalEventInput) -> None:
        accept_workflow_event(None, event=event, dependencies=bootstrap.agent_dependencies())

    def waiting(self, started: httpx.Response) -> httpx.Response:
        attempt_id = started.json()["attempt"]["id"]
        dispatch_workflow_commands(None, limit=10, dependencies=bootstrap.agent_dependencies())
        for sequence, kind in (
            (2, "ATTEMPT_PROVISIONING"),
            (3, "ATTEMPT_RUNNING"),
            (4, "WAITING_INPUT"),
        ):
            self.accept(self.event(attempt_id, kind, sequence))
        result = self.member.get(f"/api/v1/agent-runs/{started.json()['run']['id']}")
        assert result.status_code == 200, result.text
        assert result.json()["attempts"][0]["state"] == "WAITING_INPUT"
        return cast(httpx.Response, result)

    def resume(
        self, started: httpx.Response, revision: int, key: str = "agent-e2e-resume"
    ) -> httpx.Response:
        run_id, attempt_id = started.json()["run"]["id"], started.json()["attempt"]["id"]
        return cast(
            httpx.Response,
            self.member.post(
                f"/api/v1/agent-runs/{run_id}/attempts/{attempt_id}/resume",
                json={},
                headers=headers(key, f'"v{revision}"'),
            ),
        )

    def facts(self) -> dict[str, list[Any]]:
        with self.owner.connect() as db:
            return {
                name: list(
                    db.execute(
                        text(f"SELECT to_jsonb(t) FROM agent.{name} t ORDER BY to_jsonb(t)::text")
                    ).scalars()
                )
                for name in (
                    "agent_run",
                    "agent_attempt",
                    "execution_binding",
                    "checkpoint",
                    "workflow_command",
                    "canonical_event",
                    "idempotency_key",
                )
            }

    def audits(self) -> list[dict[str, Any]]:
        with self.owner.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    text(
                        "SELECT * FROM audit.audit_event "
                        "WHERE action LIKE 'agent.%' ORDER BY occurred_at, id"
                    )
                ).mappings()
            ]


@pytest.fixture
def e2e(
    isolated_agent_database: IsolatedAgentDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[AgentE2E]:
    owner = isolated_agent_database.owner
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name in ("pepper", "totp_key", "idempotency_key"):
        (secret_dir / name).write_bytes(uuid4().bytes + uuid4().bytes)
    monkeypatch.setenv("SECRET_MATERIAL_PATH", str(secret_dir))
    _clear_runtime_caches()
    with ExitStack() as stack:
        engines = {
            name: stack.enter_context(
                _runtime_engine(owner, owner.url, privilege_role=f"{name}_rw")
            )
            for name in (
                "audit",
                "identity",
                "organization",
                "workspace",
                "authorization",
                "requirement",
                "source_control",
            )
        }
        engines["agent"] = isolated_agent_database.runtime
        for name, engine in engines.items():
            factory = {
                "audit": "runtime_engine",
                "source_control": "source_control_query_runtime_engine",
            }.get(name, f"{name}_runtime_engine")
            monkeypatch.setattr(bootstrap, factory, lambda engine=engine: engine)
        try:
            stdout, stderr = StringIO(), StringIO()
            assert (
                bootstrap_admin.main(
                    ["--employee-no", "00000001", "--display-name", "Agent E2E Administrator"],
                    engine=engines["identity"],
                    dependencies=bootstrap.identity_dependencies(),
                    security_changes=bootstrap.security_change_orchestrator(),
                    authorization_engine=engines["authorization"],
                    authorization_dependencies=bootstrap.authorization_dependencies(),
                    stdout=stdout,
                    stderr=stderr,
                )
                == 0
            )
            app = bootstrap.create_app()
            admin = stack.enter_context(
                TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
            )
            _initialize(
                admin,
                employee_no="00000001",
                temporary_password=stdout.getvalue().strip(),
                password="AgentAdmin!Recovery2026",
                key="agent-admin-init",
                request_ids={
                    step: f"req-agentadmin-{step}"
                    for step in ("login", "password", "enroll", "confirm")
                },
            )
            created = admin.post(
                "/api/v1/admin/accounts",
                json={
                    "employeeNo": "00000002",
                    "displayName": "Agent E2E Member",
                    "profession": "BACKEND",
                    "reason": "Recovery acceptance",
                },
                headers=headers("agent-member-create"),
            )
            assert created.status_code == 201, created.text
            member = stack.enter_context(
                TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
            )
            _initialize(
                member,
                employee_no="00000002",
                temporary_password=created.json()["temporaryPassword"],
                password="AgentMember!Recovery2026",
                key="agent-member-init",
                request_ids={
                    step: f"req-agentmember-{step}"
                    for step in ("login", "password", "enroll", "confirm")
                },
            )
            token = member.cookies.get("ep_session")
            assert token
            with engines["identity"].begin() as db:
                principal = validate_session(
                    db,
                    raw_token=token,
                    dependencies=bootstrap.identity_dependencies(),
                    touch_activity=False,
                )
            assert principal is not None and principal.session_kind is SessionKind.FULL
            harness = AgentE2E(
                owner, engines, admin, member, created.json()["account"]["id"], "", ""
            )
            with owner.begin() as db:
                db.execute(
                    text(
                        "INSERT INTO workspace.workspace (id, name, owner_id, version) "
                        "VALUES (:id, 'Agent E2E', :owner, 1)"
                    ),
                    {"id": WORKSPACE_ID, "owner": harness.member_id},
                )
                db.execute(
                    text(
                        "INSERT INTO workspace.members_projection "
                        "(workspace_id, account_id, source, computed_at) "
                        "VALUES (:workspace, :member, 'OWNER', now())"
                    ),
                    {"workspace": WORKSPACE_ID, "member": harness.member_id},
                )
            for capability in (
                "requirement.create",
                "code.change",
                "agent.definition.read",
                "agent.run.execute",
                "agent.run.read",
            ):
                harness.grant(capability)
            # Register platform metadata only; no provider or repository worker is invoked.
            assert (
                source_control_repository.main(
                    [
                        "register",
                        "--repository-id",
                        REPOSITORY_ID,
                        "--workspace-id",
                        WORKSPACE_ID,
                        "--project-id",
                        "801",
                        "--project-path",
                        "platform/agent-e2e",
                        "--connection-ref",
                        "dev-unresolved",
                        "--credential-secret-ref",
                        "secret-ref:unresolved",
                        "--webhook-signing-secret-ref",
                        "secret-ref:unresolved-webhook",
                    ],
                    engine=engines["source_control"],
                    dependencies=bootstrap.source_control_dependencies(),
                    stdout=StringIO(),
                    stderr=StringIO(),
                )
                == 0
            )
            requirement = member.post(
                "/api/v1/requirements",
                json={
                    "workspaceId": WORKSPACE_ID,
                    "type": "feat",
                    "title": "Agent recovery",
                    "description": "No repository execution",
                    "acceptanceCriteria": ["Recovery evidence"],
                    "initialRepositoryId": REPOSITORY_ID,
                },
                headers=headers("agent-requirement-create"),
            )
            assert requirement.status_code == 201, requirement.text
            assert requirement.json()["workItem"]["assignmentState"] == "ASSIGNED"
            harness.requirement_id = requirement.json()["requirement"]["id"]
            harness.work_item_id = requirement.json()["workItem"]["id"]
            yield harness
        finally:
            _clear_runtime_caches()


def test_http_postgres_attempt_interrupt_authorize_resume_and_finish(e2e: AgentE2E) -> None:
    control = e2e.grant("agent.run.control")
    assert e2e.member.get("/api/v1/agent-definitions").status_code == 200
    started = e2e.start()
    assert started.json()["run"]["createdBy"] == "00000002"
    source = started.json()["run"]["businessContext"]
    assert source["requirementId"] == e2e.requirement_id
    assert source["workItemId"] == e2e.work_item_id
    assert started.json()["run"]["goalRef"] == (
        f"requirement:{e2e.requirement_id}:work-item:{e2e.work_item_id}"
    )
    waiting = e2e.waiting(started)
    revision = waiting.json()["attempts"][0]["revision"]
    before = e2e.facts()
    session = e2e.member.cookies.get("ep_session")
    revoked = e2e.admin.request(
        "DELETE",
        f"/api/v1/admin/grants/{control.json()['id']}",
        json={"reason": "Recheck recovery authorization"},
        headers=headers("agent-control-revoke", control.headers["etag"]),
    )
    assert revoked.status_code == 200, revoked.text
    assert e2e.resume(started, revision).status_code == 403
    assert e2e.facts() == before
    e2e.grant("agent.run.control")
    resumed = e2e.resume(started, revision)
    assert resumed.status_code == 202, resumed.text
    assert e2e.member.cookies.get("ep_session") == session
    assert resumed.json()["attempt"]["runnerGeneration"] == 2
    assert resumed.json()["attempt"]["id"] == started.json()["attempt"]["id"]
    assert resumed.json()["attempt"]["bindingId"] == started.json()["binding"]["id"]
    assert e2e.facts()["execution_binding"] == before["execution_binding"]
    after_resume = e2e.facts()
    attempt_id = started.json()["attempt"]["id"]
    stale = e2e.event(attempt_id, "ATTEMPT_FINALIZING", 5)
    with pytest.raises(StaleRunnerGeneration):
        e2e.accept(stale)
    assert e2e.facts() == after_resume
    dispatch_workflow_commands(None, limit=10, dependencies=bootstrap.agent_dependencies())
    for sequence, kind in (
        (1, "ATTEMPT_PROVISIONING"),
        (2, "ATTEMPT_RUNNING"),
        (3, "ATTEMPT_FINALIZING"),
        (4, "ATTEMPT_SUCCEEDED"),
    ):
        e2e.accept(e2e.event(attempt_id, kind, sequence, generation=2))
    finished = e2e.member.get(f"/api/v1/agent-runs/{started.json()['run']['id']}")
    assert finished.status_code == 200
    assert finished.json()["run"]["state"] == "SUCCEEDED"
    assert finished.json()["run"]["businessContext"] == source
    assert finished.json()["attempts"][0]["state"] == "SUCCEEDED"
    final_facts = e2e.facts()
    assert [row["state"] for row in final_facts["workflow_command"]] == ["DISPATCHED", "DISPATCHED"]
    for command in final_facts["workflow_command"]:
        assert command["receipt"] == {"commandKey": command["command_key"], "outcome": "ACCEPTED"}
    events = e2e.member.get(f"/api/v1/agent-runs/{started.json()['run']['id']}/events")
    assert events.status_code == 200, events.text
    assert len(events.json()["items"]) == 8
    assert all("data" not in item for item in events.json()["items"])
    assert events.json()["items"][-1]["traceId"] == "trace-e2e"
    actions = [row["action"] for row in e2e.audits()]
    assert [action for action in actions if action != "agent.event.accept"] == [
        "agent.run.start",
        "agent.attempt.waiting_input",
        "agent.attempt.resume",
        "agent.event.reject_stale_generation",
        "agent.attempt.succeeded",
    ]
    assert actions.count("agent.event.accept") == 5
    worker_audits = [
        row
        for row in e2e.audits()
        if row["action"]
        in (
            "agent.attempt.waiting_input",
            "agent.event.reject_stale_generation",
            "agent.attempt.succeeded",
        )
    ]
    assert {row["correlation_id"] for row in worker_audits} == {
        "agent-correlation:" + hashlib.sha256(CORRELATION.encode()).hexdigest()
    }
    rejection = next(
        row for row in worker_audits if row["action"] == "agent.event.reject_stale_generation"
    )
    assert rejection["result"] == "REJECTED"
    assert "STALE_RUNNER_GENERATION" in rejection["reason"]
    assert stale.id not in rejection["reason"]
    with e2e.owner.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM source_control.source_control_effect")
            ).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text("SELECT count(*) FROM source_control.repository_branch_binding")
            ).scalar_one()
            == 0
        )


def test_exact_grant_does_not_bypass_lost_membership(e2e: AgentE2E) -> None:
    e2e.grant("agent.run.control")
    started = e2e.start()
    waiting = e2e.waiting(started)
    before = e2e.facts()
    with e2e.owner.begin() as db:
        db.execute(
            text(
                "DELETE FROM workspace.members_projection "
                "WHERE workspace_id=:workspace AND account_id=:member"
            ),
            {"workspace": WORKSPACE_ID, "member": e2e.member_id},
        )
    assert e2e.resume(started, waiting.json()["attempts"][0]["revision"]).status_code == 403
    assert e2e.facts() == before
    with e2e.owner.begin() as db:
        db.execute(
            text(
                "INSERT INTO workspace.members_projection "
                "(workspace_id, account_id, source, computed_at) "
                "VALUES (:workspace, :member, 'OWNER', now())"
            ),
            {"workspace": WORKSPACE_ID, "member": e2e.member_id},
        )
    assert e2e.resume(started, waiting.json()["attempts"][0]["revision"]).status_code == 202


def test_principal_version_changes_after_resolution_fail_closed(
    e2e: AgentE2E, monkeypatch: pytest.MonkeyPatch
) -> None:
    e2e.grant("agent.run.control")
    started = e2e.start()
    waiting = e2e.waiting(started)
    before = e2e.facts()
    original = bootstrap.authorization_capability_guard

    def change_version_then_guard(
        principal: Any, capability: str, workspace_id: str | None
    ) -> None:
        with e2e.owner.begin() as db:
            db.execute(
                text(
                    'UPDATE "authorization".principal_version '
                    "SET version=version+1 WHERE account_id=:id"
                ),
                {"id": e2e.member_id},
            )
        original(principal, capability, workspace_id)

    monkeypatch.setattr(bootstrap, "authorization_capability_guard", change_version_then_guard)
    with TestClient(
        bootstrap.create_app(), base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        client.cookies.update(e2e.member.cookies)
        response = client.post(
            f"/api/v1/agent-runs/{started.json()['run']['id']}/attempts/{started.json()['attempt']['id']}/resume",
            json={},
            headers=headers(
                "agent-version-race", f'"v{waiting.json()["attempts"][0]["revision"]}"'
            ),
        )
    assert response.status_code == 503, response.text
    assert e2e.facts() == before


@pytest.mark.parametrize("rejection", [False, True])
def test_event_audit_failure_rolls_back_and_never_disguises_rejection(
    e2e: AgentE2E, monkeypatch: pytest.MonkeyPatch, rejection: bool
) -> None:
    e2e.grant("agent.run.control")
    started = e2e.start()
    waiting = e2e.waiting(started)
    assert e2e.resume(started, waiting.json()["attempts"][0]["revision"]).status_code == 202
    attempt_id = started.json()["attempt"]["id"]
    incoming = e2e.event(attempt_id, "ATTEMPT_PROVISIONING", 1, generation=1 if rejection else 2)
    before, audits_before = e2e.facts(), e2e.audits()
    append = SqlAlchemyAgentUnitOfWork.append_audit_event

    def append_then_fail(uow: SqlAlchemyAgentUnitOfWork, audit: Any) -> None:
        append(uow, audit)
        raise RuntimeError("injected audit failure after append")

    monkeypatch.setattr(SqlAlchemyAgentUnitOfWork, "append_audit_event", append_then_fail)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        e2e.accept(incoming)
    assert e2e.facts() == before
    assert e2e.audits() == audits_before
