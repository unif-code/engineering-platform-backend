# Backend V0.10 Agent Delivery Design

**Date:** 2026-08-31

**Status:** Approved implementation design

**Baseline:** `9b71b82602bb4231f6ccc2622c0f49a91287a366`

**Branch:** `codex/backend-v10-agent-delivery`

## 1. Decision source and scope

This design implements the backend-only V0.10 gate defined by the approved flow-first
roadmap and architecture documents. The user explicitly authorized autonomous design,
specification, planning, implementation, verification, review, commit, and a push only
after the branch is independently closed and green.

In scope:

- a Source Control-owned Agent Delivery deep module;
- a credential-broker Port whose implementation is outside the Guest;
- single-use, short-TTL push grants bound to Attempt, Repository, task branch, target
  commit, and content digest;
- explicit consumer-side stacking Ports for not-yet-merged Agent/Sandbox contracts;
- exact remote-head verification, UNKNOWN, reconciliation, fencing, revocation, audit,
  and safe delivery facts;
- a restricted DEV adapter with no real credentials or provider prerequisite;
- PostgreSQL integration and authenticated HTTP query coverage;
- OpenAPI version `0.10.0` and generated-contract consistency.

Out of scope:

- implementing or inventing Agent Run, Sandbox, Artifact, Evidence, or Requirement-owned
  storage and APIs from parallel V0.5-V0.9 work;
- changing any Requirement human Decision or automating Review, acceptance, or merge;
- a real GitLab credential, real provider push, deployment, backup, HA, frontend, tag,
  merge, or release acceptance;
- compatibility shims for older prototypes.

No other worktree is an input. Missing upstream contracts are represented only by
V0.10-owned consumer Ports and versioned delivery facts.

## 2. Boundary and deletion test

Source Control already owns task-branch bindings, merge-request bindings, external
effects, and reconciliation. Agent push delivery is another Source Control external
effect and stays inside that module.

The new deep module owns all of the following together:

- push-grant lifecycle and one-time consumption;
- immutable binding validation at the Source Control boundary;
- broker invocation and exact readback;
- delivery-effect state and reconciliation leasing;
- attempt fencing and broker revocation;
- secret-free audit facts and downstream delivery facts.

The public package-root facade exposes use cases and stable DTOs. Callers do not import
the new domain, application, Port, or adapter internals.

Deletion test: removing the deep module removes agent push capability entirely while
leaving branch creation, integration MR creation/merge, and human Requirement Decision
semantics intact. Therefore it has an independent responsibility and is not a wrapper
around existing Source Control code.

## 3. Chosen shape and rejected alternatives

### Chosen: dedicated Agent Delivery effect ledger

`source_control.agent_push_request` is a dedicated effect ledger. It reuses the existing
Source Control module boundary and reconciliation principles, but does not stretch
`source_control.source_control_effect` with Attempt-, token-, expiry-, fence-, or content-
specific nullable columns.

### Rejected: extend the generic effect table

That table currently represents branch and MR operations keyed by WorkItem/MR subjects.
Agent delivery has different uniqueness, security, and lifecycle invariants. Extending
it would weaken its existing operation-shape checks and couple unrelated migrations.

### Rejected: create placeholder upstream modules

Creating local Agent Run, Sandbox, Artifact, Evidence, or Requirement APIs would fake
contracts owned by parallel versions. V0.10 instead defines consumer-owned Ports and a
versioned fact outbox that upstream owners can implement or consume later.

### Rejected: let the Guest push directly

Direct push requires credentials or credential-bearing helpers inside the Guest and
cannot provide broker-side single-use enforcement. The Guest receives only an opaque,
one-time grant; the broker resolves provider credentials outside the Guest.

## 4. Domain model

### 4.1 Immutable execution binding

`AgentExecutionBindingSnapshot` is returned by `AgentExecutionBindingPort` after it
validates the presented raw fencing token. It contains only non-secret facts:

- `attempt_id` and positive `attempt_generation`;
- `execution_binding_digest` (`sha256:<64 lowercase hex>`);
- `requirement_id`, `work_item_id`, `workspace_id`;
- `repository_id`, `branch_binding_id`, and exact task `branch_name`;
- `active` and `fenced` state.

The raw fencing token is method input only. It is never included in a DTO, exception,
audit envelope, broker request, event payload, or database row.

### 4.2 Push authorization command

The trusted orchestration caller supplies:

- idempotency key and correlation id;
- all immutable execution-binding coordinates;
- expected remote head SHA;
- target commit SHA;
- content digest (`sha256:<64 lowercase hex>`);
- zero or more opaque artifact references;
- requested expiry.

Authorization performs these checks in order:

1. required IDs, hashes, task-branch name, and expiry shape are valid;
2. TTL is positive and no longer than policy `max_push_grant_ttl`;
3. Repository is `AUTHORIZED` and the stored branch binding matches workspace,
   requirement, WorkItem, repository, binding id, and branch exactly;
4. `AgentExecutionBindingPort` returns the same active, unfenced binding for the raw
   fencing token;
5. no local fence covers the Attempt generation;
6. idempotency and coordinate uniqueness are satisfied.

The result is `AgentPushGrant(request_id, grant_token, expires_at)`. `grant_token` is a
control-plane capability, not a Source Control credential. Only its SHA-256 digest is
stored. A replay with the same idempotency key and fingerprint returns the existing
request but never reissues the raw token; the caller must retain the first response.
A changed fingerprint is an idempotency conflict.

### 4.3 Effect states

`AgentPushState`:

- `AUTHORIZED`: valid, unused grant;
- `IN_FLIGHT`: token consumed and broker call started;
- `UNKNOWN`: broker outcome cannot be proven;
- `RECONCILIATION`: leased reconciliation attempt;
- `SUCCEEDED`: exact remote head equals the bound target commit while still unfenced;
- `BLOCKED`: terminal safe failure, including expired, rejected, or head conflict;
- `FENCED`: terminal local denial or a push observed after the generation was fenced.

Allowed transitions are explicit. Terminal rows never return to an active state.
`IN_FLIGHT` is persisted before the external call. A timeout or uncertain result becomes
`UNKNOWN`; it is never treated as success.

### 4.4 Single use and replay

The grant digest is consumed by an atomic compare-and-set from `AUTHORIZED` to
`IN_FLIGHT`, guarded by:

- request id;
- grant digest;
- unexpired `expires_at`;
- expected state;
- absence of a covering local fence.

Only one concurrent caller can win. Repeated execution after success returns the same
safe delivery snapshot without another broker write. Repeated execution while unknown
does not retry the write; reconciliation performs read-only observation.

### 4.5 Fencing and revocation

`source_control.agent_delivery_fence` stores the highest fenced generation for an
Attempt and a safe reason code. Fencing is monotonic and idempotent.

The fence use case commits local fencing first, atomically transitions all nonterminal
requests at or below the generation to `FENCED`, and appends audit/fact rows. It then
asks `AgentPushBrokerPort.revoke()` to revoke outstanding broker capability. Broker
revocation failure is recorded as a safe audit outcome and retried independently; it
does not reopen locally fenced grants.

The execution binding is checked before the broker call and again after the call. A
fence race therefore cannot produce a successful delivery fact. If reconciliation later
observes the target commit after the fence, the broker freezes the task branch and the
ledger remains `FENCED` with reason `PUSH_OBSERVED_AFTER_FENCE`.

## 5. Ports and adapters

### `AgentExecutionBindingPort`

Consumer-owned validation seam for the future Agent/Sandbox owner. It validates the raw
fencing token and returns the immutable snapshot. Absence of this Port fails closed.

### `AgentPushBrokerPort`

The Port exposes only:

- `push_and_verify(BrokerPushRequest) -> BrokerPushObservation`;
- `observe(BrokerPushLocator) -> BrokerPushObservation`;
- `revoke(BrokerGrantLocator) -> BrokerRevocationResult`;
- `freeze_branch(BrokerPushLocator) -> BrokerFreezeResult`.

No method accepts or returns a provider credential, secret reference, environment
mapping, filesystem path, source archive, or command string. A production broker owns
credential resolution and pack transport behind this Port.

### Restricted DEV adapter

`RestrictedDevAgentPushBroker` is deterministic and in-memory. It supports exact-head
success, unknown result, denial, observation, revocation, and branch freeze. It refuses
non-DEV construction and has no network, subprocess, filesystem, environment, or secret
dependency. It is test/DEV evidence only, never proof of a real provider call.

### Grant token issuer

`AgentPushGrantIssuerPort` returns a cryptographically random opaque token plus its
SHA-256 digest. The production local adapter uses `secrets.token_urlsafe`; tests use a
deterministic fake. Raw tokens are returned once and never persisted.

## 6. Persistence and concurrency

Migration `0010_sc_agent_delivery` follows `0006_sc_mr_reconcile` and adds:

- `agent_push_request` effect ledger;
- `agent_delivery_fence` monotonic fence ledger;
- `agent_delivery_fact` append-only downstream fact outbox.

Database checks enforce UUID/reference shape, exact 40-hex commit SHAs, SHA-256 digest
shape, positive generation, expiry after issue, state-specific timestamps/head/error
shape, bounded JSON artifact references, and safe topic/state values.

Uniqueness:

- idempotency key;
- immutable push coordinate `(attempt_id, repository_id, branch_name,
  target_commit_sha, content_digest)`;
- one fact `(push_request_id, topic)` per semantic outcome;
- one fence row per Attempt.

Runtime role grants are least privilege:

- request and fence ledgers: `SELECT, INSERT, UPDATE`, no delete/truncate;
- facts: `SELECT, INSERT`, no update/delete/truncate.

Claiming uses `FOR UPDATE SKIP LOCKED`. State changes use state/generation/attempt
compare-and-set guards. Downgrade fails closed if any V0.10 row exists.

## 7. External-effect flow

### Authorize

1. Validate command and execution snapshot.
2. Lock Repository and task-branch binding; validate exact ownership coordinates.
3. Check local fence and idempotency.
4. Issue the raw grant and persist only its digest with `AUTHORIZED`.
5. Append `source_control.agent-push-authorized` audit.
6. Commit, then return the one-time raw grant.

### Execute

1. Revalidate execution binding using the raw fencing token.
2. Hash the presented grant token.
3. Atomically consume the grant and persist `IN_FLIGHT`; commit.
4. Invoke the external broker outside the transaction.
5. On exact verified head, revalidate execution/fence and persist `SUCCEEDED` plus one
   delivery fact atomically.
6. On uncertain result, persist `UNKNOWN` and schedule reconciliation.
7. On safe denial/conflict, persist `BLOCKED`.

### Reconcile

1. Claim due `UNKNOWN`, expired `IN_FLIGHT`, or expired reconciliation leases with
   `SKIP LOCKED`; persist `RECONCILIATION` and commit.
2. Call broker `observe` only; never repeat the push.
3. Exact target head while unfenced becomes `SUCCEEDED` plus a confirmed fact.
4. Exact target head while fenced triggers branch freeze plus a fenced-drift fact.
5. Expected old head remains retryable UNKNOWN with bounded backoff.
6. Any other head becomes terminal `BLOCKED` with `REMOTE_HEAD_CONFLICT`.

## 8. Integration seams

The confirmed fact topic is `source-control.agent-push-confirmed.v1`. Its payload
contains only stable IDs, the target commit SHA, content digest, artifact references,
`executorType=AGENT`, Attempt generation, correlation id, and observation time.

The fenced-drift fact topic is `source-control.agent-push-fenced.v1` and contains the
same safe binding facts plus the fence reason. It never masquerades as delivery success.

Future Artifact/Evidence/Reconciliation owners consume these facts through the public
Source Control facade. Existing Branch and MR seams remain authoritative:

- the request must reference an existing task-branch binding;
- successful delivery updates no MR and performs no merge;
- a human may later use the existing integration-MR flow;
- no V0.10 code writes Requirement tables or invokes a Decision transition;
- Agent success, failure, UNKNOWN, or fencing cannot change human Decision.

## 9. HTTP and OpenAPI

No Agent push command is exposed over the human session API because the workload
identity/authentication contract belongs to an unmerged upstream module. Publishing a
human-authenticated write route would weaken the boundary.

V0.10 adds a human read-only query:

`GET /api/v1/workspaces/{workspaceId}/agent-deliveries/{deliveryId}`

It requires the existing `requirement.read` capability scoped to the workspace, rejects
cross-workspace access as not found, and returns only safe ledger facts. It never returns
grant/fencing digests, request fingerprints, broker internals, secret references, or
credential-like fields. Missing and cross-scope rows have the same 404 response.

The application/OpenAPI version becomes `0.10.0`; this is a code contract version only,
not a release, tag, deployment, or acceptance claim.

## 10. Audit and secrecy

Audit actions include authorization, consumption, confirmed delivery, unknown result,
blocked delivery, reconciliation, fence, revocation result, and fenced drift. Envelopes
contain system actor, stable target id, safe result/reason code, and correlation id only.

Tests use a high-entropy sentinel and inspect database text values, audit rows, fact
payloads, HTTP bodies, exceptions, and captured logs. The sentinel must appear nowhere.
Production code must not log request objects or serialize grant/fencing tokens.

No provider credential, real secret reference, `.env` value, guest environment entry,
trace value, artifact content, command line, or persistent-disk credential is introduced.

## 11. Verification and acceptance boundary

Required evidence:

- domain transition/validation tests;
- PostgreSQL migration, constraints, downgrade guard, and least-privilege tests;
- concurrent single-use authorization/execution and reconciliation claims;
- HTTP authentication, capability, cross-workspace, and secrecy tests;
- repeated push is one external write;
- result-unknown is never success and reconciliation never repeats the write;
- stale fencing token and covered generation are denied;
- a push observed after fence freezes the branch and emits no confirmed fact;
- full Ruff, mypy, import-linter, Alembic, pytest, and OpenAPI checks.

Passing these gates closes the V0.10 code branch only. It does not prove a real GitLab
push, merged upstream contracts, deployment, release, or human acceptance.
