# V0.8 Agent Control Plane Backend Design

- Date: 2026-08-31
- Status: approved by the delegated V0.8 implementation request
- Repository: `engineering-platform-backend`
- Baseline: `9b71b82602bb4231f6ccc2622c0f49a91287a366`
- Architecture owners: `engineering-platform-docs/architecture/{03,06,08,12,appendix-parameters}.md`
- Flow-first programme: `engineering-platform-docs/docs/superpowers/{specs/2026-08-28-flow-first-release-roadmap-design.md,plans/2026-08-28-flow-first-product-development.md}`

## Goal

Deliver an independently testable V0.8 backend slice that owns Agent Definition, Run/Attempt, immutable Execution Binding, canonical event/trace, checkpoint, workflow dispatch, and the user actions to start, observe, cancel, and resume an Attempt. The slice proves interruption and recovery for an Attempt that has no repository-write side effects. It does not claim that Temporal, OpenBao, Model Gateway, Pydantic AI, or Kata is deployed.

## Scope

Included:

- A new `agent` deep module with the repository-standard `api/application/domain/ports/adapters` layers and package-root Facade.
- Immutable, versioned Agent Definitions and one explicitly labelled DEV fixed Definition for the no-side-effect control-plane probe.
- Run and ordered Attempt facts, immutable Execution Binding, runner generation/fencing, canonical events with trace context, immutable checkpoint references, and workflow command effects.
- A stable `WorkflowOrchestratorPort` and restricted `DevTemporalAdapter`; no `temporalio` dependency and no SDK payload in platform facts.
- A stable binding policy/context seam. The Requirement adapter consumes only the current package-root Requirement Facade. The DEV binding adapter supplies explicit `DEV_FAKE` runtime/model/bundle snapshots until V0.7 public contracts are merged.
- PostgreSQL migration, least-privilege `agent_rw`, transactional Audit, HTTP API, idempotency, ETag/`If-Match`, current authorization checks, contract tests, real PostgreSQL/HTTP E2E, and OpenAPI version `0.8.0`.

Excluded:

- V0.9 Sandbox Controller, Kata materialization, repository mutation, credentials, push/MR delivery, arbitrary tools/network, Pydantic AI execution, or Provider calls.
- V0.10 Agent delivery, V0.11 specialist behaviour, V0.12 child execution/image build, frontend, deployment, backup, capacity, HA, merge to `main`, tag, or Release Acceptance.
- Any dependency on unmerged V0.5/V0.6/V0.7 worktrees or their private types.

## Approaches considered

1. **Agent-owned control plane facts behind stable Ports (selected).** The module owns all durable semantics. Requirement context, workflow orchestration, and future V0.7 configuration/model facts enter through adapters. This keeps the public Facade small, makes PostgreSQL and HTTP behaviour independently testable, and prevents Temporal or SDK types from becoming contracts.
2. **Add the Temporal Python SDK now.** Rejected because no formal Temporal deployment, Service Identity, or namespace evidence exists. A real SDK dependency would make local green tests look like infrastructure integration and would expose Temporal retry/payload choices too early.
3. **Put Run state in Requirement or bootstrap code.** Rejected by the deletion test: state-machine, event-sequence, checkpoint, and workflow-effect complexity would spread across Requirement routes, workers, and adapters. The Agent module has an independent responsibility and must own that complexity.

## Deep module interface

The package-root Facade is the caller and test surface:

```python
register_definition(db, command, dependencies) -> AgentDefinition
start_run(db, command, dependencies) -> StartRunResult
get_run(db, run_id, dependencies) -> AgentRunView
list_events(db, run_id, cursor, limit, dependencies) -> CanonicalEventPage
cancel_attempt(db, command, dependencies) -> AttemptControlResult
resume_attempt(db, command, dependencies) -> AttemptControlResult
accept_workflow_event(db, event, dependencies) -> EventAcceptance
dispatch_workflow_commands(db, limit, dependencies) -> DispatchBatch
```

The interface hides transition legality, generation fencing, binding hashing, event ordering, checkpoint validation, workflow command deduplication, idempotent response sealing, persistence, and Audit. API routes and workers use the same Facade; tests do not reach past it.

## Dependency seams

| Seam | Category | V0.8 adapter | Later adapter |
| --- | --- | --- | --- |
| `RequirementExecutionContextPort` | in-process module Facade | reads V0.4 Requirement public DTOs and current assignment | evolves only when a merged Requirement Facade adds an Agent-specific context contract |
| `ExecutionBindingPolicyPort` | local-substitutable | `DevExecutionBindingPolicy` returns bounded `DEV_FAKE` snapshots and forbids repository-write permissions | V0.7 Configuration/Model/Skill public Facades after merge |
| `WorkflowOrchestratorPort` | remote but owned | `DevTemporalAdapter` records platform orchestration receipts without SDK objects | Temporal SDK adapter in the Orchestrator deployable after identity/TLS/namespace integration |
| `AgentRepository` | local-substitutable | SQLAlchemy/PostgreSQL adapter | no alternative is required at the external Facade; tests use real PostgreSQL for persistence contracts |

The V0.8 adapters never import another module's internals. Third-party identifiers, payload classes, retries, histories, and SDK state remain adapter-private.

## Domain model and persistence

The `agent` schema owns:

- `agent_definition`: immutable `(id, version)` definitions with platform schemas and fixed capability/skill/permission declarations.
- `agent_run`: workspace-scoped business goal, creator, selected definition, latest Attempt, state, and revision.
- `agent_attempt`: ordered attempts, current state, immutable binding reference, current runner generation/fencing token, event sequence, waiting deadline, terminal evidence, and revision.
- `execution_binding`: one immutable row per Attempt containing the platform snapshot and its canonical SHA-256 digest. The runtime role has no UPDATE/DELETE privilege.
- `canonical_event`: append-only event/trace rows unique by event ID and `(attempt, generation, sequence)`.
- `checkpoint`: append-only, protected Artifact references with schema version, adapter version, content hash, and classification. SDK state is never stored inline.
- `workflow_command`: durable START/CANCEL/RESUME effects with platform command keys and bounded dispatch state.
- `idempotency_key`: module-local sealed HTTP response claims compatible with the shared idempotency engine.

No Agent table has a cross-schema foreign key. Requirement, WorkItem, Assignment, repository, model, policy, Artifact, and runtime identifiers are immutable references verified through Ports before binding.

## Attempt state and recovery

The stored state set follows architecture 03:

```text
CREATED -> BINDING -> QUEUED -> PROVISIONING -> RUNNING
RUNNING -> WAITING_INPUT -> QUEUED
RUNNING -> FINALIZING -> SUCCEEDED | FAILED
active -> CANCELING -> CANCELED
active -> CANCELING -> TIMED_OUT
```

V0.8's DEV adapter may exercise `PROVISIONING` but performs no Sandbox or repository action. The V0.9 adapter will replace that physical step without changing these facts.

Entering `WAITING_INPUT` requires one canonical event and one immutable checkpoint reference in the same transaction. Resume:

- rechecks current user authorization at the HTTP edge;
- locks the Attempt and checks `If-Match`, state, deadline, checkpoint, and original binding digest;
- keeps the same Attempt and Binding;
- increments runner generation, rotates the fencing token, resets the per-generation event sequence, queues one RESUME command, and records Audit;
- rejects every late event from an older generation with `STALE_RUNNER_GENERATION` and no state mutation.

Terminal states cannot resume. A later business retry is a new ordered Attempt and new Binding; it is not part of the V0.8 HTTP surface.

## Canonical events, traces, and checkpoints

Inbound worker data is first converted to a platform `CanonicalEventInput`. It carries platform event type, Attempt ID, generation, sequence, correlation/causation IDs, trace/span IDs, bounded summary, and a platform-owned data object. The domain rejects unknown event types, sequence gaps, duplicate IDs with different digests, stale generations, and event-driven illegal transitions.

Exact replay of the same event is idempotent. A conflicting replay fails closed. Prompt/source text, credentials, SDK objects, Temporal histories, and arbitrary exception payloads are not accepted. Audit stores only action/result/reason/references, never event body or goal text.

## Workflow effect contract

User commands commit Agent facts, Audit, and one `workflow_command` in a single PostgreSQL transaction. Dispatch is a separate retryable step:

1. claim a PLANNED command;
2. call `WorkflowOrchestratorPort` with a platform DTO and stable command key;
3. record `DISPATCHED` on a durable receipt, `UNKNOWN` on acknowledgement ambiguity, or `FAILED` only for a proven deterministic rejection;
4. reconcile `UNKNOWN` by the same command key before issuing any new effect.

The restricted DEV adapter returns a platform receipt and never starts a thread, process, model, repository tool, or network request.

## HTTP interface and authorization

All JSON uses `CamelModel`, errors use Problem Details, IDs are strings, and writes require `Idempotency-Key`, same-origin checks, and `If-Match` when controlling an existing Attempt.

| Method and path | Capability | Result |
| --- | --- | --- |
| `GET /api/v1/agent-definitions` | `agent.definition.read` platform | active definitions |
| `POST /api/v1/agent-runs` | `agent.run.execute` workspace | `202`, Run/Attempt/Binding receipt |
| `GET /api/v1/agent-runs/{runId}` | `agent.run.read` workspace | current Run, Attempts, binding/checkpoint summaries |
| `GET /api/v1/agent-runs/{runId}/events` | `agent.run.read` workspace | cursor page of canonical events |
| `POST /api/v1/agent-runs/{runId}/attempts/{attemptId}/cancel` | `agent.run.control` workspace | `202`, idempotent cancel request |
| `POST /api/v1/agent-runs/{runId}/attempts/{attemptId}/resume` | `agent.run.control` workspace | `202`, same-binding next generation |

Every request resolves the current session and calls the Authorization Facade-backed guard after resolving the immutable workspace scope. A Principal version change, revoked Grant, lost Membership, or unavailable authorization denies/fails closed before the Agent command. UI visibility and an earlier successful action are never reused as authorization evidence.

## Failure and concurrency rules

- Same Idempotency-Key + same fingerprint replays the exact sealed response; different fingerprint returns conflict.
- `If-Match` protects cancel/resume. Competing control actions serialize on the Attempt row; only one expected revision can win.
- Exact event replay returns the prior acceptance. Sequence gaps, altered replays, and stale generations cannot advance state.
- Binding and checkpoint rows are immutable by model and database privilege.
- Workflow acknowledgement ambiguity remains `UNKNOWN`; no success is inferred and no duplicate new command is created.
- Audit append failure rolls back the protected Agent fact.
- Adapter/config/context unavailability fails before a workflow command becomes dispatchable.

## Stacking points

- V0.7 must later supply public Model Deployment, Route, Capability Bundle, Skill, Tool/Context/Network Policy, and effective Configuration snapshots. V0.8 records these as `DEV_FAKE` binding facts and imports no unmerged V0.7 type.
- V0.9 replaces the no-op physical execution path with Sandbox/Lease/Kata/Runner Generation adapters. V0.8 does not add `SandboxPort` methods or repository-write permission.
- V0.10 consumes Agent outcomes through Source Control/Artifact public contracts; V0.8 never pushes or opens an MR.
- Formal Temporal/OpenBao integration must prove TLS/identity/namespace/secret injection and callback recovery separately. Passing the DEV adapter tests is not that evidence.

## Verification and acceptance

The implementation is code-complete only when:

1. Domain tests cover all legal and illegal transitions, immutable binding hashing, no-write permission rejection, event replay/sequence/stale-generation cases, checkpoint requirements, cancel/resume races, and terminal irreversibility.
2. Migration tests prove independent head, constraints, append-only/immutable privileges, audit function access, downgrade protection, and no third-party columns/payloads.
3. Real PostgreSQL/HTTP E2E proves start -> dispatch -> running -> waiting/checkpoint -> authorization revoke denial -> reauthorize resume -> stale-generation rejection -> finalization/success, with correlated Audit.
4. Ruff format/check, mypy, import-linter, all pytest, Alembic heads, and OpenAPI check pass from a clean worktree.
5. The branch is committed and may be pushed after gates are green, but is not merged, tagged, deployed, or described as Release Accepted.
