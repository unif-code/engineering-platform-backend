# V0.4 SDD & Human Gate Backend Design

## Status and architecture source

This design activates the backend half of the V0.4 vertical slice defined by
`engineering-platform-docs/architecture/12-implementation-roadmap.md`. Domain ownership and terms come
from architecture topic 02 (Requirement Workflow), topic 05 (Source Control & Delivery), and topic 06
(Platform Application & Integration). This repository does not create a second glossary or redefine
those owners.

The V0.3 public surface remains stable. V0.5 Integration MR commands stay dormant even though their
implementation already exists. V0.4 is not complete until the versioned OpenAPI Artifact is consumed by
the frontend and the real SDD/Gate journey is verified.

## Goal

Let an authorized user take a V0.3 Requirement from `PREPARING` through an immutable SDD baseline and a
current-human decision, while also allowing the plan to add and explicitly assign repository-scoped
WorkItems before confirmation.

The code-level journey is:

```text
Requirement CREATED
→ binding request acknowledged → PREPARING
→ create immutable Markdown SDD Artifact version
→ optionally add WorkItems and assign/reassign eligible humans
→ bind the current SDD Artifact as the current baseline
→ submit baseline Gate with exact Route/Artifact/Policy snapshot
→ current eligible reviewer APPROVED | CHANGES_REQUESTED | REJECTED
→ APPROVED makes only assigned + repository-bound WorkItems READY
```

## Scope

Included:

- a persisted, immutable Route Snapshot payload for every new Requirement;
- a narrow V0.4 SDD Artifact implementation for authenticated UTF-8 Markdown, including server-owned
  version and SHA-256;
- adding a repository-scoped WorkItem while a Requirement is `PREPARING`;
- current WorkItem Assignment history and explicit assign/reassign with CAS;
- the existing SDD Baseline, Gate and Decision domain commands, activated through the production app;
- an initial code-owned Gate Policy that assigns the current Workspace owner and records a canonical
  policy snapshot hash and version;
- current reviewer eligibility checked again at decision time;
- a Requirement detail projection containing the current Route, SDD Baseline, Gate, Gate Assignment and
  Decision metadata;
- OpenAPI `0.4.0`, migration, authorization, Audit, idempotency and denial behavior.

Excluded:

- V0.5 WorkItem start, Integration MR creation/merge, GitLab commit or delivery UI;
- generic attachment upload, binary files, presigned URLs, scanner workflow, object storage and Artifact
  acceptance evidence (V0.6);
- Agent, Model, Chat, Sandbox, deployment, backup, capacity or HA;
- changing V0.3 Branch Binding ownership or writing Source Control tables from Requirement.

## Module and seam decisions

### Keep one deep Requirement module

Route, SDD baseline semantics, WorkItem responsibility, Gate Assignment and Decision are one cohesive
Requirement Workflow module. Creating a separate `sdd`, `task`, or `approval` module would only move the
same invariants into cross-module calls. The package-root Requirement Facade remains the external seam;
HTTP routes and Source Control callbacks call that Facade rather than internal layers.

The deletion test supports this placement: deleting `requirement` would force Route versioning, WorkItem
readiness, Gate subject binding, CAS, idempotency and Audit into multiple callers. The module therefore
earns its depth.

### Persist the full Route Snapshot

V0.3 stores only `routeSnapshotVersion` and `routeSnapshotHash`. V0.4 adds the canonical JSON payload so
future WorkItem splitting uses the Requirement's frozen route rather than today's catalog.

The V0.4 catalog is version 2 and stores stable step codes:

| Requirement type | Route steps |
| --- | --- |
| `feat` | `brainstorming`, `writing-plans`, `test-driven-development`, `verification-before-completion`, `requesting-code-review` |
| `fix` | `systematic-debugging`, `test-driven-development`, `verification-before-completion`, `requesting-code-review` |
| `refactor`, `chore` | `writing-plans`, `test-driven-development`, `verification-before-completion`, `requesting-code-review` |

The snapshot also contains the Requirement type and required WorkItem capabilities. Its SHA-256 is over
canonical JSON. Existing V0.3 rows are backfilled with their exact version-1 payload so their recorded
hash does not change.

### Narrow SDD Artifact adapter

V0.4 accepts only authenticated UTF-8 Markdown through the Requirement HTTP interface. The server:

- normalizes line endings, enforces a bounded size and rejects empty text;
- owns Artifact ID/version allocation and hashes the exact stored UTF-8 bytes;
- stores immutable versions in the Requirement schema;
- exposes content only through an authorized Requirement-scoped read;
- identifies the snapshot as `AVAILABLE`, `text/markdown; charset=utf-8` and
  `TRUSTED_PLAIN_TEXT`.

This is the first development/Team-Trial adapter behind the existing Artifact seam. It does not claim to
be the V0.6 general Artifact/object-storage implementation and accepts no binary or remote URL. A later
object adapter can preserve the same immutable snapshot interface without changing Gate semantics.

### Initial Gate Policy

V0.4 uses one explicit code-owned policy:

```text
policyCode = REQUIREMENT_BASELINE_WORKSPACE_OWNER
version = 1
default reviewer = current Workspace owner at Gate creation
```

The adapter computes a canonical policy snapshot hash and reads the owner through the Workspace public
Facade. Gate creation stores policy code, version and hash. This is a real versioned policy, not an env
fallback. Configuration-managed Workspace overrides remain a later compatible adapter; absence or
failure of the Workspace/authorization dependencies fails closed.

### Current-human checks

Gate creation records a Gate Assignment. Decision succeeds only when:

- the actor equals the current Gate assignee;
- the account remains enabled;
- the actor remains a formal member of the Requirement Workspace;
- `requirement.baseline.decide` remains effective for that Workspace;
- the Gate subject still matches Requirement version, Route snapshot and current SDD baseline;
- the caller supplies the current Requirement ETag and a stable Idempotency-Key.

Changing the Workspace owner does not rewrite an existing Gate Assignment. A dedicated reassign command
supersedes the prior Assignment, records a reason, increments Gate revision and rechecks candidate
eligibility.

## WorkItem planning and Assignment

### Add WorkItem

`POST /api/v1/requirements/{requirementId}/work-items` accepts only a repository ID. It is permitted only
while the Requirement is `PREPARING`, before an open Gate. The server:

- reads capabilities from the frozen Route Snapshot;
- auto-assigns the actor only if current membership, grants, repository access and resource guard all
  pass; otherwise it creates an unassigned WorkItem;
- appends a new Branch Binding request Outbox record;
- increments `requirementVersion`, `requiredWorkItemSetVersion` and Requirement revision;
- recomputes the required WorkItem set hash from the ordered stable WorkItem IDs;
- clears the current baseline pointer because its semantic subject is stale;
- never writes Source Control storage directly.

### Assign/reassign WorkItem

`POST .../work-items/{workItemId}:assign` takes `humanOwnerId` and a reason. The actor needs
`work_item.assign`; the candidate must currently satisfy account, Workspace, required capability,
repository access and resource-guard checks. The command:

- uses the WorkItem ETag and Idempotency-Key;
- supersedes the prior append-only Assignment and creates a new current Assignment;
- updates the WorkItem owner/executor projection;
- re-emits a deterministic Branch Binding request when an unassigned/owner-blocked item still lacks a
  binding;
- leaves a bound branch immutable;
- rejects WorkItems that have started V0.5 delivery.

The Assignment row is the responsibility history; WorkItem owner fields are the current projection.

## Readiness correction

V0.3 derived WorkItem `READY` from only `ASSIGNED + BOUND`. V0.4 corrects the invariant:

```text
WorkItem READY iff
  Requirement state is READY
  and Assignment state is ASSIGNED
  and Repository state is BOUND
  and WorkItem has not started delivery.
```

Repository callbacks before Gate approval therefore keep the WorkItem `DRAFT`. An APPROVED Decision
promotes every eligible required WorkItem in the same Requirement transaction. `CHANGES_REQUESTED`
returns not-started WorkItems to `DRAFT`; `REJECTED` cancels them. No Source Control outcome can bypass
the human baseline Gate.

## HTTP contract

V0.4 activates these commands while retaining V0.3 list/create/detail:

- `POST /api/v1/requirements/{requirementId}/sdd-artifacts`
- `GET /api/v1/requirements/{requirementId}/sdd-artifacts/{artifactId}/versions/{artifactVersion}`
- `POST /api/v1/requirements/{requirementId}/work-items`
- `POST /api/v1/requirements/{requirementId}/work-items/{workItemId}:assign`
- `POST /api/v1/requirements/{requirementId}/sdd-baselines`
- `POST /api/v1/requirements/{requirementId}/baseline-confirmations`
- `POST /api/v1/requirements/{requirementId}/baseline-gates/{gateId}:reassign`
- `POST /api/v1/requirements/{requirementId}/baseline-decisions`

All mutation bodies use strict camelCase DTOs, return RFC 9457 failures, require same-origin protection,
and use server-derived actor, state, hash, version and timestamps. Versioned aggregate writes require a
strong `If-Match`; every mutation requires `Idempotency-Key`.

The default app still excludes all V0.5 delivery command routes.

## Persistence

Requirement migration `0005` adds:

- `requirement.route_snapshot JSONB NOT NULL` with canonical backfill;
- `requirement.sdd_artifact_version` with immutable `(artifact_id, version)` identity, Requirement
  ownership, content, media type, SHA-256, trust/state and creator/time;
- `requirement.work_item_assignment` with append-only revisions and one current partial-unique row;
- Gate policy code/hash columns;
- the minimum grants needed by `requirement_rw`.

Existing assigned WorkItems receive a deterministic revision-1 Assignment whose ID equals the WorkItem
ID. Existing V0.3 Gate rows, if any, receive the version-1 code-policy snapshot metadata; their decisions
are not rewritten.

## Failure and concurrency behavior

- Artifact identity/version/hash mismatch: fail closed and retain immutable prior versions.
- Stale Requirement, WorkItem or Gate ETag: `409` with a safe problem title; caller must refetch.
- Missing policy, Workspace owner, account, membership, grants or repository eligibility: `503` for
  unavailable dependencies or `403/409` for deterministic denial; never select a fallback reviewer.
- Same Idempotency-Key + same canonical payload: replay the sealed response.
- Same key + different payload: conflict with no new facts.
- Concurrent Assignment or Gate reassignment: at most one current row; loser receives a stale revision.
- Existing GitLab branch or Source Control reconciliation remains authoritative and is never guessed.

## Verification and release status

Required evidence before backend merge:

- migration upgrade/downgrade and least-privilege tests on real PostgreSQL;
- domain/application tests for every state, replay, stale subject and dependency failure;
- default-app OpenAPI tests proving V0.4 routes are active and V0.5 routes absent;
- real HTTP journey with two principals, reassignment, changes requested, new Artifact version and final
  approval;
- Ruff, MyPy, import-linter, Alembic heads, full PostgreSQL suite and deterministic OpenAPI export;
- fixed-HEAD review with no unresolved Critical or Important finding.

Passing these gates means backend V0.4 code complete candidate only. GitHub CI, `api-v0.4.0` Artifact,
frontend consumption, deployment, external Chrome acceptance and explicit Release Acceptance remain
separate evidence.
