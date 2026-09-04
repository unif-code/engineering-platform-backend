# V0.6 Policy Governance and Production Activation

Status: approved scope, implementation pending. User approved the Requirement policy lifecycle and one-time TOTP publication extension on 2026-09-04. This supplement closes the production activation gaps in `2026-08-31-backend-v06-artifact-acceptance-design.md`; it does not restart the completed domain slice.

## Baseline and authority

- Worktree: `engineering-platform-backend-v06`, branch `codex/backend-v06-artifact-acceptance`.
- Supplement baseline: `1b54f340ba9e84c2e13a0fb70fb221a70c6e9436`, on merged V0.5 main `a15c9a1ee067efad71815562d4c9a160f5824b3f`.
- Local integration target (2026-09-04): main `12dc622` already contains V0.8/V0.9/V0.10. Preserve those modules and public/private contract version `0.10.0`; the standalone V0.6 `0.6.0` version below is historical. A no-DDL `0011_sc_delivery_join` joins the unchanged V0.6 and V0.10 Source Control migration heads. Local integration is not consolidated regression or release acceptance.
- Architecture owners: docs repository `architecture/02-requirement-workflow.md`, `05-source-control-delivery.md`, `10-configuration-governance.md`. Their module ownership and security floors remain binding.
- Configuration coordinates lifecycle; Requirement owns Gate business meaning and all Gate policy persistence; Identity owns sessions, TOTP and consumed authentication facts; Authorization owns grants/fences; Organization owns reporting relationships; Source Control owns provider observations/effects. Cross-module calls use package-root Facades/Ports, never foreign tables or foreign write transactions.

## Scope

Complete typed Requirement Gate policies, their usable Configuration lifecycle, exact one-use administrative reauthentication, real policy/reviewer qualification adapters, production API/worker wiring, immutable delivery freeze validation, authorization registration, version/OpenAPI, and production PostgreSQL/HTTP verification. Latest user authority (2026-09-04) is LOCAL ONLY: implementers deliver scoped local commits for fixed-SHA independent review; root is authorized to integrate reviewed V0.6 into local main and safely clean its merged branch/worktree. Implementers do not merge, clean or operate other worktrees. Earlier push/PR/remote-CI/main-push-CI instructions are historical plans awaiting separate explicit authority, not current actions. Keep existing required CI configuration enabled; no tag or deployment authority is added.

Excluded: frontend, Agent/Chat/Model/Sandbox, binary artifact storage/scanning, infrastructure/deployment/backup/DR/load engineering, tags/releases, and other versions' worktrees. Do not modify `docs/superpowers/progress/current.md` without `【同步进度】`.

## Typed Gate policy

The sole new namespace is `requirement.gate`, schema revision 1, scope `PLATFORM`. Do not add Workspace overrides, arbitrary namespaces or a generic capability registry in this slice.

| Key | Type/default | Permitted values | Effect |
| --- | --- | --- | --- |
| `acceptance.additional_required_capabilities` | unique sorted string tuple / empty | only `code.change` | NEW_OBJECT |
| `formal_review.additional_required_capabilities` | unique sorted string tuple / empty | only `code.change` | NEW_OBJECT |
| `draft_archive_after_days` | strict positive integer / 30 | whole days, must be safely representable by the existing date/time implementation | NEXT_SCHEDULE |

The two capability fields are meaningful, only-strengthening requirements. Requirement's typed schema owns this small explicit allowlist; it does not claim that a general Authorization capability registry exists. `code.change` must remain an actual workspace-scoped execution permission in the published route contract. Unknown keys/values, duplicate capability entries, wrong types/scope/schema, or attempts to replace the required base capability are rejected. The third field governs this owner's draft lifecycle: the 30-day default and NEXT_SCHEDULE behavior come from owner architecture 10 and `architecture/appendix-parameters.md` (Draft automatic archival). It is not inherited from Identity, hard-coded in a worker or a new backup/retention feature.

Complete resolved snapshots also record immutable system constraints:

- Acceptance default reviewer is the Requirement creator, never the Workspace owner by convenience.
- Formal Review defaults: ordinary Member owner to direct Leader; effective Leader owner to self. Unsupported/ambiguous/missing/invalid reporting context fails closed.
- Mandatory capabilities remain `requirement.acceptance.decide` and `merge_request.review`; configured capabilities are ANDed with these, never substituted.
- Current assignment, effective human account, current Workspace membership, correct Scope, and exact subject/version/hash checks cannot be disabled by policy.
- A new Gate freezes full effective values, namespace/scope/schema/version/content hash and resolution evidence. Reassignment and Decision use that frozen policy but re-evaluate current identity/membership/grants. Publishing a later policy never silently rewrites an existing Gate.

Ordinary Acceptance/Formal Review reassignment is part of this existing Assignment contract (architecture 02:113 and 05:115). Its executable owner command must exist, not only historical storage: the immutable default reviewer can choose a currently qualified candidate before a final Decision; a selected current reviewer cannot delegate again unless also the default. The default actor must remain a current initialized enabled human/Workspace member, but cannot be required to hold the assignee's Decision capability, because a creator lacking Acceptance qualification must still be able to delegate to a qualified candidate. The owner command validates the current subject/selection/head, OPEN Gate/current Assignment and strong expected revision, freezes the same policy, appends a new Assignment revision and audit, supersedes only the prior Assignment and advances Gate revision atomically with its idempotent response. Old assignments and completed Decisions are never rewritten. This does not add a generic administrator override or change V0.4 baseline Gate semantics.

Absence of a Decision-capability requirement for the default actor does not waive action authorization: architecture 01 requires Capability + Scope + Assignment independently. Ordinary delivery reassignment requires the actor's explicit `requirement.delivery_gate.assign` Grant at the exact WORKSPACE Scope, in addition to the immutable default-reviewer identity and live account/membership checks. The candidate separately requires the complete frozen Decision qualifications. Register this finite business capability explicitly in V0.6; do not reuse `requirement.baseline.assign`, automatically grant it from ownership, or add it to SuperAdmin's automatic platform capabilities.

## Owner lifecycle and initialization

Reuse existing Configuration lifecycle semantics and public HTTP contracts, replacing Identity-only dispatch with an explicit allowlisted namespace-to-owner runtime registry. Each registered runtime owns its Engine, transaction, idempotency and owner Facade. Configuration must not lend its connection to another owner's write operations. Preserve Identity policy behavior and all existing endpoints.

`GET /api/v1/admin/policies` takes optional `namespace`, default `identity`, preserving its single-catalog/single-active response; `namespace=requirement.gate` returns this owner only. Unknown namespaces fail closed. The archival CLI dispatches each registered owner independently. For Requirement it reads the current typed `draft_archive_after_days` and policy version, uses the existing meaningful-activity and conditional archival rules, and commits Requirement draft transition/outbox/audit in the Requirement transaction. Archive failure cannot change active policy; one owner's unavailable snapshot cannot cause guessed defaults for another.

Requirement owns typed catalog, draft create/read/update/validation/preview/archive, active/version/history reads, publish and rollback. Draft ownership/revision/hash/base/dependencies must be checked. Publication atomically writes a new immutable version, active pointer, domain outbox, audit and idempotent response in one Requirement transaction. Rollback is explicitly a separate draft-only command: `POLICY_ROLLBACK` consumes fresh exact-bound authentication and returns a new draft from historical content while leaving the active pointer unchanged. The draft must then be validated, previewed and separately published with fresh `POLICY_PUBLISH` authentication to create a higher version. Never auto-publish rollback or move the pointer back to an old version. The rollback binding uses the server-generated candidate draft ID/revision and historical content hash; its receipt cannot authorize publication. Draft, unique receipt reference, audit and idempotent rollback response commit atomically in Requirement; replay returns the same draft without consuming again.

Fresh-install initialization uses one explicit, audited `SYSTEM_SEED` migration with the same immutable-version/active-pointer pattern as the existing Identity policy seed. It is initialization, not a substitute for the complete lifecycle. Application startup, readiness and request paths never publish defaults or repair missing pointers. Missing/corrupt/unsupported snapshots fail closed. Runtime roles cannot rewrite immutable history. No generic take-over/rebase/promotion features are added by this supplement.

## One-time exact publication authentication

Identity exposes an internal public runtime operation conceptually named `verify_and_consume_policy_reauth(raw_session, totp_code, binding, attempt_id) -> ConsumedReauthReceipt`. It owns an Identity Engine; callers cannot supply a Requirement Connection. No new bearer credential or public HTTP receipt endpoint is added.

The server constructs a truly immutable binding containing actor, operation (`POLICY_PUBLISH` or `POLICY_ROLLBACK`), namespace, scope, draft ID/revision, content hash, schema revision, base active version, canonical dependency versions, command attempt and request fingerprint. Identity validates a current FULL session and derives its stable non-secret session reference itself; arbitrary client-supplied session IDs are not trusted. Receipt/binding collections use immutable representations, not mutable dictionaries hidden in a frozen dataclass.

In an independent Identity transaction, validate the session/account/current SuperAdmin status, apply existing challenge failure limits and TOTP step CAS, persist the exact consumed binding and audit, and commit before returning a receipt. Persist security failure counters without accidentally rolling them back merely because the API raises an error. Never persist or log raw session/TOTP/secret material. Reuse the existing five-minute freshness window, with a final expiry check before owner publication. Unknown Identity commit result fails closed and returns no usable receipt.

Requirement publication command order (rollback uses the same claim/check/consume/final-check discipline but atomically creates a draft and response, never a published version or pointer change):

1. Claim its own idempotency command; replay an already committed matching response before consuming new TOTP.
2. Lock its active pointer and draft; validate owner/revision/hash/schema/base/validation/preview/dependencies and current `platform.configuration.manage` authorization.
3. Construct the binding and call Identity's independent consume operation.
4. Verify returned receipt matches this binding and session/actor, is consumed and unexpired; recheck publication conditions and current authorization/session at the documented decision point.
5. In the same Requirement transaction, publish with a unique receipt reference, outbox, audit, pointer and idempotent response.

If Identity consumption succeeds but Requirement fails, consumption remains permanent. No compensation, reactivation, cached-receipt retry, or automatic publication. A new execution needs a fresh TOTP; a committed successful request replay still returns its original response. Same-key concurrency is serialized by Requirement idempotency; different keys with the same TOTP are blocked by Identity CAS. A unique owner receipt reference additionally prevents double publication.

Authorization is judged at an explicit final read, with account/session/principal version and fence drift checks. Separate owner transactions are not represented as a global atomic transaction: a revocation after the final authorization read cannot be claimed to be locked through Requirement commit.

## Real routing and qualification

Add a narrow Organization package-root `reporting_context(account_id)` read that validates and returns the relevant Member/Leader relationship facts and canonical hash. Do not invent an organization revision or load unrelated employees to resolve one owner's reviewer.

Authorization owns one reusable current-actor qualification runtime for Requirement and Source Control; bootstrap only constructs and injects it. It holds its own Authorization Engine and reads Identity/Workspace through public Facades and narrow facts Ports. Existing SC `CurrentActorEligibilityAdapter` and the new Requirement delivery guard map the public snapshot into their domain contracts without duplicating the underlying reads. Capture real account version, Workspace version, member source/computed time, principal version/fence/dirty state and effective matching grants, plus checked time and canonical evidence hash. Reject missing/dirty/unavailable data, wrong workspace or scope and version drift; compare owner versions/fence before and after reads. Do not use hard-coded `membership_version=1`/`scope_version=1` as evidence. The existing HTTP-session `DecisionDependencies` is not this actor-facts interface and retains its separate contract.

Requirement's policy Facade exposes resolved Acceptance and Formal Review snapshots. Source Control consumes it through a Port and freezes policy plus Organization evidence. Evaluate the actual requesting merger, including independently authorized operators, at admission, queued provider dispatch and reconciliation; owner identity is not a substitute. Preserve V0.5 Binding/Integration actor checks. Do not weaken existing default/current assignment and reassignment semantics.

## Activation contract

- Default `create_app()` exposes all V0.6 routes and composes real dependencies. Remove the temporary explicit-router-only mode; normal detail/list serialization supports every V0.6 state while retaining V0.5 fields.
- Public Source Control relay/process/reconcile batches and the real CLI include Evidence/Formal lanes. Allocate a strict total budget fairly to every lane; minimum limits become 4/5/3 respectively. This minor-version contract change is documented and tested. Per-lane errors/retries remain isolated and reported; do not leave a dormant version-specific batch API as an alternative implementation.
- Freeze commands validate the locked current Requirement delivery set through the same shared `required_work_item_set_hash` rules as `get_requirement_delivery_snapshot`. Current read model and immutable freeze remain distinct responsibilities. A bad stored set hash fails closed before creating outbox or freeze facts.
- Add Authorization migration after `0008_auth_v05_routes`, preserving `work_item.execute` and `merge_request.merge`, and registering actual V0.6 routes/capabilities. Preserve the existing multi-owner migration branches and append-only role grants.
- Publish code/OpenAPI version `0.6.0`; regenerate the contract, do not hand-edit generated OpenAPI.

## Acceptance evidence

Execution scheduling amendment (2026-09-04): the user requested stopping repeated local bulk regressions and consolidating them after version integration. Keep minimal feature/TDD/fix verification during implementation and existing required PR CI enabled; do not represent an interrupted/local-unrun suite as passing. This changes when local regression runs, not the required security or release evidence.

Use TDD and real PostgreSQL owner runtime roles. Tests must prove exact reauthentication binding/replay/concurrency/expiry/failure semantics; full owner policy lifecycle and Identity regression; frozen policy behavior and revocation; real default API/CLI composition; V0.5 human delivery through V0.6 Acceptance/Formal merge; independent merge operator; unknown reconciliation and invalid hash rejection; schema fresh install and V0.5 upgrade.

Fake only external GitLab effects for the production end-to-end chain, not module persistence or reviewer policy. Verify actual exact SHAs/evidence. The full evidence inventory includes Ruff format/check, mypy, import contracts, migrations, full pytest with required PostgreSQL integration, OpenAPI consistency and fixed-SHA independent review. Local bulk verification is deferred under the scheduling amendment; PR CI and independent main CI are historical future-delivery requirements, not currently authorized executions. Report these exclusions explicitly. Historical test counts or focused-only checks are not whole-version completion evidence.
