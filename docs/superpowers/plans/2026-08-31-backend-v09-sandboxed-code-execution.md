# Backend V0.9 Sandboxed Code Execution Implementation Plan

> **Execution rule:** Follow red-green-refactor for every behavior task. Run the
> named focused test first and observe the intended failure before production
> code. Do not modify `docs/superpowers/progress/current.md` without the exact
> user command `【同步进度】`.

**Goal:** Deliver an independently testable V0.9 Sandbox Controller contract,
PostgreSQL ledger, restricted DEV adapter, private HTTP API and OpenAPI artifact
without inventing V0.8 Attempt facts or V0.10 delivery effects.

**Architecture:** Add the documented `agent_run` deep module. Its package-root
facade exposes platform DTOs and the eight-method `SandboxPort`; a separate
FastAPI Controller app adapts private workload-authenticated HTTP to the same
application service. SQL and execution-plane details remain behind internal
ports. State changes and canonical Audit append in one module transaction;
external adapter steps use durable intermediate states and reconciliation.

**Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, PostgreSQL 18,
Alembic, cryptography/AES-GCM, pytest, Ruff, mypy, import-linter.

**Design source:**
`docs/superpowers/specs/2026-08-31-backend-v09-sandboxed-code-execution-design.md`

## Global execution constraints

- Work only in
  `D:\tongyi\code\engineering-platform\.worktrees\engineering-platform-backend-v09`.
- Keep all database tests on the dedicated PostgreSQL instance at
  `127.0.0.1:55439`; never use the shared 5432 test database.
- Never read, copy or merge another version worktree.
- Never print or commit real credentials. Test tokens/keys must be visibly local
  fixtures.
- Do not add public browser Sandbox routes to the main Control Plane app.
- Do not add Kubernetes/Kata/provider types outside a future physical adapter;
  V0.9 contains no such dependency.
- Do not implement Git push/MR, child execution, frontend, deployment, backup or
  HA.
- Commit cohesive checkpoints after their focused gates pass.

## Task 1: Lock the package-root contract and module boundary

**Files:**

- Create: `tests/agent_run/test_contract.py`
- Create: `control_plane/app/modules/agent_run/domain/models.py`
- Create: `control_plane/app/modules/agent_run/domain/__init__.py`
- Create: `control_plane/app/modules/agent_run/ports/sandbox.py`
- Create: `control_plane/app/modules/agent_run/ports/__init__.py`
- Create: `control_plane/app/modules/agent_run/__init__.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_contract_guard.py`

**Red:** Write tests that import only the package root, assert the exact eight
method names, verify frozen/versioned platform DTOs, and prove no physical or
provider vocabulary is exported. Add `agent_run` to the module-layer and
package-root Facade guard expectations. Run:

```powershell
uv run pytest tests/agent_run/test_contract.py tests/test_contract_guard.py -q
```

Expected failure: package/module contract does not exist.

**Green:** Implement enums, immutable Pydantic/domain DTOs, discriminated
results, commands, the `SandboxPort` protocol and the narrow package-root
exports. Add symmetric import-linter rules for `agent_run`.

**Refactor/verify:** Remove any unneeded wrapper or future Attempt/Child DTO.
Run the focused tests plus:

```powershell
uv run lint-imports
uv run mypy control_plane/app/modules/agent_run tests/agent_run/test_contract.py
```

## Task 2: Create the provider-neutral PostgreSQL ledger

**Files:**

- Create: `tests/agent_run/conftest.py`
- Create: `tests/agent_run/test_migration.py`
- Create: `migrations/agent_run/0001_sandbox_runtime_base.py`
- Modify: `alembic.ini`
- Modify: `control_plane/app/shared/db/settings.py`

**Red:** Add real PostgreSQL tests for expected tables, constraints, partial
unique indexes, append-only/least-privilege grants, NOLOGIN role, separate
runtime URL, fencing-token digest storage and forbidden column/vocabulary scan.
Test that `agent_run_rw` cannot read other business schemas, delete/truncate,
create tables or update immutable binding facts. Run:

```powershell
uv run pytest tests/agent_run/test_migration.py -q
```

Expected failure: migration branch/schema/settings do not exist.

**Green:** Create `agent_run` schema and the eight design tables, constraints,
indexes and least-privilege grants. Add the migration head and
`AGENT_RUN_DATABASE_URL`.

**Refactor/verify:** Recreate/migrate the dedicated database and run the test as
the runtime role. Check both upgrade and downgrade source discipline; do not
embed runtime passwords.

## Task 3: Validate immutable bindings and restricted DEV boundaries

**Files:**

- Create: `tests/agent_run/test_domain_policy.py`
- Create: `tests/agent_run/test_dev_runtime_adapter.py`
- Create: `control_plane/app/modules/agent_run/domain/policy.py`
- Create: `control_plane/app/modules/agent_run/ports/runtime.py`
- Create: `control_plane/app/modules/agent_run/adapters/dev_runtime.py`
- Create: `control_plane/app/modules/agent_run/adapters/__init__.py`

**Red:** Specify validation for deadline, digests, runner protocol, fixed branch
and base commit, positive unit weight, explicit tools, default-deny network
target references and secret-lease class. Add malicious repository fixtures for
`.git` control data, executable hook declarations, symlink/junction escapes,
unknown tools, raw URLs/IPs, provider credential classes and widened checkout.
Run:

```powershell
uv run pytest tests/agent_run/test_domain_policy.py tests/agent_run/test_dev_runtime_adapter.py -q
```

Expected failure: policy and adapter do not exist.

**Green:** Implement pure validation and a deterministic `LAB_ONLY` restricted
adapter. It treats repository snapshots as data, never executes source or hooks,
never follows links, validates the immutable manifest and returns a logical
readiness handshake. It must not emit Kata/KVM success evidence.

**Refactor/verify:** Keep filesystem/DEV mechanics in the adapter; domain and
API remain provider neutral.

## Task 4: Implement SQL repository, command receipts and atomic admission

**Files:**

- Create: `tests/agent_run/test_sql_repository.py`
- Create: `tests/agent_run/test_capacity_concurrency.py`
- Create: `control_plane/app/modules/agent_run/ports/repository.py`
- Create: `control_plane/app/modules/agent_run/adapters/sqlalchemy_repository.py`
- Create: `control_plane/app/modules/agent_run/application/errors.py`
- Create: `control_plane/app/modules/agent_run/application/__init__.py`

**Red:** Against real PostgreSQL, test:

- environment/ledger creation is idempotent;
- same command scope/fingerprint can replay a completed sealed receipt;
- same key with another fingerprint conflicts;
- one active materialization, lease and generation per execution;
- capacity and active-attempt limits cannot overcommit;
- 8+ concurrent provision reservations yield exactly one active generation and
  never negative/over-limit counters;
- stale revision/fence checks mutate no rows.

Run:

```powershell
uv run pytest tests/agent_run/test_sql_repository.py tests/agent_run/test_capacity_concurrency.py -q
```

Expected failure: repository/admission implementation does not exist.

**Green:** Implement raw SQL/SQLAlchemy repository operations with explicit row
locks, unique-index race handling, positive monotonic counters, CAS updates,
SHA-256 fence digests and AES-GCM sealed command receipts. Keep receipt
plaintext outside persistence.

**Refactor/verify:** Make transaction ownership explicit; do not hide network or
execution adapter calls inside repository methods.

## Task 5: Provision through the deep application service

**Files:**

- Create: `tests/agent_run/test_controller_provision.py`
- Create: `control_plane/app/modules/agent_run/application/controller.py`
- Modify: `control_plane/app/modules/agent_run/__init__.py`

**Red:** Test successful provision ordering, readiness echo validation,
idempotent exact replay, capacity/policy/deadline/binding denials, adapter
failure cleanup, safe result mapping and transactionally appended Audit. Assert
Audit contains canonical logical facts but no fence, secret, source, URL,
provider or physical topology. Run:

```powershell
uv run pytest tests/agent_run/test_controller_provision.py -q
```

Expected failure: application controller is missing.

**Green:** Implement `SandboxDependencies` and `SandboxController`. Use durable
`PROVISIONING`, call the execution adapter outside a database transaction,
validate readiness, CAS to `READY`, seal the response receipt and append Audit
through the existing transactional appender. Map all expected failures to the
canonical denial set.

**Refactor/verify:** The controller is the only owner of provision ordering;
callers and adapters must not reproduce it.

## Task 6: Enforce generation/fence on preview, checkpoint and finalization

**Files:**

- Create: `tests/agent_run/test_generation_fencing.py`
- Modify: `control_plane/app/modules/agent_run/application/controller.py`
- Modify: `control_plane/app/modules/agent_run/adapters/sqlalchemy_repository.py`
- Modify: `control_plane/app/modules/agent_run/adapters/dev_runtime.py`

**Red:** Prove the current generation can publish only a bound preview, persist
ordered evidence and finalize; an older generation/token/revision cannot
publish, checkpoint or finalize. Every stale attempt returns
`STALE_RUNNER_GENERATION`, changes no owned fact and appends a safe denial Audit.
Prove V0.9 `handoffToChild` is present but returns `POLICY_DISABLED` and creates
no child row. Run:

```powershell
uv run pytest tests/agent_run/test_generation_fencing.py -q
```

Expected failure: operations are unimplemented.

**Green:** Implement handle validation, ordered evidence refs, disabled preview
default, fixed handoff denial, finalization intent and common result types.

**Refactor/verify:** No Git push, MR, registry or child execution helper may
appear.

## Task 7: Unify release, cancel, timeout and reconciliation cleanup

**Files:**

- Create: `tests/agent_run/test_cleanup.py`
- Create: `tests/agent_run/test_reconciliation.py`
- Modify: `control_plane/app/modules/agent_run/application/controller.py`
- Modify: `control_plane/app/modules/agent_run/ports/runtime.py`
- Modify: `control_plane/app/modules/agent_run/adapters/dev_runtime.py`
- Modify: `control_plane/app/modules/agent_run/adapters/sqlalchemy_repository.py`

**Red:** With an adapter call log and injected failures, assert the sequence:

```text
evidence -> fence -> secret revoke -> lease release -> destroy -> terminal Audit
```

Test idempotent cancellation, timeout semantics, already-terminal replay,
partial failure to `QUARANTINED`, no reactivation after revoke/release, expired
lease reconciliation, orphan/unknown fail-closed behavior and stable receipt
ordering. Run:

```powershell
uv run pytest tests/agent_run/test_cleanup.py tests/agent_run/test_reconciliation.py -q
```

Expected failure: common cleanup/reconciliation is absent.

**Green:** Implement one resumable cleanup routine backed by completion
timestamps and a reconciler that continues from the first incomplete step.
Cancellation locates/locks the current generation; timeout is a reason, not a
second implementation.

**Refactor/verify:** Remove duplicated cleanup code and ensure failure cannot
restore a fence, lease or secret.

## Task 8: Add private workload-authenticated Controller HTTP API

**Files:**

- Create: `tests/agent_run/test_api.py`
- Create: `control_plane/app/modules/agent_run/api/dto.py`
- Create: `control_plane/app/modules/agent_run/api/runtime.py`
- Create: `control_plane/app/modules/agent_run/api/routes.py`
- Create: `control_plane/app/modules/agent_run/api/__init__.py`
- Create: `control_plane/app/bootstrap/sandbox_controller.py`

**Red:** Test OpenAPI route shapes and live HTTP behavior:

- all eight operations map below `/api/v1/internal/sandbox`;
- Bearer workload identity is mandatory and unauthorized/unknown identities
  fail closed;
- camelCase DTOs contain no physical/provider fields;
- writes require `Idempotency-Key`, and existing-materialization writes require
  `If-Match`;
- GET/mutation responses carry strong ETags;
- canonical denials are RFC 9457 Problem Details with `code` and no adapter
  exception text;
- replay returns the exact sealed response;
- main `create_app()` does not contain private Sandbox routes.

Run:

```powershell
uv run pytest tests/agent_run/test_api.py -q
```

Expected failure: private app/routes do not exist.

**Green:** Implement Pydantic API DTOs, identity verifier port/runtime,
route-to-domain mapping, Problem handlers and the separate app factory. Default
runtime readiness/protected calls fail closed unless explicitly configured;
tests inject a fake verifier and real controller.

**Refactor/verify:** Keep authentication material out of settings models,
OpenAPI, logs and Audit. No route is mounted in the browser app.

## Task 9: Prove real PostgreSQL and HTTP end-to-end behavior

**Files:**

- Create: `tests/agent_run/test_e2e.py`
- Modify as required: `tests/agent_run/conftest.py`

**Red:** Write full HTTP journeys using the migrated dedicated PostgreSQL role:

1. authorized provision -> READY -> status -> finalize -> released capacity;
2. exact provision replay returns the same handle without another generation;
3. concurrent HTTP provision creates one generation;
4. stale generation attack is denied and audited;
5. malicious repository/boundary request is denied and audited;
6. timeout/cancel plus reconciliation completes cleanup;
7. no credential, fence plaintext or provider/physical term appears in stored
   Audit or logical schema.

Run:

```powershell
uv run pytest tests/agent_run/test_e2e.py -q
```

Expected failure: at least one cross-layer contract is incomplete.

**Green:** Wire only missing cross-layer pieces. Do not weaken focused contracts
to make E2E green.

**Refactor/verify:** Re-run all `tests/agent_run` with
`REQUIRE_INTEGRATION_DB=1`.

## Task 10: Version and verify both OpenAPI artifacts

**Files:**

- Modify: `control_plane/app/__init__.py`
- Modify: `scripts/export_openapi.py`
- Modify: `openapi.json`
- Create: `sandbox-openapi.json`
- Create: `tests/agent_run/test_openapi_artifact.py`
- Modify: CI workflow only if the current export check is not already invoked
  through the existing gate.

**Red:** Assert both schemas use version `0.9.0`, are deterministic, the private
artifact contains exactly the Controller paths/security/headers, the main
artifact excludes them and neither schema contains forbidden infrastructure or
secret fields. Run:

```powershell
uv run pytest tests/agent_run/test_openapi_artifact.py -q
uv run python scripts/export_openapi.py --check
```

Expected failure: version/artifact mismatch.

**Green:** Bump the package/API version to `0.9.0`; extend the exporter to
render/check `openapi.json` and `sandbox-openapi.json`; regenerate both.

**Refactor/verify:** Check stable sorting and no timestamp/environment-specific
content.

## Task 11: Run scoped gates and commit implementation checkpoints

**Files:** all changed implementation/test/config/artifact files.

Run, fixing product code rather than suppressing failures:

```powershell
uv run ruff format .
uv run ruff check .
uv run mypy .
uv run lint-imports
uv run alembic upgrade heads
uv run pytest tests/agent_run -v
uv run python scripts/export_openapi.py --check
git diff --check
```

Commit coherent checkpoints using Conventional Commits. Never commit generated
caches, coverage, local secret material or database data.

## Task 12: Full verification, two-axis review and branch handoff

Run the complete gates on the dedicated database:

```powershell
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run lint-imports
uv run alembic upgrade heads
uv run pytest -v
uv run python scripts/export_openapi.py --check
```

Then perform parallel Standards and Spec reviews from the V0.9 design/baseline,
plus a general implementation review. Resolve every critical/important finding
and repeat affected focused tests and complete gates.

Before any completion claim, capture fresh:

- `git status --short --branch`
- `git log --oneline --decorate -n 12`
- current HEAD and baseline ancestry;
- migration heads;
- exact full gate output;
- absence of forbidden secrets/provider/physical vocabulary in domain/API/DB/
  Audit artifacts.

If and only if the code slice is independently closed, all gates are green and
review findings are resolved, push
`codex/backend-v09-sandboxed-code-execution`. Do not merge main, tag, delete the
branch/worktree or claim deployment/release acceptance.
