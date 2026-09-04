# Backend V0.9 Sandboxed Code Execution Design

**Date:** 2026-08-31

**Status:** Approved by the user-provided autonomous execution mandate

**Target:** `engineering-platform-backend`

**Baseline:** `9b71b82602bb4231f6ccc2622c0f49a91287a366`
**Release slice:** V0.9 backend/runtime contract only

## 1. Outcome

V0.9 adds a deep `agent_run` module whose single public execution seam is the
stable `SandboxPort`. The module owns logical Sandbox Environment,
Materialization, Capacity Lease and Runner Generation facts. A separate Sandbox
Controller application exposes the same contract over a private, workload-
authenticated HTTP API and persists its facts in PostgreSQL.

This slice can be independently exercised with a restricted `LAB_ONLY` DEV
adapter, real PostgreSQL and HTTP tests. It does not claim that Kata/KVM, a real
V0.8 Agent Attempt caller, frontend streaming, cluster deployment or release
acceptance exists.

## 2. Evidence and constraints

The decision follows the current architecture documents and current repository,
not a future worktree:

- the architecture names `agent_run` as the Control Plane module and Sandbox
  Controller as an independently scalable, high-risk deployable;
- Sandbox owns Environment, Materialization, Lease and Generation, but not
  Requirement, WorkItem, Attempt, Artifact, Audit or Secret authority;
- the appendix fixes eight `SandboxPort` operations;
- V0.9 is single-repository `fix`; V0.10 owns controlled push/MR delivery and
  V0.12 owns Child/Image Build orchestration;
- the baseline contains no published V0.8 Agent Run/Attempt/Execution Binding
  facade, so V0.9 must not invent one;
- every mutable command requires idempotency, and mutations of an existing
  materialization also require strong revision comparison;
- Kubernetes, Kata and provider concepts may exist only inside a future physical
  adapter. They are forbidden from domain types, HTTP DTOs, database columns,
  OpenAPI and Audit.

## 3. Alternatives considered

### A. Public browser-facing Sandbox CRUD

Rejected. It would let a browser construct execution bindings and bypass the
future Agent owner. It also turns infrastructure lifecycle into a shallow CRUD
surface.

### B. Wait until V0.8 exists and make no V0.9 module

Rejected. The Controller contract, ledger, fencing, cleanup and restricted
adapter can close independently. Waiting would couple a safety boundary to an
unpublished caller and leave no contract for V0.8 to integrate against.

### C. New top-level `sandbox` module

Rejected. The target module map already assigns Agent/Sandbox application facts
to `agent_run`. A second module would create an ownership split with no
independent business responsibility.

### D. `agent_run` deep module plus private Controller transport

Chosen. The package-root facade exposes platform DTOs and `SandboxPort`; the
private HTTP app is an adapter for workload-to-workload calls. Callers learn
eight stable operations and platform facts, while lease locking, fencing,
cleanup, secret revocation and execution-plane details remain local.

## 4. Ownership and deletion test

`agent_run` owns:

- `SandboxEnvironment`: a stable, logical environment association;
- `SandboxMaterialization`: one disposable execution-plane instance;
- `CapacityLease`: an atomic capacity reservation;
- `RunnerGeneration`: the monotonically increasing execution generation;
- command receipts, state revisions, cleanup progress and reconciliation facts.

It does not own:

- Agent Run/Attempt state or Temporal workflow state;
- Requirement, WorkItem, Gate or Decision;
- source code, Artifact bytes, Audit events or Secret values;
- Git push, merge request, registry push or child execution state.

Removing `SandboxPort` would force every caller to duplicate lease admission,
unique generation, fencing, cleanup order and canonical denial handling. It
therefore passes the deletion test. Separate public Lease, Runner, Secret and
Kubernetes ports would merely expose implementation ordering and are rejected.

## 5. Stable platform contract

The package-root facade exports the following logical DTOs. All IDs are opaque
strings at the contract edge. UUID validation is an implementation detail for
facts owned by this module.

### 5.1 References

```text
CommandContext
  idempotencyKey, correlationId, causationId?, requestId?, actor

ExecutionRef
  executionId, kind = SINGLE_REPOSITORY_FIX

SandboxEnvironmentRef
  environmentId, workspaceId, requirementId

RepositoryCheckout
  repositoryId, branchName, baseCommitSha, accessMode = READ_WRITE_WORKTREE

VersionedProfileRef
  id, version, digest

ResourceProfileRef
  id, version, digest, unitWeight

BoundaryManifest
  allowedToolIds[]
  allowedNetworkTargetRefs[]
  secretLeaseRef
  repositoryCheckout
  toolPolicy, networkPolicy, secretScope

ExecutionBindingProjection
  execution, environment, bindingDigest, deadlineAt
  runtimeProfile, resourceProfile, runnerManifest, boundaries
  policyVersions[]
```

`ExecutionBindingProjection` is an immutable Controller input projection. It is
not an Attempt entity and has no foreign key to a nonexistent V0.8 table. When
V0.8 is published, its real owner maps the immutable binding into this DTO.

### 5.2 Handle and evidence

```text
MaterializationHandle
  materializationId, environmentId, executionId
  leaseId, generation, fencingToken, revision, deadlineAt

EvidenceRef
  kind = CHECKPOINT | PATCH | LOG | TEST_RESULT | PREVIEW_METADATA | DIAGNOSTIC
  artifactId, version, sha256, classification

CanonicalDenial
  code, failureDimension?, policyVersion?, diagnosticRef?, retryable
```

The raw fencing token is returned only to an authorized caller and runner. The
generation ledger stores only its cryptographic digest. An idempotency receipt
may retain the response as authenticated AES-GCM ciphertext so the exact handle
can be replayed; plaintext is never stored. The token is never written to Audit,
diagnostics or logs.

### 5.3 Eight operations

```python
class SandboxPort(Protocol):
    def provision_materialization(command) -> ProvisionResult: ...
    def get_materialization_status(query) -> MaterializationStatus: ...
    def publish_preview(command) -> PreviewResult: ...
    def checkpoint_and_release(command) -> ReleaseReceipt: ...
    def handoff_to_child(command) -> HandoffResult: ...
    def finalize_execution(command) -> FinalizationReceipt: ...
    def cancel_execution(command) -> CancellationReceipt: ...
    def reconcile_lease(command) -> ReconciliationReceipt: ...
```

V0.9 behavior:

| Operation | Behavior |
| --- | --- |
| `provisionMaterialization` | Validate deadline and immutable binding, atomically admit capacity, mint one generation/fence, call the restricted execution adapter, and return `READY` only after a matching readiness handshake. |
| `getMaterializationStatus` | Return logical state, revision, current generation and safe evidence/denial references. Never return a host, Pod, provider resource or direct endpoint. |
| `publishPreview` | Publish only through a bound preview capability. The DEV adapter is disabled by default and returns `RUNTIME_CAPABILITY_DENIED`. |
| `checkpointAndRelease` | Persist evidence references, fence, revoke, release and destroy in that order. |
| `handoffToChild` | Stable slot only; V0.9 always returns `POLICY_DISABLED` and creates no child fact. |
| `finalizeExecution` | Persist patch/checkpoint/test evidence and perform the common cleanup chain. It never pushes Git. |
| `cancelExecution` | Idempotently cancel or time out the current active generation using the same cleanup chain. |
| `reconcileLease` | Converge expired/orphaned/partial cleanup facts without guessing external success. |

## 6. Lifecycle and safety algorithms

### 6.1 Provision

The transaction and adapter sequence is:

1. validate the binding schema, digests, fixed repository checkout and deadline;
2. validate the effective admission snapshot and every boundary reference;
3. acquire the execution lock, then lock the environment capacity ledger;
4. reject without a materialization when the policy or physical ceiling would
   be exceeded;
5. reserve the lease, create the materialization and mint the next generation;
6. persist the fencing-token digest, authenticated encrypted recovery capsule,
   and command subject binding in the reservation transaction;
7. ask the execution adapter to materialize exactly that immutable manifest;
8. verify readiness echoes binding digest, generation, protocol and deadline;
9. transition to `READY` with compare-and-swap and write Audit in the same
   database transaction;
10. on any adapter or handshake failure, enter the common cleanup chain and
    return a canonical denial/failure.

The adapter call cannot be part of a database transaction. The durable
`PROVISIONING` state plus reconciliation makes the boundary explicit rather
than pretending distributed atomicity.

### 6.2 Unique generation and fencing

- `(execution_id, generation)` is unique and generation is monotonically
  increasing for an execution;
- a partial unique index permits at most one active generation and one active
  lease for an execution;
- every runner-side mutation checks materialization ID, lease ID, generation,
  fencing token digest and expected revision;
- any mismatch is `STALE_RUNNER_GENERATION`, creates a denial Audit event and
  changes no materialization or evidence fact;
- a new generation can become active only after the previous one is fenced and
  its lease is released or quarantined.

### 6.3 Cleanup

All release/finalize/cancel/timeout/reconciliation paths share one application
routine and an observable sequence:

```text
persist evidence references
  -> fence execution side effects
  -> revoke short-lived secret lease
  -> release capacity lease
  -> destroy materialization
  -> write terminal receipt and Audit
```

The materialization stores timestamps for completed cleanup steps. A retry skips
completed steps and never reverses their order. If a step fails, the state is
`QUARANTINED`; reconciliation resumes from the first incomplete step. Failure to
destroy never reactivates a fence or secret.

Runtime observation is explicitly `PRESENT`, `ABSENT`, or `UNKNOWN`. Only confirmed
absence permits recording execution-side cleanup without an external call;
unknown observation quarantines and retries. Expired command-owned `PROVISIONING`
reservations are reconciliation candidates. A new Controller and restricted DEV
adapter use PostgreSQL facts to resume without process-local token/state recovery.

## 7. Capacity admission

The Controller persists one capacity ledger row per environment and locks it
for admission/release. A private `AdmissionPolicyPort` supplies a provider-free
snapshot:

```text
policyVersion, policyEnabled, activeAttemptLimit, maximumUnits
```

The V0.9 restricted DEV policy has explicit, bounded values and reports
`LAB_ONLY`; it cannot claim Kata/KVM evidence. Admission has no queue, preemption,
steal, overcommit or temporary expansion.

Canonical outcomes are:

- `POLICY_DISABLED` when the effective capability is disabled;
- `POLICY_LIMIT_REACHED` when the active-attempt policy limit is reached;
- `CAPACITY_UNAVAILABLE` when unit capacity is exhausted;
- `RESOURCE_EXHAUSTED` with a safe dimension for a hard resource failure.

## 8. Tool, network, secret and repository boundaries

The immutable `BoundaryManifest` is the only execution-plane authority:

- repository checkout is fixed by repository ID, branch and base commit;
- a repository is data, never configuration for the Controller;
- the restricted adapter never executes repository hooks, startup files or
  symlink escapes;
- tool access is an explicit allowlist and unknown tools are denied;
- network is default-deny and permits only bound target references;
- the guest receives only a short-lived workload/Model Gateway secret lease
  reference; provider, source-control write and registry credentials are
  categorically rejected;
- malformed, missing or widened boundaries fail closed.

The DEV fake adapter validates and records the manifest but does not execute
untrusted repository code. Consequently it proves Controller contracts and
negative cases, not the Kata isolation gate.

## 9. Canonical denial and Audit

Only architecture-defined reason codes cross the facade and HTTP boundary:

- `CAPACITY_UNAVAILABLE`
- `POLICY_LIMIT_REACHED`
- `POLICY_DISABLED`
- `RUNTIME_BINDING_INVALID`
- `RUNTIME_CAPABILITY_DENIED`
- `RUNTIME_BOUNDARY_VIOLATION`
- `STALE_RUNNER_GENERATION`
- `RESOURCE_EXHAUSTED`

Expected denials are discriminated application results. HTTP maps them to RFC
9457 Problem Details with a safe `code` and optional `failureDimension`; adapter
exception names and messages are never returned.

Every protected success, denial, state transition, fence and reconciliation
writes append-only Audit. The envelope contains actor/workload, logical target,
action, result, canonical reason, correlation/request IDs and safe digest/ref
facts. It excludes fencing tokens, Secret values, prompts, source, full URLs,
provider identifiers and infrastructure topology.

Domain mutation and Audit append use the existing `audit.append_event` function
inside the same PostgreSQL transaction. Adapter steps completed outside the
transaction are recorded by the next compare-and-swap transaction.

Each evidence/fence/revoke/capacity-release/destroy CAS appends its own logical
Audit in that same transaction. Terminal and reconciliation Audit preserve the
persisted cancellation enum, including `SECURITY_VIOLATION` and `TERMINATED`.
Status reads append a protected-success event; canonical denials use safe codes
and dimensions, never runtime exception text.

## 10. Idempotency and concurrency

- command scope is `(actor, operation, idempotency_key)`;
- the canonical request fingerprint includes execution, immutable binding or
  handle, intent and expected revision;
- same scope plus same fingerprint replays the sealed HTTP response;
- same scope plus another fingerprint returns an idempotency conflict;
- command records are acquired under row/unique-index protection with an owner,
  expiry, phase and stable operation ID; a live owner conflicts, an expired owner
  can be taken over, and each completion is conditioned on current ownership;
- existing-materialization mutations require `If-Match: "vN"` and map it to
  expected revision;
- stale revisions never overwrite a newer state;
- concurrent provision requests for one execution yield one active lease and
  generation; the losing transaction either replays or returns a canonical
  conflict/active-materialization result;
- cancellation locates and locks the current generation atomically and cannot
  resurrect a terminal materialization.

Provision generation allocation and cancel selection share an execution-scoped
transaction lock across environments. Preview first persists a guarded intent,
then publishes through the stable operation ID. Runtime publication serializes
with fencing, and fencing invalidates prior preview access. Receipt completion
and the correlated external result are committed together.

### 10.1 Workload authorization and time authority

An internal, default-deny `WorkloadAuthorizationPort` authorizes the authenticated
actor, logical operation and environment. All eight Controller operations check
this scope before exposing evidence or changing state, including exact replay.
Identity verification alone grants no operation or environment access. The seam
uses only platform strings; no provider or tenant SDK type enters domain DTOs or
the public `SandboxPort`. Missing grants yield safe audited
`RUNTIME_CAPABILITY_DENIED:workload_scope` failures.

Reconciliation accepts `observedAt` only at or before the injected trusted
Controller clock. Tests advance that clock to establish expiry; request data
cannot advance lease authority. This does not define or infer V0.8 Attempt facts.

## 11. Persistence

Migration `migrations/agent_run/0001_sandbox_runtime_base.py` creates schema
`agent_run` and a `agent_run_rw` NOLOGIN privilege role. It contains only
provider-neutral tables:

- `sandbox_environment`
- `capacity_ledger`
- `sandbox_materialization`
- `capacity_lease`
- `runner_generation`
- `evidence_reference`
- `command_receipt`
- `reconciliation_run`

The recovery migration adds internal command ownership, encrypted materialization
capsules, preview intents and restricted DEV runtime state. These remain
provider-neutral internal facts; they are not a physical runtime implementation.

Important constraints include:

- positive revisions, generation and unit weights;
- immutable binding/profile/boundary snapshots and digests;
- one active materialization/lease/generation per execution;
- fencing-token digest in the generation ledger, never raw token; any copy in a
  replay receipt is authenticated ciphertext;
- valid state/cleanup timestamp combinations;
- idempotency scope uniqueness;
- evidence sequence uniqueness and stable ordering;
- no foreign key to an unpublished V0.8 Attempt.

The role receives only the table and `audit.append_event` privileges required by
this module. It cannot read other business schemas or create/delete/truncate
module facts.

## 12. Private HTTP and OpenAPI

`control_plane.app.bootstrap.sandbox_controller.create_sandbox_controller_app`
is a separate FastAPI application. Its routes live below
`/api/v1/internal/sandbox` and map one-to-one to `SandboxPort`:

- `POST /materializations`
- `GET /materializations/{materializationId}`
- `POST /materializations/{materializationId}/preview`
- `POST /materializations/{materializationId}/checkpoint-release`
- `POST /materializations/{materializationId}/handoff`
- `POST /materializations/{materializationId}/finalize`
- `POST /executions/{executionId}/cancel`
- `POST /leases/reconcile`

Every route requires an authenticated workload principal supplied by a private
`ServiceIdentityVerifier`. Mutable routes require `Idempotency-Key`; mutations
of an existing materialization require strong `If-Match`. The default runtime
fails readiness and protected requests closed when the DEV identity/runtime
configuration is absent.

The main browser API does not mount these routes. Version becomes `0.9.0`, so
`openapi.json` records the backend release slice without adding a browser
Sandbox surface. A separate checked-in `sandbox-openapi.json` captures the
private Controller contract. The existing export/check command verifies both
artifacts deterministically.

## 13. Module layout

```text
control_plane/app/modules/agent_run/
  __init__.py
  api/{dto,routes,runtime}.py
  application/{controller,errors}.py
  domain/{models,policy}.py
  ports/{sandbox,repository,runtime}.py
  adapters/{dev_runtime,sqlalchemy_repository}.py
control_plane/app/bootstrap/sandbox_controller.py
migrations/agent_run/0001_sandbox_runtime_base.py
tests/agent_run/
```

Only the package root is a cross-module facade. Internal layers may consume the
public Audit facade but never another module's internal repository or adapter.
Import contracts and static schema guards enforce this rule and the physical-
type exclusion.

## 14. Verification strategy

Development follows red-green-refactor. Required tests include:

1. facade/domain contract and stable eight-method presence;
2. migration structure, constraints, privileges and forbidden-column scan;
3. immutable binding and boundary validation;
4. real PostgreSQL idempotent replay and payload mismatch;
5. concurrent provisioning yields one active lease/generation;
6. stale generation cannot publish, checkpoint or finalize and creates a safe
   denial Audit;
7. capacity/policy/deadline/binding denials are canonical and audited;
8. malicious repository metadata, hooks, symlink escapes, unbound tools,
   network targets and credential classes fail closed;
9. cleanup ordering and partial-failure reconciliation;
10. cancellation and timeout idempotency;
11. private HTTP camelCase DTOs, bearer workload identity, required headers,
    ETags, Problem Details and replay;
12. real PostgreSQL plus HTTP end-to-end provision/finalize and denial paths;
13. OpenAPI artifact/version checks and main API non-exposure;
14. import and source/schema guards against Kubernetes/Kata/provider leakage;
15. repository-wide format, lint, type, import, migration, tests and OpenAPI
    gates.

## 15. Acceptance and non-claims

This backend/runtime slice is independently code-complete when all tests and
gates pass, critical/important review findings are resolved, and the branch is
committed. It may then be pushed as an implementation branch.

It is not:

- a published V0.8 integration;
- a real Kata/KVM or cluster deployment proof;
- an Agent end-to-end user journey or frontend acceptance;
- V0.10 push/MR delivery;
- V0.12 child/image build;
- backup, HA or release authorization.

Those distinctions remain explicit in the final handoff.
