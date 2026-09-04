# V0.8 Agent Control Plane Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the V0.8 backend Agent Control Plane slice for definitions, runs/attempts, immutable bindings, durable workflow commands, canonical events/traces, checkpoints, and authorized start/observe/cancel/resume actions.

**Architecture:** A new `agent` deep module owns every durable Agent fact behind its package-root Facade. Requirement context, binding policy, and workflow orchestration are injected Ports; current production composition uses the V0.4 Requirement Facade plus explicitly labelled no-side-effect DEV binding and Temporal adapters, so no unmerged V0.7 type or V0.9 Sandbox implementation enters the module.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, PostgreSQL 18, Alembic, Vitest-compatible OpenAPI Artifact consumers, pytest, Ruff, mypy, import-linter

**Spec:** `docs/superpowers/specs/2026-08-31-backend-v08-agent-control-plane-design.md`

## Global Constraints

- Work only in `codex/backend-v08-agent-control-plane` based on `9b71b82602bb4231f6ccc2622c0f49a91287a366`; do not read or merge V0.5/V0.6/V0.7 worktrees.
- Do not modify `docs/superpowers/progress/current.md` without the exact user trigger `【同步进度】`.
- No repository write, push, MR, Sandbox/Kata, arbitrary tool/network, Provider credential, Pydantic AI, or Temporal SDK behaviour belongs to V0.8.
- Every production behaviour is introduced by a failing test observed before implementation.
- Other modules are used only through package-root Facades. Agent domain/API/database/Audit contain only platform DTOs and identifiers.
- Binding, canonical event, and checkpoint rows are append-only/immutable; the runtime role receives no UPDATE/DELETE privilege on them.
- Every user request re-resolves the current Principal and rechecks its exact Platform/Workspace capability before the Agent command.
- Protected writes require transactional Audit. Audit never includes goal text, prompt/source body, event data, Secret, token, provider payload, or SDK object.
- HTTP writes require `Idempotency-Key`; cancel/resume additionally require `If-Match` and return the current ETag.
- The DEV binding source is persisted as `DEV_FAKE`; passing it is not Temporal, OpenBao, Model Gateway, or Sandbox integration evidence.
- Final gates are copied from `.github/workflows/ci.yml`: locked sync, Ruff format/check, mypy, import-linter, Alembic heads, full pytest, and OpenAPI `--check`.

---

### Task 1: Agent domain state machine and deep-module contract

**Files:**
- Create: `tests/agent/test_domain.py`
- Create: `control_plane/app/modules/agent/domain/models.py`
- Create: `control_plane/app/modules/agent/domain/transitions.py`
- Create: `control_plane/app/modules/agent/domain/errors.py`
- Create: `control_plane/app/modules/agent/domain/__init__.py`
- Create: `control_plane/app/modules/agent/{application,ports,adapters,api}/__init__.py`
- Create: `control_plane/app/modules/agent/__init__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_contract_guard.py`

**Interfaces:**
- Produces: `AttemptState`, `RunState`, `WorkflowCommandKind`, `WorkflowCommandState`, `AgentDefinition`, `AgentRun`, `AgentAttempt`, `ExecutionBinding`, `CanonicalEventInput`, `CheckpointInput`, and `transition_attempt`.
- Invariant: `ExecutionBinding` is frozen and calculates its digest from canonical platform fields; permissions containing `repository.write`, `git.push`, `source_control.write`, or `merge` are rejected.

- [ ] **Step 1: Write failing domain tests**

```python
def test_waiting_input_requires_checkpoint_and_resume_keeps_binding() -> None:
    attempt = running_attempt(binding_id="binding-1", generation=1, revision=4)
    with pytest.raises(CheckpointRequired):
        transition_attempt(attempt, AttemptState.WAITING_INPUT, checkpoint=None, now=NOW)
    waiting = transition_attempt(
        attempt, AttemptState.WAITING_INPUT, checkpoint=CHECKPOINT, now=NOW
    )
    resumed = resume_generation(waiting, fencing_token="fence-2", now=NOW)
    assert resumed.binding_id == "binding-1"
    assert resumed.runner_generation == 2
    assert resumed.state is AttemptState.QUEUED


def test_binding_rejects_repository_write_permissions() -> None:
    with pytest.raises(RepositoryWriteForbidden):
        execution_binding(runtime_permissions=("context.read", "git.push"))
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/agent/test_domain.py -v --basetemp .pytest-tmp-v08/domain-red`

Expected: collection fails because `control_plane.app.modules.agent` does not exist.

- [ ] **Step 3: Implement the frozen models and explicit transition table**

```python
ALLOWED_TRANSITIONS = {
    AttemptState.CREATED: {AttemptState.BINDING, AttemptState.CANCELING},
    AttemptState.BINDING: {AttemptState.QUEUED, AttemptState.FAILED, AttemptState.CANCELING},
    AttemptState.QUEUED: {AttemptState.PROVISIONING, AttemptState.CANCELING},
    AttemptState.PROVISIONING: {AttemptState.RUNNING, AttemptState.FAILED, AttemptState.CANCELING},
    AttemptState.RUNNING: {
        AttemptState.WAITING_INPUT,
        AttemptState.FINALIZING,
        AttemptState.CANCELING,
    },
    AttemptState.WAITING_INPUT: {AttemptState.QUEUED, AttemptState.CANCELING},
    AttemptState.FINALIZING: {AttemptState.SUCCEEDED, AttemptState.FAILED},
    AttemptState.CANCELING: {AttemptState.CANCELED, AttemptState.TIMED_OUT},
}
```

Add `agent` to the layers container, every module-to-module forbidden list, domain-vs-shared API list, and the configuration Facade symmetry lists in `pyproject.toml`.

- [ ] **Step 4: Run GREEN and architecture guards**

Run: `uv run pytest tests/agent/test_domain.py tests/test_contract_guard.py -v --basetemp .pytest-tmp-v08/domain-green`

Expected: all tests pass and the contract guard reports exact module symmetry.

- [ ] **Step 5: Commit**

```powershell
git add control_plane/app/modules/agent tests/agent/test_domain.py pyproject.toml
git commit -m "feat(agent): establish control plane domain model"
```

---

### Task 2: Independent Agent migration and SQL repository

**Files:**
- Create: `migrations/agent/0001_agent_control_plane.py`
- Create: `tests/agent/conftest.py`
- Create: `tests/agent/test_migration.py`
- Create: `tests/agent/test_repository.py`
- Create: `control_plane/app/modules/agent/ports/repository.py`
- Create: `control_plane/app/modules/agent/adapters/sqlalchemy.py`
- Modify: `alembic.ini`
- Modify: `control_plane/app/shared/db/settings.py`

**Interfaces:**
- Produces: independent `agent@head`, least-privilege `agent_rw`, `AgentRepositoryFactory`, and `SqlAlchemyAgentRepository`.
- Tables: `agent_definition`, `agent_run`, `agent_attempt`, `execution_binding`, `canonical_event`, `checkpoint`, `workflow_command`, and `idempotency_key`.
- Seed: the migration inserts the immutable, explicitly labelled `agent/dev-control-plane-probe` version `1` Definition used by the restricted DEV slice; later Definition versions are created by the Task 3 application command.

- [ ] **Step 1: Write failing migration and repository tests**

```python
def test_agent_runtime_cannot_mutate_binding_or_event(agent_owner_engine: Engine) -> None:
    with agent_owner_engine.connect() as db:
        assert (
            db.execute(
                text("SELECT has_table_privilege('agent_rw','agent.execution_binding','UPDATE')")
            ).scalar_one()
            is False
        )
        assert (
            db.execute(
                text("SELECT has_table_privilege('agent_rw','agent.canonical_event','DELETE')")
            ).scalar_one()
            is False
        )


def test_duplicate_event_sequence_cannot_change_payload(repository: AgentRepository) -> None:
    repository.append_event(EVENT)
    with pytest.raises(EventReplayConflict):
        repository.append_event(EVENT.model_copy(update={"summary": "changed"}))
```

- [ ] **Step 2: Run RED against the missing migration**

Run: `uv run pytest tests/agent/test_migration.py tests/agent/test_repository.py -v --basetemp .pytest-tmp-v08/sql-red`

Expected: Alembic cannot resolve `agent@head` and the SQL adapter is missing.

- [ ] **Step 3: Implement schema constraints and repository mapping**

Use UUID identifiers, UTC `TIMESTAMPTZ`, exact CHECK constraints for states/digests/classification, `UNIQUE(attempt_id, runner_generation, sequence)`, `UNIQUE(event_id)`, and no cross-schema foreign keys. Grant `agent_rw` only the exact SELECT/INSERT/column-UPDATE privileges required, plus `EXECUTE` on `audit.append_event`; deny table DELETE and binding/event/checkpoint UPDATE.

Add `agent_database_url` to `DbSettings` and `migrations/agent` to Alembic `version_locations`.

- [ ] **Step 4: Run GREEN plus migration round trip**

Run: `uv run pytest tests/agent/test_migration.py tests/agent/test_repository.py -v --basetemp .pytest-tmp-v08/sql-green`

Expected: fresh database upgrades all heads, Agent constraints and runtime privileges hold, downgrade refuses to discard business rows, and repository round trips preserve frozen DTOs.

- [ ] **Step 5: Commit**

```powershell
git add migrations/agent alembic.ini control_plane/app/shared/db/settings.py control_plane/app/modules/agent/ports control_plane/app/modules/agent/adapters/sqlalchemy.py tests/agent
git commit -m "feat(agent): persist immutable control plane facts"
```

---

### Task 3: Definitions, Requirement context, binding, start, and idempotency

**Files:**
- Create: `control_plane/app/modules/agent/ports/runtime.py`
- Create: `control_plane/app/modules/agent/application/dependencies.py`
- Create: `control_plane/app/modules/agent/application/definitions.py`
- Create: `control_plane/app/modules/agent/application/runs.py`
- Create: `control_plane/app/modules/agent/adapters/requirement.py`
- Create: `control_plane/app/modules/agent/adapters/dev_policy.py`
- Create: `tests/agent/test_start_run.py`
- Modify: `control_plane/app/modules/agent/__init__.py`

**Interfaces:**
- Consumes: Requirement package-root `get_requirement`, transactional Audit, shared sealed idempotency engine, and `SecretManagerPort`.
- Produces: `RequirementExecutionContextPort.resolve`, `ExecutionBindingPolicyPort.resolve`, `register_definition`, `list_definitions`, and `start_run`.

- [ ] **Step 1: Write failing start tests**

```python
def test_start_persists_one_run_attempt_binding_command_and_audit(runtime: AgentRuntime) -> None:
    result = start_run(runtime.db, command=START, dependencies=runtime.dependencies)
    assert result.attempt.state is AttemptState.QUEUED
    assert result.binding.source == "DEV_FAKE"
    assert result.binding.runtime_permissions == (
        "checkpoint.write",
        "context.read",
        "event.emit",
    )
    assert runtime.rows("agent.workflow_command") == 1
    assert runtime.audit_actions() == ["agent.run.start"]


def test_same_key_replays_and_changed_body_conflicts(runtime: AgentRuntime) -> None:
    first = start_run(runtime.db, command=START, dependencies=runtime.dependencies)
    replay = start_run(runtime.db, command=START, dependencies=runtime.dependencies)
    assert replay == first
    with pytest.raises(IdempotencyConflict):
        start_run(
            runtime.db,
            command=START.model_copy(update={"goal": "changed"}),
            dependencies=runtime.dependencies,
        )
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/agent/test_start_run.py -v --basetemp .pytest-tmp-v08/start-red`

Expected: missing application functions and Ports.

- [ ] **Step 3: Implement the minimal start use case**

The Requirement adapter must find the exact WorkItem and current non-superseded assignment from `RequirementDetailsDto`, verify workspace ownership, and return platform references. `DevExecutionBindingPolicy` returns a deterministic `DEV_FAKE` snapshot with no repository-write permission and no Provider credential. Start writes Run, Attempt, Binding, initial canonical events, START command, sealed idempotency result, and one safe Audit envelope in one transaction.

- [ ] **Step 4: Run GREEN and mutation cases**

Run: `uv run pytest tests/agent/test_start_run.py tests/agent/test_domain.py -v --basetemp .pytest-tmp-v08/start-green`

Expected: context mismatch, missing assignment, inactive Definition, binding-policy failure, Audit failure, and changed-key replay all leave no partial dispatchable command.

- [ ] **Step 5: Commit**

```powershell
git add control_plane/app/modules/agent migrations/agent/0001_agent_control_plane.py tests/agent/test_start_run.py
git commit -m "feat(agent): bind and start governed attempts"
```

---

### Task 4: Canonical event/trace, checkpoint, cancel, and resume

**Files:**
- Create: `control_plane/app/modules/agent/application/events.py`
- Create: `control_plane/app/modules/agent/application/control.py`
- Create: `control_plane/app/modules/agent/application/queries.py`
- Create: `tests/agent/test_events.py`
- Create: `tests/agent/test_control.py`
- Modify: `control_plane/app/modules/agent/__init__.py`

**Interfaces:**
- Produces: `accept_workflow_event`, `get_run`, `list_events`, `cancel_attempt`, and `resume_attempt`.
- Event contract: exact replay is idempotent; sequence gap, altered replay, illegal transition, or stale generation cannot mutate Attempt state.

- [ ] **Step 1: Write failing event and recovery tests**

```python
def test_waiting_event_atomically_persists_checkpoint(runtime: AgentRuntime) -> None:
    accepted = accept_workflow_event(
        runtime.db, event=WAITING_EVENT, dependencies=runtime.dependencies
    )
    assert accepted.attempt.state is AttemptState.WAITING_INPUT
    assert accepted.checkpoint is not None
    assert accepted.checkpoint.content_hash == "sha256:" + "a" * 64


def test_resume_rotates_generation_and_rejects_late_event(runtime: AgentRuntime) -> None:
    resumed = resume_attempt(runtime.db, command=RESUME, dependencies=runtime.dependencies)
    assert resumed.attempt.runner_generation == 2
    assert resumed.attempt.binding_id == WAITING_ATTEMPT.binding_id
    with pytest.raises(StaleRunnerGeneration):
        accept_workflow_event(
            runtime.db, event=OLD_GENERATION_EVENT, dependencies=runtime.dependencies
        )
    assert runtime.current_attempt().state is AttemptState.QUEUED
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/agent/test_events.py tests/agent/test_control.py -v --basetemp .pytest-tmp-v08/control-red`

Expected: missing event/control application modules.

- [ ] **Step 3: Implement row-locked event/control use cases**

Map platform event types to the explicit transition table. Hash the canonical event body before deduplication. Persist event, checkpoint, Attempt transition, workflow command, idempotent result, and Audit under one transaction. Resume reuses the exact binding digest and checkpoint reference, rotates fencing, increments generation, and starts that generation at sequence 1. Cancel is idempotent and terminal-safe.

- [ ] **Step 4: Run GREEN with concurrency counterexamples**

Run: `uv run pytest tests/agent/test_events.py tests/agent/test_control.py -v --basetemp .pytest-tmp-v08/control-green`

Expected: competing cancel/resume with the same expected revision yields one success and one stale-revision failure; old generation, sequence gap, altered replay, expired wait, missing checkpoint, and terminal resume are rejected.

- [ ] **Step 5: Commit**

```powershell
git add control_plane/app/modules/agent tests/agent/test_events.py tests/agent/test_control.py
git commit -m "feat(agent): govern events checkpoints and recovery"
```

---

### Task 5: Durable Temporal seam and restricted DEV adapter

**Files:**
- Create: `control_plane/app/modules/agent/application/workflow.py`
- Create: `control_plane/app/modules/agent/adapters/dev_temporal.py`
- Create: `tests/agent/test_workflow_dispatch.py`
- Modify: `control_plane/app/modules/agent/application/dependencies.py`
- Modify: `control_plane/app/modules/agent/__init__.py`

**Interfaces:**
- Produces: `WorkflowOrchestratorPort.start/cancel/resume/lookup`, `DevTemporalAdapter`, `dispatch_workflow_commands`, and `reconcile_workflow_commands`.
- Invariant: the adapter receives and returns only platform DTOs; dispatch uncertainty remains `UNKNOWN` and is reconciled by the original command key.

- [ ] **Step 1: Write failing workflow-effect tests**

```python
def test_unknown_ack_is_not_reissued_and_reconciles_by_same_key(runtime: AgentRuntime) -> None:
    runtime.workflow.fail_after_accept = True
    first = dispatch_workflow_commands(runtime.db, limit=10, dependencies=runtime.dependencies)
    assert first.unknown == 1
    assert runtime.workflow.command_keys == [START_COMMAND.command_key]
    runtime.workflow.fail_after_accept = False
    reconciled = reconcile_workflow_commands(
        runtime.db, limit=10, dependencies=runtime.dependencies
    )
    assert reconciled.confirmed == 1
    assert runtime.workflow.command_keys == [START_COMMAND.command_key]
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/agent/test_workflow_dispatch.py -v --basetemp .pytest-tmp-v08/workflow-red`

Expected: workflow dispatcher and adapter are missing.

- [ ] **Step 3: Implement bounded claim/dispatch/reconcile**

Claim commands with `FOR UPDATE SKIP LOCKED`, increment bounded attempts, and persist only platform orchestration receipts. `DevTemporalAdapter` keeps a command-key map in process, performs no network/process/model/repository action, and supports deterministic accepted, rejected, and acknowledgement-unknown test modes.

- [ ] **Step 4: Run GREEN**

Run: `uv run pytest tests/agent/test_workflow_dispatch.py tests/agent/test_control.py -v --basetemp .pytest-tmp-v08/workflow-green`

Expected: duplicate dispatch, unknown acknowledgement, deterministic rejection, and reconciliation all converge without duplicate effects or third-party payload persistence.

- [ ] **Step 5: Commit**

```powershell
git add control_plane/app/modules/agent tests/agent/test_workflow_dispatch.py
git commit -m "feat(agent): add durable temporal workflow seam"
```

---

### Task 6: Authorized HTTP API and OpenAPI contract

**Files:**
- Create: `control_plane/app/modules/agent/api/dto.py`
- Create: `control_plane/app/modules/agent/api/runtime.py`
- Create: `control_plane/app/modules/agent/api/routes.py`
- Create: `tests/agent/test_api.py`
- Modify: `control_plane/app/bootstrap/app.py`
- Modify: `openapi.json`

**Interfaces:**
- Produces operation IDs: `agent_definitions_list`, `agent_runs_start`, `agent_runs_get`, `agent_run_events_list`, `agent_attempt_cancel`, and `agent_attempt_resume`.
- Capabilities: `agent.definition.read`, `agent.run.execute`, `agent.run.read`, and `agent.run.control`.

- [ ] **Step 1: Write failing API contract tests**

```python
def test_every_user_action_rechecks_current_capability(agent_client: AgentClient) -> None:
    started = agent_client.start()
    agent_client.permissions.revoke("agent.run.read")
    denied = agent_client.http.get(f"/api/v1/agent-runs/{started['run']['id']}")
    assert denied.status_code == 403
    agent_client.permissions.grant("agent.run.read")
    assert agent_client.http.get(f"/api/v1/agent-runs/{started['run']['id']}").status_code == 200


def test_control_requires_idempotency_origin_and_if_match(agent_client: AgentClient) -> None:
    response = agent_client.http.post(agent_client.cancel_path)
    assert response.status_code == 422
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/agent/test_api.py -v --basetemp .pytest-tmp-v08/api-red`

Expected: Agent router and DTOs are missing.

- [ ] **Step 3: Implement CamelModel DTOs and guarded routes**

Resolve immutable workspace scope from the request body or stored Run, then call the injected current capability guard on every route. Never cache a prior authorization decision. Convert domain errors to 404/409/422/503 Problem Details, expose ETag from Attempt revision, and use opaque event cursors.

- [ ] **Step 4: Run GREEN and export OpenAPI**

Run: `uv run pytest tests/agent/test_api.py tests/test_error_contract.py -v --basetemp .pytest-tmp-v08/api-green`

Run: `uv run python scripts/export_openapi.py`

Expected: all Agent paths use camelCase platform DTOs, declare problem responses/security, and no schema contains `Temporal`, `PydanticAI`, `WorkflowExecution`, or SDK payload types.

- [ ] **Step 5: Commit**

```powershell
git add control_plane/app/modules/agent control_plane/app/bootstrap/app.py tests/agent/test_api.py openapi.json
git commit -m "feat(api): expose v0.8 agent control contract"
```

---

### Task 7: Real PostgreSQL/HTTP recovery E2E, version, review, and full gates

**Files:**
- Create: `tests/agent/test_e2e.py`
- Create: `tests/agent/test_boundary_contract.py`
- Modify: `control_plane/app/__init__.py`
- Modify: `openapi.json`

**Interfaces:**
- Consumes: all Agent Facade, adapters, PostgreSQL migration, HTTP routes, current authorization guard, and transactional Audit.
- Produces: version `0.8.0` OpenAPI Artifact and complete local gate evidence; no tag or Release.

- [ ] **Step 1: Write the failing end-to-end recovery test**

```python
def test_http_postgres_attempt_interrupt_authorize_resume_and_finish(e2e: AgentE2E) -> None:
    started = e2e.start_run()
    e2e.worker_started_and_waiting(started, checkpoint=CHECKPOINT)
    assert e2e.get_run(started)["attempts"][0]["state"] == "WAITING_INPUT"
    e2e.permissions.revoke("agent.run.control")
    assert e2e.resume(started).status_code == 403
    e2e.permissions.grant("agent.run.control")
    resumed = e2e.resume(started)
    assert resumed.status_code == 202
    assert resumed.json()["attempt"]["runnerGeneration"] == 2
    assert e2e.late_generation_event(started).failure_code == "STALE_RUNNER_GENERATION"
    e2e.worker_finalized_and_succeeded(started, generation=2)
    assert e2e.get_run(started)["attempts"][0]["state"] == "SUCCEEDED"
    assert e2e.audit_actions(started) == [
        "agent.run.start",
        "agent.attempt.waiting_input",
        "agent.attempt.resume",
        "agent.event.reject_stale_generation",
        "agent.attempt.succeeded",
    ]
```

- [ ] **Step 2: Run RED and then implement only missing composition/evidence wiring**

Run: `uv run pytest tests/agent/test_e2e.py tests/agent/test_boundary_contract.py -v --basetemp .pytest-tmp-v08/e2e-red`

Expected: failure identifies missing final bootstrap or boundary mapping, not a pre-passing test.

- [ ] **Step 3: Set version and regenerate exact OpenAPI bytes**

Set `control_plane/app/__init__.py` to `__version__ = "0.8.0"`, run `uv run python scripts/export_openapi.py`, and assert the boundary test finds no third-party type or secret-bearing field in domain DTOs, database columns, API schemas, or Audit envelopes.

- [ ] **Step 4: Run focused GREEN and migration head**

Run: `uv run alembic upgrade heads`

Run: `uv run pytest tests/agent -v --basetemp .pytest-tmp-v08/agent-green`

Expected: Agent suite passes against the real local PostgreSQL where marked integration; no repository mutation is observed.

- [ ] **Step 5: Perform fresh full CI-equivalent verification**

```powershell
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run lint-imports
uv run alembic upgrade heads
uv run pytest -v --basetemp .pytest-tmp-v08/full
uv run python scripts/export_openapi.py --check
git diff --check
git status --short --branch
```

Expected: zero failures/errors, OpenAPI clean, and only intended V0.8 files changed.

- [ ] **Step 6: Review requirements and code**

Re-read the spec line by line; inspect `git diff 9b71b826..HEAD` and the uncommitted diff for state-machine gaps, cross-module imports, third-party leaks, missing Audit, authorization reuse, mutable evidence, idempotency/concurrency mistakes, and scope creep. Fix every finding through a new failing regression test.

- [ ] **Step 7: Commit and push the isolated branch**

```powershell
git add control_plane/app/__init__.py control_plane/app/modules/agent control_plane/app/bootstrap/app.py control_plane/app/shared/db/settings.py migrations/agent tests/agent alembic.ini pyproject.toml openapi.json docs/superpowers
git commit -m "feat(agent): complete v0.8 control plane slice"
git push -u origin codex/backend-v08-agent-control-plane
```

After push, verify local HEAD equals `git ls-remote --heads origin codex/backend-v08-agent-control-plane`. Do not merge `main`, create a tag, modify progress, or delete any branch/worktree.

---

## Self-Review

- Spec coverage maps to Tasks 1–7: domain/interface, persistence, binding/start, events/checkpoints/control, Temporal seam, API/authorization, PostgreSQL/HTTP E2E/OpenAPI/gates.
- Type names are consistent across tasks: `AttemptState`, `ExecutionBinding`, `CanonicalEventInput`, `WorkflowOrchestratorPort`, `AgentRepository`, `AgentRuntime`, and `AgentHttpRuntime` each have one owner.
- V0.7 and V0.9 stacking points are adapters, never copied Facades or SDK types.
- Every code task contains an observed RED, minimal implementation, GREEN, and single-topic commit.
- The plan authorizes only the user-requested branch push after green gates; merge, tag, deployment, Release Acceptance, progress sync, and other worktree cleanup remain excluded.
