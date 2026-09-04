# Backend V0.10 Agent Delivery Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to execute this
> plan task-by-task in this session. Use strict red-green-refactor and do not read or
> modify any other worktree.

**Goal:** Deliver a backend-only, credential-brokered Agent push flow whose single-use
grant is bound to Attempt/Repository/task branch/target commit/content digest, whose
unknown outcomes reconcile safely, and whose results cannot change a human Requirement
Decision.

**Architecture:** Add a Source Control-owned Agent Delivery deep module with its own
PostgreSQL effect/fence/fact ledgers. Depend on not-yet-merged Agent/Sandbox behavior
through consumer-owned Ports; expose only stable package-root use cases and a safe human
read query. Invoke an outside-Guest broker through a credential-free Port, persist
`IN_FLIGHT` before the write, reconcile by observation only, and fence monotonically.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy Core, PostgreSQL 18,
Alembic, pytest, Ruff, mypy, import-linter.

**Design source:**
`docs/superpowers/specs/2026-08-31-backend-v10-agent-delivery-design.md`

**Delivery constraints:** Do not edit `docs/superpowers/progress/current.md`; do not add
real GitLab credentials/provider requirements; do not implement parallel-version
modules; do not merge, tag, deploy, clean branches, or claim release acceptance.

---

## Task 1: Lock the migration contract with failing PostgreSQL tests

**Files:**

- Modify: `tests/source_control/test_migration.py`
- Create: `migrations/source_control/0010_source_control_agent_delivery.py`

### Step 1: Write the failing migration tests

Add assertions for:

- the three new tables and exact column/check/index names;
- idempotency and immutable-coordinate uniqueness;
- append-only fact privileges and minimum runtime privileges;
- no grant/fencing token or credential-value columns;
- state-shape database counterexamples;
- a downgrade guard that rejects live V0.10 data.

### Step 2: Run the focused migration tests and observe RED

Run:

```powershell
uv run pytest -q tests/source_control/test_migration.py --basetemp=.pytest_cache/v10-migration-red
```

Expected: failure because `agent_push_request`, `agent_delivery_fence`, and
`agent_delivery_fact` do not exist.

### Step 3: Implement the migration minimally

Create revision `0010_sc_agent_delivery` with down revision `0006_sc_mr_reconcile`.
Install schema checks, indexes, foreign keys to the existing Repository/branch binding,
least-privilege grants, and the fail-closed downgrade guard.

### Step 4: Run the focused tests and observe GREEN

Run the command from Step 2. Expected: all migration tests pass.

### Step 5: Check migration graph and formatting

```powershell
uv run alembic heads
uv run ruff format --check migrations/source_control/0010_source_control_agent_delivery.py tests/source_control/test_migration.py
uv run ruff check migrations/source_control/0010_source_control_agent_delivery.py tests/source_control/test_migration.py
```

## Task 2: Define domain invariants and state transitions test-first

**Files:**

- Create: `tests/source_control/test_agent_delivery_domain.py`
- Create: `control_plane/app/modules/source_control/domain/agent_delivery.py`
- Modify: `control_plane/app/modules/source_control/domain/__init__.py`

### Step 1: Write failing domain tests

Cover:

- exact commit and SHA-256 digest validation;
- positive generation and short, positive TTL;
- artifact reference count/length bounds;
- immutable execution-binding equality;
- allowed and rejected state transitions;
- safe DTO serialization excludes all token/credential fields;
- confirmed and fenced facts have exact versioned topics and mutually exclusive shape.

### Step 2: Run RED

```powershell
uv run pytest -q tests/source_control/test_agent_delivery_domain.py
```

Expected: import failure because the domain API does not exist.

### Step 3: Implement immutable models and pure transition functions

Define `AgentPushState`, command/snapshot/grant/delivery/fact DTOs, safe error types,
fingerprint helpers, token-digest helper, and an explicit transition table. Keep the
domain independent of adapters, HTTP, and other business modules.

### Step 4: Run GREEN and mutation-style counterchecks

Run the focused test. Then temporarily negate one transition guard, confirm the targeted
test fails for the expected reason, restore the guard, and rerun green.

## Task 3: Add consumer Ports, repository adapter, and restricted DEV adapters

**Files:**

- Create: `control_plane/app/modules/source_control/ports/agent_delivery.py`
- Create: `control_plane/app/modules/source_control/adapters/agent_delivery_sqlalchemy.py`
- Create: `control_plane/app/modules/source_control/adapters/agent_delivery_dev.py`
- Modify: `control_plane/app/modules/source_control/ports/__init__.py`
- Modify: `control_plane/app/modules/source_control/adapters/__init__.py`
- Modify: `control_plane/app/modules/source_control/application/dependencies.py`
- Create: `tests/source_control/test_agent_delivery_adapters.py`

### Step 1: Write failing adapter contract tests

Specify:

- repository insert/query/CAS/claim/fence/fact operations against real PostgreSQL;
- `SKIP LOCKED` claims are disjoint;
- the grant issuer returns raw-once plus digest and does not retain the raw value;
- the restricted DEV broker rejects non-DEV mode and supports success, unknown, deny,
  observe, revoke, and freeze without network/filesystem/env/subprocess;
- Port request/response models contain no credential or secret-reference fields.

### Step 2: Run RED

```powershell
uv run pytest -q tests/source_control/test_agent_delivery_adapters.py --basetemp=.pytest_cache/v10-adapter-red
```

### Step 3: Implement the narrow Ports and adapters

Keep SQL column updates allow-listed. Return mappings through domain DTOs. The broker
adapter stores only simulated remote heads and call counters in memory.

### Step 4: Run GREEN

Run the focused adapter tests and then:

```powershell
uv run mypy control_plane/app/modules/source_control tests/source_control/test_agent_delivery_adapters.py
uv run lint-imports
```

## Task 4: Implement grant authorization and single-use execution with TDD

**Files:**

- Create: `control_plane/app/modules/source_control/application/agent_delivery.py`
- Modify: `control_plane/app/modules/source_control/application/__init__.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Create: `tests/source_control/test_agent_delivery_commands.py`

### Step 1: Write failing command tests

Cover real PostgreSQL behavior for:

- exact Repository/branch/execution binding validation;
- missing execution or broker dependencies fail closed;
- bounded expiry and expired grant rejection;
- same idempotency key/same fingerprint replay versus changed fingerprint conflict;
- concurrent grant execution yields exactly one broker push;
- repeated execution after success returns the same snapshot with no second push;
- expected-head conflict is terminal blocked;
- broker unknown becomes `UNKNOWN`, never success;
- audit actions are appended in the same state transaction.

### Step 2: Run RED

```powershell
uv run pytest -q tests/source_control/test_agent_delivery_commands.py --basetemp=.pytest_cache/v10-command-red
```

### Step 3: Implement authorization and execution in transaction phases

Implement public facade functions that accept an Engine/dependencies and open explicit
transactions. Commit `IN_FLIGHT` before calling the broker. Never catch broad exceptions
as success; provider uncertainty has a dedicated safe exception/result.

### Step 4: Run GREEN and concurrency counterexample

Run the focused tests repeatedly:

```powershell
uv run pytest -q tests/source_control/test_agent_delivery_commands.py --basetemp=.pytest_cache/v10-command-green
uv run pytest -q tests/source_control/test_agent_delivery_commands.py -k concurrent --basetemp=.pytest_cache/v10-command-concurrent-1
uv run pytest -q tests/source_control/test_agent_delivery_commands.py -k concurrent --basetemp=.pytest_cache/v10-command-concurrent-2
uv run pytest -q tests/source_control/test_agent_delivery_commands.py -k concurrent --basetemp=.pytest_cache/v10-command-concurrent-3
```

## Task 5: Implement reconciliation, fencing, revocation, and fact outbox

**Files:**

- Create: `control_plane/app/modules/source_control/application/agent_delivery_reconciliation.py`
- Modify: `control_plane/app/modules/source_control/application/__init__.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Create: `tests/source_control/test_agent_delivery_reconciliation.py`

### Step 1: Write failing reconciliation/fence tests

Cover:

- unknown reconciliation observes but never repeats push;
- expected old head reschedules UNKNOWN with bounded backoff;
- exact target head confirms once and emits one confirmed fact;
- third-party head blocks with `REMOTE_HEAD_CONFLICT`;
- concurrent reconcilers claim disjoint rows;
- stale fencing token or a covered generation cannot execute;
- fencing is monotonic/idempotent and requests are locally fenced before revoke;
- revoke unknown does not reopen a grant;
- push/result arriving after fence freezes the branch, stays `FENCED`, emits only the
  fenced fact, and emits no confirmed fact.

### Step 2: Run RED

```powershell
uv run pytest -q tests/source_control/test_agent_delivery_reconciliation.py --basetemp=.pytest_cache/v10-reconcile-red
```

### Step 3: Implement claim/observe/complete and fence/revoke phases

Use leases and compare-and-set guards. Recheck the execution binding/fence after broker
success and before creating a confirmed fact. Keep broker calls outside transactions.

### Step 4: Run GREEN and mutate the post-push fence check

Run focused green. Temporarily remove the post-push fence check and confirm the
push-after-fence counterexample fails, restore it, and rerun green.

## Task 6: Add authenticated, scope-safe HTTP query and OpenAPI 0.10.0

**Files:**

- Modify: `control_plane/app/modules/source_control/api/dto.py`
- Create: `control_plane/app/modules/source_control/api/agent_deliveries.py`
- Modify: `control_plane/app/modules/source_control/api/__init__.py`
- Modify: `control_plane/app/bootstrap/app.py`
- Modify: `control_plane/app/__init__.py`
- Create: `tests/source_control/test_agent_delivery_api.py`
- Modify: `openapi.json` via exporter

### Step 1: Write failing HTTP tests

Test:

- session is required;
- `requirement.read` is checked at the path workspace;
- successful camelCase response contains only safe fields;
- cross-workspace and missing delivery share the same 404 Problem Details;
- SQL outage is 503 Problem Details;
- OpenAPI security and response contracts contain no token, digest-internal, secret,
  credential, environment, path, or command fields.

### Step 2: Run RED

```powershell
uv run pytest -q tests/source_control/test_agent_delivery_api.py --basetemp=.pytest_cache/v10-api-red
```

### Step 3: Implement the read-only route and composition

Compose only query runtime dependencies. Do not add a workload write endpoint. Set
`__version__ = "0.10.0"`.

### Step 4: Run GREEN and regenerate OpenAPI

```powershell
uv run pytest -q tests/source_control/test_agent_delivery_api.py --basetemp=.pytest_cache/v10-api-green
uv run python scripts/export_openapi.py
uv run python scripts/export_openapi.py --check
```

## Task 7: Prove end-to-end behavior and secret non-retention

**Files:**

- Create: `tests/source_control/test_agent_delivery_e2e.py`
- Create: `tests/source_control/test_agent_delivery_secrecy.py`

### Step 1: Write end-to-end and secrecy tests

Use real PostgreSQL plus HTTP query composition and the restricted DEV broker. Exercise:

- authorize -> consume -> broker exact-head -> fact -> human query;
- authorize -> broker unknown -> reconcile exact-head;
- repeated result/execute replay;
- stale generation and push-after-fence;
- a unique sentinel raw grant/fencing token absent from every text/JSON database column,
  audit row, fact payload, HTTP body, exception, and captured log.

### Step 2: Run RED, then make only integration fixes

```powershell
uv run pytest -q tests/source_control/test_agent_delivery_e2e.py tests/source_control/test_agent_delivery_secrecy.py --basetemp=.pytest_cache/v10-e2e-red
```

Do not weaken assertions. Fix composition gaps only, then rerun green.

### Step 3: Run all Source Control tests

```powershell
uv run pytest -q tests/source_control --basetemp=.pytest_cache/v10-source-control
```

## Task 8: Full gates, counterfactual review, and delivery

**Files:**

- Review all changed files from baseline `9b71b82602bb4231f6ccc2622c0f49a91287a366`.

### Step 1: Run static and architecture gates

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run lint-imports
uv run alembic heads
uv run python scripts/export_openapi.py --check
```

### Step 2: Run clean-schema migration and full test suite

Use the isolated V0.10 PostgreSQL container and a repository-local `--basetemp`:

```powershell
uv run alembic upgrade heads
uv run pytest -q --basetemp=.pytest_cache/v10-final
```

Expected: no skips introduced, no warnings caused by V0.10, and all tests pass.

### Step 3: Perform counterfactual and security review

Review the diff for:

- any path that marks UNKNOWN as success;
- any repeated broker write during replay/reconciliation;
- any success fact after a fence;
- any Requirement Decision mutation or automated merge/review;
- any raw token/credential in DTO, persistence, log, audit, fact, error, environment,
  filesystem, command, fixture, or OpenAPI;
- any import of another business module's internals;
- any invented Agent/Sandbox/Artifact/Evidence API;
- any update to `docs/superpowers/progress/current.md`.

Run focused anti-example tests after review.

### Step 4: Commit coherent implementation checkpoints

Use Conventional Commits, for example:

- `feat(source-control): add agent push grant ledger`
- `feat(source-control): reconcile fenced agent delivery`
- `test(source-control): prove agent delivery boundaries`

Before every commit run `git diff --check` and inspect staged files. Do not amend unrelated
or user-owned work.

### Step 5: Verify current branch and push only after fresh green evidence

Confirm branch, clean status, commit SHA, and remote destination. Push only
`codex/backend-v10-agent-delivery`; do not merge, tag, deploy, or delete branches.

### Step 6: Report epistemic status

Report code/contract closure, exact verification evidence, commit/push outcome, and the
remaining real-provider/upstream/deployment/release gates separately.
