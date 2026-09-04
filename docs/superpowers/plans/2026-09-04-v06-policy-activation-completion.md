# V0.6 Policy Governance and Activation Implementation Plan

> **For agentic workers:** REQUIRED: use superpowers:subagent-driven-development in this session. Steps use TDD; one implementation writer at a time, independent task reviews, then whole-branch review.

**Goal:** Close the approved policy/security and production activation gaps, deliver a locally committed V0.6 checkpoint for fixed-SHA review, then let root integrate it into local main and safely clean the merged V0.6 branch/worktree. Remote delivery remains outside current authority.

**Spec:** `docs/superpowers/specs/2026-09-04-v06-policy-activation-completion-design.md`.

**Architecture:** Typed Gate policy data stays in Requirement; Configuration dispatches owner runtimes; Identity independently consumes exact one-use authentication. Public Facades and Ports compose production qualification, API and worker effects.

**Tech stack:** Existing Python/FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL 18/pytest/uv stack; no new framework.

## Global Constraints

- Latest authority amendment (2026-09-04): LOCAL ONLY. The user's continuation authorizes root to integrate reviewed V0.6 into local main and safely clean the merged V0.6 branch/worktree. Implementers only implement, minimally verify and commit in their scoped worktree; they do not merge, clean or operate other worktrees. Push, PR creation, remote CI execution, tags and deployment are not authorized. Earlier remote-delivery/main-push-CI instructions are historical plans awaiting separate explicit authority. Keep existing required CI configuration enabled; this does not authorize remote workflow execution.

- User execution amendment (2026-09-04): stop repeated local batch/whole-suite regressions and defer consolidated regression until version integration. Existing PR required CI remains enabled. During implementation run only the smallest necessary feature/TDD/fix checks; do not restart affected-module bulk suites because a task step below originally requested them. Record interrupted runs as INTERRUPTED, never PASS. This amendment overrides the earlier per-task bulk-regression scheduling, not security/owner contracts or truthful evidence reporting.

- Work only in `D:/tongyi/code/engineering-platform/.worktrees/engineering-platform-backend-v06` on `codex/backend-v06-artifact-acceptance`; preserve existing V0.5 and V0.6 implementation. No other worktree, frontend or docs-owner writes.
- Cross-module calls use package-root Facades/Ports, never foreign tables or foreign write transactions.
- Configuration coordinates lifecycle; Requirement owns Gate policy meaning and persistence; Identity owns sessions/TOTP/consumed authentication facts.
- No secrets in code, logs, test reports or commits. No `.env` contents. No service reset, deployment, tag or release. Root alone owns authorized V0.6 local-main integration and safe merged-branch/worktree cleanup; no remote action is authorized.
- Do not modify `docs/superpowers/progress/current.md` without `【同步进度】`.
- Missing/corrupt/unsupported policy or unavailable/currently invalid qualifications fail closed. UNKNOWN external effects are never inferred successful.
- Acceptance defaults to creator; Formal Review defaults Member to direct Leader and Leader to self. Canonical permissions are `requirement.acceptance.decide`, `merge_request.review`, `merge_request.merge`; additional capabilities only strengthen frozen policy requirements.
- Preserve V0.5 independent merge actor, second WorkItem start, topic/revision-specific delivery context; preserve V0.6 exact-head effects, historical supersede, replay fingerprints and NO_DELIVERY_COMMIT retry behavior.
- Do not add compatibility shims, parallel implementations or fake production version evidence. The current snapshot query and immutable delivery freeze remain distinct, sharing integrity validation.
- Record actual RED/GREEN commands/results, focused results, files and self-review in each task report. A fixture skip is not a PostgreSQL pass. Local bulk regression is deferred by the user amendment above; existing required CI remains the integration gate.
- Each implementer may commit its scoped changes but must not push/PR/merge or spawn subagents. Root commissions independent review.

### Task 1: Identity-owned exact one-use policy reauthentication

**Files:**
- Create focused `control_plane/app/modules/identity/domain/policy_reauthentication.py`, `application/policy_reauthentication.py`, `adapters/policy_reauthentication.py` and a public Port/DTO only where needed by existing layer rules.
- Modify Identity `__init__.py`, existing session/SuperAdmin repository interfaces only for narrow reusable seams; add the next Identity migration after current head.
- Add `tests/identity/test_policy_reauthentication.py`, real-DB tests alongside it, and update affected migration/lifecycle head expectations.

**Behavior:**
Expose the internal runtime operation `verify_and_consume_policy_reauth(raw_session, totp_code, binding, attempt_id) -> ConsumedReauthReceipt`. Runtime owns Identity Engine and dependencies. Binding is immutable: actor, operation POLICY_PUBLISH/POLICY_ROLLBACK, namespace, scope, draft ID/revision, content hash, schema revision, base version, canonical dependency versions, command attempt/fingerprint; Identity derives the stable non-secret session reference from current FULL session. Receipt is internal, not an HTTP bearer token.

Reuse the existing challenge/TOTP CAS and failure-rate machinery, not a second TOTP algorithm. Independently commit exact consumed binding and audit before returning. Preserve failed attempt counters; no raw session/TOTP storage. Five-minute TTL, immutable consumed facts and unambiguous commit success; no receipt on unknown/error. Keep existing Identity policy publication working.

**Steps:**
1. Read spec reauthentication section, Identity AGENTS, existing `verify_admin_totp`, `sessions.py`, `super_admin.py`, policy command runtime and fixture migrations; identify safe public session resolution and repository extension.
2. RED: immutable binding/canonical hash; current FULL session actor mismatch/revoked/session stage; successful consumed receipt fields; exact-binding mismatch; same TOTP concurrent/different attempt rejects; failure counters persist; expired receipt metadata; unknown commit returns no receipt. Use real owner-role DB for transaction/CAS facts, doubles only for clock/secret manager and controlled failure seams.
3. GREEN: implement minimal immutable models, persistence/migration and runtime; return only after successful transaction exit; errors sanitized.
4. Verify public imports/owner grants and old Identity policy/session/TOTP suites. Run focused new tests, affected Identity suite, Ruff/mypy/import checks. Record true DB pass/skip separately.
5. Self-review and commit `feat(identity): bind one-time policy reauthentication to exact commands`.

**Not in this task:** Configuration routing or Requirement policy consumer. Task 2 owns them and tests consume-success/owner-failure behavior.

### Task 2: Requirement policy lifecycle and Configuration owner dispatch

**Files:**
- Create Requirement typed policy domain/catalog, application lifecycle, repository Port and SQLAlchemy adapter in `control_plane/app/modules/requirement/{domain,application,ports,adapters}/` using focused policy files; export owner runtime through `requirement/__init__.py`.
- Add Requirement migration after `0007` for drafts/immutable versions/active pointer/receipt reference and any required policy outbox/idempotency structures; explicit audited SYSTEM_SEED.
- Modify `configuration/{__init__.py,ports/policy_owner.py,application/dependencies.py,api/routes.py,adapters/identity.py}` and add a Requirement adapter/runtime registry contract as necessary; bootstrap registers exact namespace runtimes.
- Modify `control_plane/tools/archive_drafts.py` to dispatch owner-held transactions and add matching CLI/Requirement archival tests.
- Add `tests/requirement/test_gate_policy.py`, `test_gate_policy_persistence.py`, `tests/configuration/test_requirement_policy_lifecycle.py`; update affected role/head fixtures.

**Behavior:**
Namespace `requirement.gate`, schema revision 1, PLATFORM only. Keys `acceptance.additional_required_capabilities` and `formal_review.additional_required_capabilities` default empty; unique sorted immutable set values permit only `code.change`. Third key `draft_archive_after_days` is a strict positive whole-day integer, default 30, NEXT_SCHEDULE, rejecting values unrepresentable in the existing date/time implementation. This is the owner architecture 10/parameter appendix's required draft lifecycle setting, not Identity's policy. Reject unknown/duplicate/wrong type/scope/schema. Complete runtime snapshots include mandatory base capabilities and fixed default routes. Do not build a generic capability registry.

Reuse generic lifecycle application behavior where it has independent responsibility; do not copy Identity lifecycle logic wholesale. Make namespace dispatch explicit and route every read/write/idempotency operation through the correct owner engine. Preserve identity namespace endpoints/behavior. The single-catalog `GET /api/v1/admin/policies` gains optional `namespace=identity`; the Requirement query returns only its own catalog/active snapshot and unknown names fail closed. Support catalog, draft create/read/update/validate/preview/archive, active/history/version, publish and rollback-to-new-version. Owner checks: current SuperAdmin and `platform.configuration.manage` PLATFORM, draft owner/revision/hash/base/validation/preview/dependencies. No startup/request auto-seed or fallback. Archival CLI dispatches each owner; Requirement uses its own current versioned archive interval, conditionally archives by draft ID/state/revision/owner/activity and atomically writes outbox/audit, never touching active pointer or borrowing Identity policy.

Publication uses Task 1 consumed receipt and its documented independent transaction. Claim idempotency first, lock/check draft and active pointer, generate exact binding, consume, revalidate binding/expiry/current session+authorization and owner conditions; atomically write version/pointer/outbox/audit/unique receipt/idempotent response. Owner failure burns authentication; never auto-reuse proof. Successful replay precedes reauth. Preserve two-command rollback: POLICY_ROLLBACK consumes fresh authentication bound to the server-generated candidate draft ID/revision/content and creates/returns only that draft, unique receipt reference, audit and idempotent response. Active pointer stays unchanged. Validation/preview and a separate POLICY_PUBLISH with fresh authentication are mandatory before a higher version activates; rollback receipt is never reusable for publication.

**Steps:**
1. RED typed catalog/security floors and draft lifecycle, missing/corrupt/unsupported active failure, version immutability; Requirement archive interval changes affect the next schedule, preview/view does not refresh activity, concurrent edit skips archive, repeat archive is idempotent, invalid/missing policy never falls back.
2. Implement Requirement owner storage/schema initialization and public adapter; reuse existing generic drafts/validation/preview primitives and transaction abstractions without cross-owner imports.
3. RED production Configuration HTTP lifecycle with real Requirement/Identity roles and explicit namespace registry; unknown namespace rejected; Identity namespace regression; wrong owner/ETag/hash/base/missing reason/wrong scope denied.
4. RED and GREEN real cross-owner publish/rollback tests: committed response replay without consuming again, same-key races, different-key same TOTP, stale publication binding, owner transaction failure after consumed receipt (Identity fact remains, no Requirement publish), expiry, authorization/fence drift, exact receipt uniqueness. Rollback must leave active unchanged, replay the identical draft, and require separate validation/preview/fresh publication authentication. Failure responses do not disclose authentication material.
5. Run affected Requirement policy/Configuration/Identity policy tests, migration upgrade/grants tests, Ruff/mypy/import checks. Self-review and commit `feat(configuration): activate requirement-owned gate policy lifecycle`.

**Not in this task:** Gate decision adapters or broad API activation; Task 3 consumes the new policy Facade. The registry is only the necessary two-owner dispatch, not an extensibility framework.

### Task 3: Production Gate policy, reviewer routing and qualification evidence

**Files:**
- Add Organization `reporting_context` public DTO/query/Port repository support in existing module layers and `__init__.py`.
- Add Authorization `domain/qualification.py`, `ports/qualification.py`, `application/qualification.py`, `adapters/qualification.py`, dependencies and package-root exports for one owner-engine `ActorQualificationRuntime.evaluate(actor_id, workspace_id, required_capabilities)` and immutable fact snapshot. Keep existing HTTP `DecisionDependencies` separate.
- Modify Requirement `ports/runtime.py`, add `adapters/delivery_gates.py`, update acceptance/formal application callers and policy Facade; Source Control formal routing Port/adapter consumers. V0.4 `adapters/gates.py` baseline semantics are not changed by this task.
- Add the missing ordinary delivery-Gate reassignment owner command in focused `application/delivery_assignments.py`, public Facade/result DTO and narrow repository mutations using the existing delivery assignment history schema. Task 4 exposes its HTTP contract; do not route through the V0.4 baseline command.
- Modify existing `source_control/adapters/eligibility.py` to map the public Authorization qualification snapshot into `BindingEligibility`, removing duplicated reads; bootstrap only constructs/injects the shared runtime into both consumers.
- Add `tests/organization/test_reporting_context.py`, Requirement Gate runtime tests and SC real-routing/actor tests; update existing doubles to real contract names.

**Behavior:**
Narrow relevant reporting context with canonical facts hash, no fabricated organization revision. Acceptance creator; ordinary owner direct Leader; effective Leader self; missing/ambiguous/unsupported/invalid fails closed. Snapshot full policy and organization evidence. Frozen Gate policy remains effective for reassignment/Decision after active policy changes; check live account/membership/scope/all required capabilities/current assignment every time.

Ordinary delivery reassignment is required by architecture 02/05: only the immutable default reviewer may reassign before a final Decision; a selected current reviewer cannot further delegate unless also the default. The default actor must still be a current initialized enabled human/Workspace member, but need not have the assignee's Decision capability (an unqualified creator must be able to choose a qualified Acceptance candidate). The candidate must satisfy the complete frozen Gate policy and current qualifications. Validate current Requirement/Gate/Assignment and exact subject/selection/head, OPEN/no final Decision and expected Gate revision under owner locks; exact idempotency, old Assignment supersession, new monotonically revised Assignment, Gate revision CAS and audit commit together. Preserve frozen resolution/policy and old assignment history. This clarification does not introduce an arbitrary administrator override or alter the baseline Gate command.

The default actor still needs the independent exact-WORKSPACE `requirement.delivery_gate.assign` Grant (architecture 01 Capability + Scope + Assignment floor). Task 3 enforces it in the owner command through qualification; Task 4 explicitly registers/exposes it without granting it automatically to owners or SuperAdmin. A missing assign Grant denies, while assign Grant without Decision Grant permits an otherwise valid default actor to select a qualified candidate.

Read only public Facades. Evidence records actual account version, Workspace version/member source and computed time, Authorization principal version/fences/dirty state and matching grants, checked time/hash. Read versions/fence before and after to detect drift. Replace misleading membership/scope version fields with accurate semantics; no fake constant versions. Authorization decision point is the final consistent read, not a claimed cross-owner commit lock.

**Steps:**
1. RED routing fixtures: creator differs Workspace owner, ordinary Member has direct Leader, Leader self, missing relation, unrelated invalid account does not poison an otherwise valid relevant context.
2. GREEN Organization public query and real policy/routing adapters.
3. RED frozen additional `code.change` enforcement on decision/reassign; publishing new policy affects only new gates; missing grants, revoked/disabled/removed member, dirty fence and changing versions deny; evidence hashes match actual observed fields.
4. GREEN reusable qualification evidence and actual Source Control actor checks at admission/dispatch/reconciliation. Preserve independent authorized merger and reject revoked queued actor or unqualified owner.
5. Run affected Organization/Requirement/SC suites and static/import gates, self-review, commit `feat(delivery): resolve governed reviewers from live owner facts`.

### Task 4: Default API, worker lanes and immutable snapshot activation

**Files:**
- Modify `bootstrap/app.py`, `bootstrap/source_control_runtime.py`, Requirement and SC dependencies/public exports, `source_control/application/batches.py`, `application/v06_batches.py`, `tools/source_control_worker.py` and relevant CLI docs.
- Modify Requirement evidence/freeze implementation and shared current delivery-set validation only where required.
- New Authorization migration after `0008_auth_v05_routes`, navigation allowlists/tests and lifecycle head/grant expectations; version source and generated `openapi.json`.
- Update `tests/requirement/test_v06_api.py`, V0.5 activation tests, SC batch/worker/composition tests and snapshot consistency/recovery tests.
- Expose Task 3's ordinary delivery-Gate reassignment Facade through the V0.6 router/DTO with the correct owner authorization, strong ETag/If-Match and idempotency contract; do not reimplement its domain rules in HTTP.

**Behavior:**
Expose all V0.6 default routes using production policy/qualification/repository/Requirement callback adapters. Preserve V0.5 routes/fields/permissions; no default hidden route mode. CLI/public batch relay/process/reconcile includes all lanes with total limits and fair lane quotas; minimum limits are 4/5/3. Fold dormant v06 batch behavior into one implementation; preserve retry/error isolation and summary semantics.

Immutable freeze validates current locked Requirement set/version with existing `required_work_item_set_hash`; bad stored set hash creates no freeze/outbox. Preserve distinct current query vs immutable freeze. Set version `0.6.0`, regenerate OpenAPI through existing script.

**Steps:**
1. RED default app/OpenAPI/routes and ordinary detail/list for AWAITING_ACCEPTANCE/AWAITING_MERGE/COMPLETED; no fake production dependencies.
2. RED real worker entrypoint lane reachability, strict minima/total budgets/fairness/error isolation across binding/integration/webhook/evidence/formal; GREEN integrate production workers.
3. RED current snapshot -> freeze -> SC evidence exact hash/set/version, tampered hash fail-closed; GREEN shared validation within locked transaction.
4. GREEN runtime wiring, new permission migration, preserved navigation, version/export; remove stale dormant/old-version claims in the original V0.6 spec/plan without changing current progress.
5. Run affected API/CLI/composition/transaction tests and static/OpenAPI gates; self-review; commit `feat(delivery): activate v0.6 production API and worker flows`.

### Task 5: Production PostgreSQL end-to-end proof and delivery preparation

**Files:**
- Extend/create `tests/source_control/test_v06_production_e2e.py`, migration/lifecycle upgrade tests and existing failure/recovery scenarios; update test fixtures only where needed for real public composition.
- Add an evidence/runbook document under `docs/` if existing convention calls for one; do not update current progress.

**Behavior and tests:**
1. Start from default production app and actual public workers, real PostgreSQL owner roles, actual Identity/Organization/Workspace/Authorization/Requirement/SC persistence and governed policy. Fake only external GitLab effects and deterministic clock/secret fixture support.
2. Continue V0.5 human WorkItem integration into frozen Evidence, Selection, creator Acceptance, formal review by direct Leader/self and independent authorized merge actor. Assert exact returned SHA/evidence and ordinary GET/list terminal states.
3. Prove two-round rework, duplicate callbacks, unknown reconciliation, multi-WorkItem partial completion, same-head NO_DELIVERY_COMMIT retry, revoked queued actor, stale policy/assignment and corrupted snapshot rejection.
4. Fresh DB all heads and V0.5 -> V0.6 upgrade, historical effect rekey, append-only grants, safe migration down/up where supported. No test skips disguised as passes.
5. Run existing full repository CI-equivalent gates with required PostgreSQL integration: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy .`, `uv run lint-imports`, `uv run alembic upgrade heads` against an explicitly isolated test DB, `uv run pytest -v`, `uv run python scripts/export_openapi.py --check`. Do not mutate the user's development DB just to run migration gates. If unavailable, report precise missing evidence and keep mandatory PostgreSQL 18 CI as a hard merge gate.
6. Fix task-scoped failures through TDD, self-review, checkpoint only the intended local branch. Report tests/current SHA/remaining evidence to root for fixed-SHA independent review. The implementer stops at the local checkpoint; root may integrate reviewed V0.6 into local main and safely clean the merged V0.6 branch/worktree. Earlier push/PR/remote-CI/main-push-CI steps are historical and require separate explicit authority. Preserve existing required CI configuration and report deferred verification without claiming remote or release acceptance.
