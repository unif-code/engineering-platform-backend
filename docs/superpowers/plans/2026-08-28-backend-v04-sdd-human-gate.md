# V0.4 SDD & Human Gate Backend Implementation Plan

> **For Codex:** Execute test-first in this isolated worktree. Do not activate V0.5 delivery routes. Do
> not update `docs/superpowers/progress/current.md` unless the user sends `【同步进度】`.

**Goal:** Publish the backend contract needed for the V0.4 Route/SDD/WorkItem Assignment/Human Gate
journey while preserving V0.3 behavior and Source Control ownership.

**Architecture:** Keep a single deep `requirement` module. Persist frozen Route/Artifact/Assignment/Gate
facts in its schema, consume Identity/Workspace/Authorization/Source Control only through package-root
Facades, and expose a narrow versioned HTTP interface. Production dependencies fail closed.

**Design:**
`docs/superpowers/specs/2026-08-28-backend-v04-sdd-human-gate-design.md`

## Task 1: Freeze the V0.4 persistence and domain model

**Files:**

- Create: `migrations/requirement/0005_requirement_sdd_human_gate.py`
- Modify: `control_plane/app/modules/requirement/domain/models.py`
- Modify: `control_plane/app/modules/requirement/domain/transitions.py`
- Modify: `control_plane/app/modules/requirement/ports/runtime.py`
- Modify: `control_plane/app/modules/requirement/ports/repository.py`
- Modify: `control_plane/app/modules/requirement/adapters/runtime.py`
- Modify: `control_plane/app/modules/requirement/adapters/sqlalchemy.py`
- Modify: `tests/requirement/test_migration.py`
- Modify: `tests/requirement/test_migration_lifecycle.py`
- Modify: `tests/requirement/test_domain.py`

1. RED: assert the new Route payload, Artifact version and Assignment-history tables/columns, constraints,
   deterministic V0.3 backfill, and minimum `requirement_rw` grants.
2. RED: specify Route v2 canonical hashes and WorkItem readiness requiring Requirement `READY`.
3. GREEN: implement migration, immutable domain DTOs, repository operations and SQL adapter.
4. Verify migration upgrade/downgrade, Alembic heads and focused domain tests.
5. Commit: `feat(requirement): persist v0.4 planning facts`.

## Task 2: Create immutable SDD Artifact versions

**Files:**

- Create: `control_plane/app/modules/requirement/application/artifacts.py`
- Create: `control_plane/app/modules/requirement/adapters/artifacts.py`
- Modify: `control_plane/app/modules/requirement/application/dependencies.py`
- Modify: `control_plane/app/modules/requirement/application/common.py`
- Modify: `control_plane/app/modules/requirement/__init__.py`
- Create: `tests/requirement/test_sdd_artifacts.py`

1. RED: cover line-ending normalization, size/empty rejection, server-owned ID/version/hash, immutable
   versions, authorized Requirement ownership, idempotent replay and payload conflict.
2. GREEN: add one Artifact port with SQL production and in-memory test adapters; return immutable result
   DTOs through the Requirement package-root Facade.
3. RED/GREEN: query exact content only under the owning Requirement and reject cross-Requirement IDs.
4. Run focused tests and commit: `feat(requirement): create immutable sdd artifacts`.

## Task 3: Add WorkItems and governed current Assignment

**Files:**

- Modify: `control_plane/app/modules/requirement/application/commands.py`
- Modify: `control_plane/app/modules/requirement/application/queries.py`
- Modify: `control_plane/app/modules/requirement/application/common.py`
- Modify: `control_plane/app/modules/requirement/adapters/assignment.py`
- Modify: `control_plane/app/modules/requirement/{domain,ports,adapters}/...`
- Create: `tests/requirement/test_planning_commands.py`
- Modify: `tests/requirement/test_commands.py`
- Modify: `tests/requirement/test_binding.py`

1. RED: adding a WorkItem is allowed only in `PREPARING`, consumes the frozen Route, increments semantic
   versions/hash, clears a stale current baseline and appends one binding Outbox message atomically.
2. RED: assign/reassign requires a current eligible candidate, WorkItem CAS, reason, idempotency and an
   append-only current Assignment; unavailable dependencies fail closed.
3. RED: binding before Gate approval remains `DRAFT`; approval promotes only assigned+bound items;
   changes requested demotes not-started items and rejection cancels them.
4. GREEN: implement commands, repository operations, eligibility adapter and Audit.
5. Run command/binding/concurrency tests and commit: `feat(requirement): govern work item planning`.

## Task 4: Activate the Gate with production policy and reviewer adapters

**Files:**

- Create: `control_plane/app/modules/requirement/adapters/gates.py`
- Modify: `control_plane/app/modules/requirement/application/commands.py`
- Modify: `control_plane/app/modules/requirement/application/queries.py`
- Modify: `control_plane/app/modules/requirement/application/dependencies.py`
- Modify: `control_plane/app/modules/requirement/application/common.py`
- Modify: `control_plane/app/modules/requirement/__init__.py`
- Modify: `control_plane/app/modules/workspace/__init__.py` only if a narrow Workspace-by-ID Facade is
  required by the adapter.
- Modify: `tests/requirement/test_baseline_gate.py`
- Create: `tests/requirement/test_gate_adapters.py`

1. RED: Gate stores policy code/version/hash and the current Workspace owner Assignment.
2. RED: decision rechecks enabled account, formal membership and
   `requirement.baseline.decide`; current-assignee mismatch and dependency errors fail closed.
3. RED: Gate reassignment supersedes history, validates the candidate, increments Gate revision and
   rejects stale concurrent writers.
4. GREEN: implement the version-1 Workspace-owner policy and composed reviewer guard through public
   module Facades.
5. GREEN: extend Requirement details with current baseline/Gate/Assignment/Decision metadata.
6. Run focused tests and commit: `feat(requirement): activate human baseline gates`.

## Task 5: Publish the V0.4 HTTP and authorization contract

**Files:**

- Modify: `control_plane/app/modules/requirement/api/dto.py`
- Modify: `control_plane/app/modules/requirement/api/routes.py`
- Modify: `control_plane/app/modules/requirement/api/__init__.py`
- Modify: `control_plane/app/bootstrap/app.py`
- Create: `migrations/authorization/0007_authorization_v04_requirement_routes.py`
- Modify: `tests/authorization/test_migration.py`
- Modify: `tests/requirement/test_api.py`
- Replace: `tests/requirement/test_v03_api_activation.py` with V0.4 activation assertions.
- Modify: `tests/test_openapi_export.py`

1. RED: assert the exact V0.4 paths, strict camelCase bodies, security, required Idempotency-Key and
   strong ETag headers, additive detail projection, and V0.5 route exclusion.
2. RED: cover cross-Workspace reads/writes, stale CAS, wrong current assignee, unavailable dependencies,
   safe Problem Details and request-id correlation.
3. GREEN: register `work_item.create`, `work_item.assign`, `requirement.baseline.submit`,
   `requirement.baseline.assign` and `requirement.baseline.decide` at Workspace scope; do not auto-grant
   them to V0.2 Super Admin compatibility.
4. GREEN: assemble the production Artifact, Gate Policy and reviewer adapters and include only foundation
   + planning + baseline routers in `create_app`.
5. Run API/authorization/OpenAPI focused tests and commit:
   `feat(api): expose v0.4 sdd human gate contract`.

## Task 6: End-to-end, version and full quality gates

**Files:**

- Modify: `tests/requirement/test_e2e.py`
- Modify: `tests/test_e2e_access_governance.py`
- Modify: `control_plane/app/__init__.py`
- Modify generated: `openapi.json`
- Modify as required: release/version tests and documentation.

1. RED/GREEN: real PostgreSQL HTTP journey with creator, Workspace owner and second assignee:
   create → branch acknowledgement → Artifact v1 → split/assign → Gate → changes requested → Artifact
   v2 → Gate reassignment → approval → exact WorkItem readiness.
2. Assert Audit correlation, immutable prior facts, replay, stale-write losers, dependency outage and
   cross-Workspace denial.
3. Set version `0.4.0`, export OpenAPI and verify deterministic output.
4. Run:
   - `uv run ruff format --check control_plane migrations tests scripts`
   - `uv run ruff check control_plane migrations tests scripts`
   - `uv run mypy control_plane migrations scripts tests`
   - `uv run lint-imports`
   - `uv run alembic heads`
   - `uv run pytest --basetemp .pytest-basetemp -q`
   - `uv run python scripts/export_openapi.py --check`
   - `git diff --check`
5. Commit: `release(api): prepare v0.4.0 artifact`.

## Task 7: Review and delivery handoff

1. Run fixed-HEAD standards/spec review and address every Critical/Important issue test-first.
2. Re-run the complete gates on the final tree and compare OpenAPI digest.
3. Push the isolated branch and open a PR to backend `main` with exact commands and evidence.
4. Merge only after normal repository gates allow it; then verify the independent `main` push CI and
   image job separately.
5. Create `api-v0.4.0` only when the release workflow can execute and the published `openapi.json` digest
   matches the verified candidate. Do not work around GitHub billing/Actions failures.
6. Keep the branch/worktree until `main` SHA/tree and the Artifact are independently verified.

## Frontend continuation (separate repository plan)

After the official `api-v0.4.0` Artifact exists, create an isolated frontend worktree from its current
`main`, lock the Artifact, generate the client, and write a separate TDD plan for Route/SDD editor,
WorkItem split/assignment, Gate reassignment/decision and detail refresh behavior. Frontend must not hand
write these DTOs or call dormant V0.5 commands.
