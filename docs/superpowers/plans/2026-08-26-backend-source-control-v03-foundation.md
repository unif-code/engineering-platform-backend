# V0.3 Source Control Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 可靠消费 Requirement Repository Binding Outbox，校验当前负责人和 Workspace Repository，按 GitLab `main` 的准确 SHA 创建确定性任务分支，并以 Inbox、Effect Ledger、不可变 Binding、签名 Webhook 与 Reconciliation 收敛重复和未知外部结果。

**Architecture:** 新 `source_control` 深模块拥有仓库授权投影、两类 Inbox、GitLab 外部 Effect 与不可变 Branch Binding。Requirement 只通过包根 Facade 暴露 Outbox relay、Binding Context 和 Ready/Blocked 回调；GitLab Adapter 执行 read-main → create-exact-SHA → read-branch，任何未知结果进入 Reconciliation，Webhook 只触发回查而不直接写领域状态。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy Core、PostgreSQL 18、Alembic、HTTPX、pytest、mypy strict、Ruff、import-linter。

**Spec:** `docs/superpowers/specs/2026-08-26-backend-source-control-v03-foundation-design.md`

## Global Constraints

- 基线固定为 `main@631979b759944b7a95bc3cd380a573fbfc4b6aab`；不得从已 rebase 的旧 PR head 构造重复历史。
- 本批不实现 MR、Merge、Artifact、Acceptance、Chat/Model、前端、GitOps 或真实 GitLab Smoke Test。
- `source_control` 不得直接读写 Requirement、Identity、Workspace、Authorization 私有表；模块间只用包根 Facade。
- 每个本地事务只修改一个模块 schema；GitLab HTTP 与跨模块 Facade 调用均在数据库事务之外。
- Requirement Outbox、Source Control Inbox、Effect、Binding 与回调均使用稳定唯一键和 revision/CAS，不宣称 exactly-once。
- GitLab 创建流程固定为读取 `main` SHA、以准确 commit SHA 创建分支、回读分支验证；不能直接用移动的 `main` ref 形成 Binding。
- 已可能到达 GitLab但无法证明结果的调用必须进入 `UNKNOWN/RECONCILIATION`，不得猜测成功。
- Webhook 只接受 Standard Webhooks HMAC-SHA256 signing token；必须验证 `webhook-id`、`webhook-timestamp`、`webhook-signature`，禁止 `X-Gitlab-Token` fallback。
- Secret 只保存 Reference；PAT、signing token、签名、原始外部 error body 与源码不得进入 DB、日志、Audit、测试快照或 Git。
- 未收到 `【同步进度】`，不得修改 `docs/superpowers/progress/current.md`。
- `openapi.json` 只允许通过 exporter 更新本批已批准的 Requirement blocked reason 枚举；Connector route 不挂主业务 app，并由专门测试证明未进入公开 OpenAPI。
- 每个任务严格执行 RED → GREEN → REFACTOR；不得用 skip、xfail、弱断言、测试专用业务分支或降低门禁通过。

---

## File Map

### Requirement owner changes

- Modify: `control_plane/app/modules/requirement/domain/models.py`
- Modify: `control_plane/app/modules/requirement/domain/transitions.py`
- Modify: `control_plane/app/modules/requirement/domain/__init__.py`
- Modify: `control_plane/app/modules/requirement/ports/repository.py`
- Modify: `control_plane/app/modules/requirement/adapters/sqlalchemy.py`
- Modify: `control_plane/app/modules/requirement/application/commands.py`
- Modify: `control_plane/app/modules/requirement/application/queries.py`
- Modify: `control_plane/app/modules/requirement/application/__init__.py`
- Modify: `control_plane/app/modules/requirement/__init__.py`
- Create: `migrations/requirement/0003_requirement_source_control_relay.py`
- Modify: `tests/requirement/test_migration.py`
- Create: `tests/requirement/test_source_control_relay.py`

### Source Control deep module

- Create: `control_plane/app/modules/source_control/__init__.py`
- Create: `control_plane/app/modules/source_control/domain/{__init__,models,transitions,naming}.py`
- Create: `control_plane/app/modules/source_control/ports/{__init__,repository,runtime,requirement,gitlab}.py`
- Create: `control_plane/app/modules/source_control/application/{__init__,dependencies,commands,relay,reconciliation,webhooks}.py`
- Create: `control_plane/app/modules/source_control/adapters/{__init__,sqlalchemy,requirement,eligibility,gitlab,webhook}.py`
- Create: `control_plane/app/modules/source_control/api/{__init__,webhooks}.py`
- Create: `migrations/source_control/0001_source_control_foundation.py`
- Modify: `alembic.ini`
- Modify: `control_plane/app/shared/db/settings.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `control_plane/app/bootstrap/source_control_connector.py`
- Create: `control_plane/tools/source_control_worker.py`
- Create: `tests/source_control/{__init__,conftest,test_domain,test_migration,test_relay,test_commands,test_gitlab_adapter,test_webhooks,test_reconciliation,test_e2e}.py`
- Modify: `tests/test_contract_guard.py`
- Modify: `tests/test_openapi_export.py`

---

### Task 1: 深化 Requirement Facade，提供可靠 Outbox relay seam

**Files:**

- Modify: `control_plane/app/modules/requirement/domain/models.py`
- Modify: `control_plane/app/modules/requirement/domain/transitions.py`
- Modify: `control_plane/app/modules/requirement/domain/__init__.py`
- Modify: `control_plane/app/modules/requirement/ports/repository.py`
- Modify: `control_plane/app/modules/requirement/adapters/sqlalchemy.py`
- Modify: `control_plane/app/modules/requirement/application/{commands,queries,__init__}.py`
- Modify: `control_plane/app/modules/requirement/__init__.py`
- Create: `migrations/requirement/0003_requirement_source_control_relay.py`
- Create: `tests/requirement/test_source_control_relay.py`
- Modify: `tests/requirement/test_migration.py`

**Interfaces:**

- Produces: frozen `RepositoryBindingRequestMessage` and `RepositoryBindingContext`.
- Produces: `claim_repository_binding_requests`、`acknowledge_repository_binding_request`、`release_repository_binding_request`、`get_repository_binding_context` package-root Facade.
- Extends: `RepositoryBindingBlockedReason` with `OWNER_UNASSIGNED`、`OWNER_INELIGIBLE`、`REPOSITORY_NOT_AUTHORIZED`、`RECONCILIATION_PENDING`.
- Preserves: current Outbox payload `{workItemId, repositoryId}` and existing Ready/Blocked Facade.

- [ ] **Step 1: Write failing relay and context tests**

Add exact observable cases to `tests/requirement/test_source_control_relay.py`:

```python
def test_claim_leases_existing_binding_request_without_publishing(db, dependencies):
    created = create_assigned_requirement(db, dependencies=dependencies)
    now = dependencies.clock.now()
    messages = claim_repository_binding_requests(
        db,
        limit=10,
        available_before=now,
        lease_until=now + timedelta(minutes=1),
        dependencies=dependencies,
    )
    assert [(m.work_item_id, m.repository_id) for m in messages] == [
        (created.work_item.id, created.work_item.repository_id)
    ]
    row = outbox_row(db, messages[0].message_id)
    assert row["state"] == "PENDING"
    assert row["attempts"] == 1
    assert row["available_at"] == now + timedelta(minutes=1)


def test_ack_is_idempotent_and_moves_created_requirement_to_preparing(db, dependencies):
    created = create_assigned_requirement(db, dependencies=dependencies)
    message = claim_one_binding_request(db, dependencies)
    first = acknowledge_repository_binding_request(
        db,
        message_id=message.message_id,
        consumer="SOURCE_CONTROL",
        dependencies=dependencies,
    )
    second = acknowledge_repository_binding_request(
        db,
        message_id=message.message_id,
        consumer="SOURCE_CONTROL",
        dependencies=dependencies,
    )
    assert first.state is RequirementState.PREPARING
    assert second == first
    assert outbox_row(db, message.message_id)["state"] == "PUBLISHED"


def test_context_exposes_current_facts_without_private_rows(db, dependencies):
    created = create_assigned_requirement(db, dependencies=dependencies)
    context = get_repository_binding_context(
        db,
        work_item_id=created.work_item.id,
        dependencies=dependencies,
    )
    assert context.work_item_revision == created.work_item.revision
    assert context.assignment_state is AssignmentState.ASSIGNED
    assert context.human_owner_id == created.work_item.human_owner_id
    assert context.required_capabilities == ("code.change",)
```

Also prove: unavailable messages are not claimed; concurrent claims return disjoint IDs; release writes only an allowlisted code and retry time; same message ack after Requirement already advanced does not regress state; a malformed historical payload fails with `RepositoryBindingMessageInvalid`.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```powershell
uv run pytest tests/requirement/test_source_control_relay.py tests/requirement/test_migration.py -q
```

Expected: collection fails because the four Facade functions and new DTOs do not exist.

- [ ] **Step 3: Add DTOs, errors and repository interface**

Add these exact domain shapes:

```python
class RepositoryBindingRequestMessage(BaseModel):
    model_config = ConfigDict(frozen=True)
    message_id: str
    requirement_id: str
    requirement_version: int
    work_item_id: str
    repository_id: str
    attempts: int


class RepositoryBindingContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    requirement_id: str
    requirement_type: RequirementType
    requirement_title: str
    workspace_id: str
    work_item_id: str
    work_item_revision: int
    repository_id: str
    assignment_state: AssignmentState
    human_owner_id: str | None
    required_capabilities: tuple[str, ...]
```

Extend `RequirementRepository` with:

```python
def claim_binding_requests(
    self,
    *,
    limit: int,
    available_before: datetime,
    lease_until: datetime,
) -> list[Any]: ...


def outbox_by_id(self, message_id: str, *, for_update: bool = False) -> Any: ...
def publish_outbox(self, message_id: str, *, now: datetime) -> Any: ...
def release_outbox(
    self,
    message_id: str,
    *,
    error_code: str,
    available_at: datetime,
) -> Any: ...
def repository_binding_context(self, work_item_id: str) -> Any: ...
```

- [ ] **Step 4: Implement claim/ack/release with SQL locking and migration**

Use `FOR UPDATE SKIP LOCKED` ordered by `(available_at,id)`. Claim increments `attempts` and moves
`available_at` to the lease end while preserving `PENDING|FAILED`. Ack updates only
`state='PUBLISHED', published_at=:now, last_error_code=NULL`; if the owner Requirement is still
`CREATED`, the same Requirement transaction CAS-transitions it to `PREPARING` and writes Audit.

Migration `0003_requirement_source_control_relay.py` must replace the WorkItem repository CHECK with the
four new safe blocked reasons included and restore the exact previous CHECK on downgrade only when no row
uses a new reason. It must not add cross-schema grants.

- [ ] **Step 5: Run focused tests and architecture check**

Run:

```powershell
uv run pytest tests/requirement/test_source_control_relay.py tests/requirement/test_migration.py tests/requirement/test_binding.py -q
uv run lint-imports
```

Expected: all selected tests pass; existing Ready/Blocked recovery remains green; import-linter reports no broken contracts.

- [ ] **Step 6: Commit Requirement seam**

```powershell
git add control_plane/app/modules/requirement migrations/requirement tests/requirement
git commit -m "feat(requirement): expose source control binding relay facade"
```

---

### Task 2: Establish the Source Control deep module and pure domain model

**Files:**

- Create: `control_plane/app/modules/source_control/{__init__,domain/__init__,domain/models,domain/transitions,domain/naming}.py`
- Create: empty package entrypoints under `api/ application/ ports/ adapters/`
- Modify: `pyproject.toml`
- Create: `tests/source_control/{__init__,test_domain}.py`
- Modify: `tests/test_contract_guard.py`

**Interfaces:**

- Produces: `EffectState`、`InboxState`、`WebhookInboxState`、`RepositoryAuthorizationState`、`RequirementCallbackState`.
- Produces: frozen DTOs `WorkspaceRepositoryDto`、`BindingRequestEnvelope`、`SourceControlEffectDto`、`RepositoryBranchBindingDto`、`GitLabWebhookEnvelope`.
- Produces: `build_task_branch_name(...)` and explicit Effect transitions.

- [ ] **Step 1: Write failing state and deterministic naming tests**

```python
@pytest.mark.parametrize(
    ("current", "target"),
    [
        (EffectState.PLANNED, EffectState.IN_FLIGHT),
        (EffectState.IN_FLIGHT, EffectState.SUCCEEDED),
        (EffectState.IN_FLIGHT, EffectState.UNKNOWN),
        (EffectState.UNKNOWN, EffectState.RECONCILIATION),
        (EffectState.RECONCILIATION, EffectState.UNKNOWN),
        (EffectState.RECONCILIATION, EffectState.SUCCEEDED),
        (EffectState.RECONCILIATION, EffectState.BLOCKED),
    ],
)
def test_effect_transition_matrix(current, target):
    assert transition_effect(current, target) is target


def test_branch_name_is_deterministic_and_unicode_safe():
    assert (
        build_task_branch_name(
            requirement_type=RequirementType.FEAT,
            work_item_number=42,
            title="创建 GitLab 分支 / HMAC 验签",
        )
        == "feat/wi-42-创建-gitlab-分支-hmac-验签"
    )


def test_branch_name_rejects_git_ref_forbidden_sequences():
    name = build_task_branch_name(
        requirement_type=RequirementType.FIX,
        work_item_number=7,
        title=".. @{ lock.lock",
    )
    assert ".." not in name
    assert "@{" not in name
    assert not name.endswith(".lock")
```

Also reject illegal reverse transitions such as `SUCCEEDED → IN_FLIGHT`, non-positive numbers, empty repository IDs and mutable Binding DTOs.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest tests/source_control/test_domain.py tests/test_contract_guard.py -q`

Expected: collection fails because `source_control` does not exist; contract guard also detects the unregistered module after the package is created but before `pyproject.toml` is updated.

- [ ] **Step 3: Implement domain model and branch naming**

Use an explicit transition table:

```python
_EFFECT_TRANSITIONS = {
    EffectState.PLANNED: {EffectState.IN_FLIGHT, EffectState.BLOCKED},
    EffectState.IN_FLIGHT: {
        EffectState.SUCCEEDED,
        EffectState.BLOCKED,
        EffectState.UNKNOWN,
    },
    EffectState.UNKNOWN: {EffectState.RECONCILIATION},
    EffectState.RECONCILIATION: {
        EffectState.SUCCEEDED,
        EffectState.BLOCKED,
        EffectState.UNKNOWN,
        EffectState.IN_FLIGHT,
    },
    EffectState.SUCCEEDED: set(),
    EffectState.BLOCKED: set(),
}
```

Implement slug normalization with `unicodedata.normalize("NFKC", title).lower()`, retain only
`str.isalnum()`, collapse other runs to one hyphen, cap the slug at 48 Unicode code points, strip
punctuation, and fall back to the Requirement type value.

- [ ] **Step 4: Register architecture contracts**

Add `control_plane.app.modules.source_control` to the layer containers, domain/shared API contract, and
all symmetric deep-import forbidden contracts. Do not add exemptions allowing Source Control internals to
import Requirement internals.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run pytest tests/source_control/test_domain.py tests/test_contract_guard.py -q
uv run lint-imports
git add control_plane/app/modules/source_control tests/source_control tests/test_contract_guard.py pyproject.toml
git commit -m "feat(source-control): establish deep module and effect state model"
```

---

### Task 3: Add the independent Source Control schema and SQL repository

**Files:**

- Create: `migrations/source_control/0001_source_control_foundation.py`
- Modify: `alembic.ini`
- Modify: `control_plane/app/shared/db/settings.py`
- Create: `control_plane/app/modules/source_control/ports/{repository,runtime,__init__}.py`
- Create: `control_plane/app/modules/source_control/adapters/{sqlalchemy,__init__}.py`
- Create: `tests/source_control/{conftest,test_migration}.py`

**Interfaces:**

- Produces: independent Alembic head `0001_source_control_foundation (source_control)`.
- Produces: sequence `source_control.work_item_number_seq` and five owner tables.
- Produces: `SourceControlRepository` / `SourceControlRepositoryFactory` and `SqlAlchemySourceControlRepository`.

- [ ] **Step 1: Write failing migration lifecycle and privilege tests**

```python
EXPECTED_TABLES = {
    "workspace_repository",
    "binding_request_inbox",
    "source_control_effect",
    "repository_branch_binding",
    "webhook_inbox",
}


def test_source_control_schema_and_minimum_privileges(owner_engine):
    assert table_names(owner_engine, "source_control") == EXPECTED_TABLES
    assert privileges(owner_engine, "source_control_rw", "repository_branch_binding") == {
        "SELECT",
        "INSERT",
    }
    assert not has_schema_privilege(owner_engine, "source_control_rw", "requirement", "USAGE")


def test_binding_is_immutable_for_runtime_role(source_control_engine):
    binding_id = insert_binding_as_owner()
    with source_control_engine.begin() as db:
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE source_control.repository_branch_binding "
                    "SET branch_name='other' WHERE id=:id"
                ),
                {"id": binding_id},
            )
```

Also assert: one Effect per WorkItem, one Binding per WorkItem, unique `(repository_id,branch_name)`, same
Webhook ID uniqueness per repository, non-secret CHECKs, upgrade/downgrade preserves Requirement facts, and
downgrade refuses when Source Control business rows still exist.

- [ ] **Step 2: Run migration tests and confirm RED**

Run: `uv run pytest tests/source_control/test_migration.py -q`

Expected: failure because the `source_control` Alembic branch and schema do not exist.

- [ ] **Step 3: Implement migration and runtime role**

Use exact table responsibilities from the spec. Core constraints include:

```sql
CONSTRAINT ck_source_control_provider CHECK (provider = 'GITLAB'),
CONSTRAINT ck_source_control_default_branch CHECK (default_branch = 'main'),
CONSTRAINT ck_source_control_effect_operation CHECK (operation = 'CREATE_TASK_BRANCH'),
CONSTRAINT ck_source_control_effect_state CHECK (
  state IN ('PLANNED','IN_FLIGHT','UNKNOWN','RECONCILIATION','SUCCEEDED','BLOCKED')
),
CONSTRAINT uq_source_control_effect_work_item UNIQUE (work_item_id),
CONSTRAINT uq_source_control_binding_work_item UNIQUE (work_item_id),
CONSTRAINT uq_source_control_binding_branch UNIQUE (repository_id, branch_name),
CONSTRAINT uq_source_control_webhook_message UNIQUE (repository_id, webhook_id)
```

Grant `source_control_rw` `SELECT/INSERT/UPDATE` on mutable tables, only `SELECT/INSERT` on
`repository_branch_binding`, and `USAGE,SELECT` on `work_item_number_seq`. Grant nothing on other schemas.

- [ ] **Step 4: Implement repository interface at domain vocabulary**

The interface must include the operations actually needed by later tasks:

```python
class SourceControlRepository(Protocol):
    db: Connection

    def insert_workspace_repository(self, **values: Any) -> Any: ...
    def workspace_repository(self, repository_id: str, *, for_update: bool = False) -> Any: ...
    def remove_workspace_repository(
        self, repository_id: str, *, expected_revision: int, now: datetime
    ) -> Any: ...
    def accept_binding_request(self, **values: Any) -> Any: ...
    def binding_request(self, message_id: str, *, for_update: bool = False) -> Any: ...
    def claim_binding_requests(
        self, *, limit: int, now: datetime, lease_until: datetime
    ) -> list[Any]: ...
    def next_work_item_number(self) -> int: ...
    def insert_effect(self, **values: Any) -> Any: ...
    def effect_by_work_item(self, work_item_id: str, *, for_update: bool = False) -> Any: ...
    def transition_effect(
        self, effect_id: str, *, expected_state: str, values: Mapping[str, object]
    ) -> Any: ...
    def claim_unknown_effects(
        self, *, limit: int, now: datetime, lease_until: datetime
    ) -> list[Any]: ...
    def insert_binding(self, **values: Any) -> Any: ...
    def binding_by_work_item(self, work_item_id: str) -> Any: ...
    def accept_webhook(self, **values: Any) -> Any: ...
```

- [ ] **Step 5: Run migration and SQL repository tests**

```powershell
uv run pytest tests/source_control/test_migration.py -q
uv run alembic heads
```

Expected: the new independent head is listed, all privilege and lifecycle tests pass, and no other migration head changes.

- [ ] **Step 6: Commit persistence foundation**

```powershell
git add migrations/source_control alembic.ini control_plane/app/shared/db/settings.py control_plane/app/modules/source_control/ports control_plane/app/modules/source_control/adapters tests/source_control
git commit -m "feat(source-control): add independent effect and binding schema"
```

---

### Task 4: Register authorized repositories and relay Requirement messages idempotently

**Files:**

- Create: `control_plane/app/modules/source_control/ports/requirement.py`
- Create: `control_plane/app/modules/source_control/application/{dependencies,commands,relay,__init__}.py`
- Create: `control_plane/app/modules/source_control/adapters/{requirement,eligibility}.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Create: `tests/source_control/{test_relay,test_commands}.py`

**Interfaces:**

- Produces internal Facade: `register_workspace_repository`、`remove_workspace_repository`、`relay_binding_requests`、`accept_binding_request`.
- Produces `RequirementBindingPort` and `OwnerEligibilityPort` seams.
- Consumes only Requirement/Identity/Workspace/Authorization package-root Facades in adapters.

- [ ] **Step 1: Write failing repository registration and relay crash-window tests**

```python
def test_register_repository_stores_only_secret_references(repository, dependencies):
    registered = register_workspace_repository(
        repository,
        repository_id="gitlab-project-1",
        workspace_id=WORKSPACE_ID,
        project_id="101",
        project_path="platform/backend",
        connection_ref="gitlab-dev",
        credential_secret_ref="openbao:source-control/gitlab-dev/token",
        webhook_signing_secret_ref="openbao:source-control/gitlab-dev/webhook",
        actor=SYSTEM,
        dependencies=dependencies,
    )
    assert registered.status is RepositoryAuthorizationState.AUTHORIZED
    assert registered.credential_secret_ref == "openbao:source-control/gitlab-dev/token"
    assert "glpat-" not in registered.model_dump_json().lower()


def test_relay_replays_after_requirement_ack_failure_without_duplicate_inbox(
    repository, requirement_port, dependencies
):
    requirement_port.fail_next_ack = True
    with pytest.raises(RequirementCallbackUnavailable):
        relay_binding_requests(repository, limit=10, dependencies=dependencies)
    assert repository.binding_request_count() == 1

    result = relay_binding_requests(repository, limit=10, dependencies=dependencies)
    assert result.accepted == 1
    assert repository.binding_request_count() == 1
    assert requirement_port.acked_message_ids == [MESSAGE_ID]
```

Also test same message ID/digest idempotence, same ID/different digest conflict, repository removal blocks new requests while historical rows remain, and a repository cannot move across Workspaces by update.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest tests/source_control/test_relay.py tests/source_control/test_commands.py -q`

Expected: failure because the application Facade and inter-module ports do not exist.

- [ ] **Step 3: Define compact inter-module ports**

```python
class RequirementBindingPort(Protocol):
    def claim_requests(
        self, *, limit: int, lease_until: datetime
    ) -> tuple[BindingRequestEnvelope, ...]: ...
    def acknowledge_request(self, message_id: str) -> None: ...
    def release_request(self, message_id: str, *, error_code: str, retry_at: datetime) -> None: ...
    def binding_context(self, work_item_id: str) -> RequirementBindingContext: ...
    def record_ready(self, result: BindingReadyResult) -> None: ...
    def record_blocked(self, result: BindingBlockedResult) -> None: ...


class OwnerEligibilityPort(Protocol):
    def evaluate(self, context: RequirementBindingContext) -> BindingEligibility: ...
```

`RequirementFacadeBindingAdapter` opens Requirement transactions through an injected Requirement engine and calls only package-root Facades. `CurrentOwnerEligibilityAdapter` calls `identity.get_account`, `workspace.is_formal_member`, and `authorization.effective_grants` through package roots; unavailable dependencies return an ineligible result rather than allowing access.

- [ ] **Step 4: Implement canonical payload hashing and relay**

Canonicalize only the stable envelope fields:

```python
payload = {
    "messageId": envelope.message_id,
    "topic": envelope.topic,
    "requirementId": envelope.requirement_id,
    "requirementVersion": envelope.requirement_version,
    "workItemId": envelope.work_item_id,
    "repositoryId": envelope.repository_id,
}
payload_hash = "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()
```

Insert `binding_request_inbox` before acknowledging Requirement. A duplicate with the same hash returns the existing Inbox row; a different hash raises `BindingRequestMessageConflict` and releases the Requirement message with an allowlisted non-retryable code.

- [ ] **Step 5: Run focused tests and commit**

```powershell
uv run pytest tests/source_control/test_relay.py tests/source_control/test_commands.py tests/requirement/test_source_control_relay.py -q
uv run lint-imports
git add control_plane/app/modules/source_control tests/source_control
git commit -m "feat(source-control): relay governed repository binding requests"
```

---

### Task 5: Implement the GitLab read-create-read branch Effect Saga

**Files:**

- Create: `control_plane/app/modules/source_control/ports/gitlab.py`
- Create: `control_plane/app/modules/source_control/adapters/gitlab.py`
- Modify: `control_plane/app/modules/source_control/application/{commands,dependencies}.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/source_control/{test_gitlab_adapter,test_commands}.py`

**Interfaces:**

- Produces `GitLabPort.get_branch(repository, name)` and `create_branch(repository, name, ref_sha)`.
- Produces internal Facade `process_binding_request(message_id)` and query `get_repository_branch_binding(work_item_id)`.
- Moves `httpx` from dev-only to main dependencies without changing its locked version unexpectedly.

- [ ] **Step 1: Write failing adapter request-shape tests**

Using `httpx.MockTransport`, prove the exact call order and parameters:

```python
def test_gitlab_adapter_reads_main_creates_from_exact_sha_and_reads_back():
    calls: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "GET" and request.url.path.endswith("/branches/main"):
            return httpx.Response(200, json={"name": "main", "commit": {"id": BASE_SHA}})
        if request.method == "POST":
            assert request.url.params["branch"] == TASK_BRANCH
            assert request.url.params["ref"] == BASE_SHA
            return httpx.Response(201, json={"name": TASK_BRANCH, "commit": {"id": BASE_SHA}})
        return httpx.Response(200, json={"name": TASK_BRANCH, "commit": {"id": BASE_SHA}})

    result = run_create_branch_saga(client_from(handler))
    assert result.commit_sha == BASE_SHA
    assert [method for method, _, _ in calls] == ["GET", "POST", "GET"]
```

Also test: token is only in `PRIVATE-TOKEN` header; URLs encode project/branch; 401/403 map to `ACCESS_DENIED`; main 404 maps to `DEFAULT_BRANCH_NOT_FOUND`; POST timeout followed by exact readback succeeds; POST 409 followed by exact readback succeeds; timeout plus unreadable branch raises `GitLabResultUnknown`; differing SHA raises `GitLabBranchConflict`; response bodies never appear in exception strings.

- [ ] **Step 2: Write failing Saga idempotency and guard tests**

```python
def test_duplicate_processing_reuses_effect_number_name_and_binding(repository, dependencies):
    first = process_binding_request(repository, message_id=MESSAGE_ID, dependencies=dependencies)
    second = process_binding_request(repository, message_id=MESSAGE_ID, dependencies=dependencies)
    assert second == first
    assert repository.effect_count(work_item_id=WORK_ITEM_ID) == 1
    assert repository.binding_count(work_item_id=WORK_ITEM_ID) == 1
    assert dependencies.gitlab.created == [(TASK_BRANCH, BASE_SHA)]


def test_timeout_never_creates_binding(repository, dependencies):
    dependencies.gitlab.create_error = GitLabResultUnknown("timeout")
    result = process_binding_request(repository, message_id=MESSAGE_ID, dependencies=dependencies)
    assert result.effect.state is EffectState.UNKNOWN
    assert repository.binding_by_work_item(WORK_ITEM_ID) is None
```

Also prove owner unassigned/ineligible and unauthorized/removed repository produce `BLOCKED` without any GitLab call; context/repository mismatch is blocked; concurrent processing leaves one Effect.

- [ ] **Step 3: Run tests and confirm RED**

Run:

```powershell
uv run pytest tests/source_control/test_gitlab_adapter.py tests/source_control/test_commands.py -q
```

Expected: failures because GitLabPort, HTTPX adapter and Saga do not exist.

- [ ] **Step 4: Implement the HTTPX GitLab adapter**

Use a caller-supplied `httpx.Client`, timeout values from `SourceControlPolicyPort`, and a token resolved by Secret Reference immediately before the call. Implement:

```python
def get_branch(self, repository: GitLabRepositoryProfile, name: str) -> BranchSnapshot:
    response = self._client.get(
        f"/projects/{quote(repository.project_id, safe='')}/repository/branches/{quote(name, safe='')}",
        headers={"PRIVATE-TOKEN": self._token(repository.credential_secret_ref)},
    )
    return self._decode_branch(response)


def create_branch(
    self,
    repository: GitLabRepositoryProfile,
    *,
    name: str,
    ref_sha: str,
) -> BranchSnapshot:
    response = self._client.post(
        f"/projects/{quote(repository.project_id, safe='')}/repository/branches",
        params={"branch": name, "ref": ref_sha},
        headers={"PRIVATE-TOKEN": self._token(repository.credential_secret_ref)},
    )
    return self._decode_branch(response)
```

Do not log request headers or response bodies. Verify full hexadecimal commit IDs at the Adapter edge.

- [ ] **Step 5: Implement Effect planning and external call boundaries**

`process_binding_request` must:

1. Claim Inbox in a short Source Control transaction.
2. Read current Requirement context and eligibility outside that transaction.
3. Validate an AUTHORIZED Workspace Repository.
4. GET `main` outside a transaction.
5. Insert or load one Effect with allocated workItemNumber, deterministic branchName and base SHA.
6. Commit `IN_FLIGHT`, call POST, then always GET the task branch.
7. In a new transaction, either insert Binding + mark `SUCCEEDED`, mark `BLOCKED`, or mark `UNKNOWN` with a due time.
8. Deliver the Requirement callback outside the Source Control transaction.

- [ ] **Step 6: Move HTTPX to runtime dependencies and run tests**

Edit `pyproject.toml` so `httpx>=0.28` is in `[project].dependencies` and not duplicated in the dev group, then run `uv lock`.

```powershell
uv lock
uv run pytest tests/source_control/test_gitlab_adapter.py tests/source_control/test_commands.py -q
uv run ruff check control_plane/app/modules/source_control tests/source_control
uv run mypy control_plane/app/modules/source_control tests/source_control
```

- [ ] **Step 7: Commit GitLab branch Saga**

```powershell
git add control_plane/app/modules/source_control tests/source_control pyproject.toml uv.lock
git commit -m "feat(source-control): create deterministic gitlab task branches"
```

---

### Task 6: Add Standard Webhooks signature verification and Webhook Inbox

**Files:**

- Create: `control_plane/app/modules/source_control/adapters/webhook.py`
- Create: `control_plane/app/modules/source_control/application/webhooks.py`
- Create: `control_plane/app/modules/source_control/api/{__init__,webhooks}.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Create: `tests/source_control/test_webhooks.py`

**Interfaces:**

- Produces `verify_gitlab_standard_webhook(...)` and `ingest_signed_gitlab_webhook(...)`.
- Produces Connector-only route `POST /webhooks/gitlab/{repositoryId}`.
- Persists only verified, sanitized Webhook Inbox summaries.

- [ ] **Step 1: Write failing official-algorithm signature tests**

```python
def sign(signing_token: str, webhook_id: str, timestamp: int, body: bytes) -> str:
    key = base64.b64decode(signing_token.removeprefix("whsec_"))
    message = f"{webhook_id}.{timestamp}.".encode() + body
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def test_signed_webhook_is_verified_before_deduplicated(client, signing_token, clock):
    body = canonical_push_body(project_id=101, ref=f"refs/heads/{TASK_BRANCH}")
    headers = {
        "webhook-id": WEBHOOK_ID,
        "webhook-timestamp": str(int(clock.now().timestamp())),
        "webhook-signature": sign(signing_token, WEBHOOK_ID, int(clock.now().timestamp()), body),
        "X-Gitlab-Event": "Push Hook",
    }
    first = client.post(f"/webhooks/gitlab/{REPOSITORY_ID}", content=body, headers=headers)
    second = client.post(f"/webhooks/gitlab/{REPOSITORY_ID}", content=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["inboxId"] == second.json()["inboxId"]
```

Also prove: missing any Standard header, bad signature, stale timestamp, invalid `whsec_` encoding, token-only `X-Gitlab-Token`, project ID mismatch and same webhook ID/different digest are rejected; multiple space-separated signatures accept when any one matches; invalid signatures leave zero Webhook Inbox rows; no response includes secret or raw body.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest tests/source_control/test_webhooks.py -q`

Expected: failure because the verifier, ingest use case and route do not exist.

- [ ] **Step 3: Implement constant-time verification over raw bytes**

Implement the official message shape exactly:

```python
message = webhook_id.encode("utf-8") + b"." + timestamp.encode("ascii") + b"." + raw_body
expected = "v1," + base64.b64encode(
    hmac.new(decoded_signing_key, message, hashlib.sha256).digest()
).decode("ascii")
valid = any(hmac.compare_digest(expected, value) for value in signature_header.split(" "))
```

Validate timestamp against `SourceControlPolicyPort.webhook_replay_window`; if Policy or signing secret resolution is unavailable, reject without fallback. Parse JSON only after this function returns success.

- [ ] **Step 4: Persist a sanitized Inbox summary**

For Push Hook, save only project ID, ref, before/after/checkout SHA, object kind, event type, provider event UUID, payload digest and receipt metadata. Unknown valid event types are saved with `state=IGNORED` and no raw payload. A duplicate same digest returns the original Inbox ID; a digest conflict records an Audit denial and returns a security error.

- [ ] **Step 5: Run tests, architecture check and commit**

```powershell
uv run pytest tests/source_control/test_webhooks.py tests/source_control/test_migration.py -q
uv run lint-imports
git add control_plane/app/modules/source_control tests/source_control
git commit -m "feat(source-control): verify and deduplicate gitlab webhooks"
```

---

### Task 7: Reconcile unknown Effects and replay Requirement callbacks

**Files:**

- Create: `control_plane/app/modules/source_control/application/reconciliation.py`
- Modify: `control_plane/app/modules/source_control/application/{commands,webhooks,__init__}.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Create: `tests/source_control/test_reconciliation.py`

**Interfaces:**

- Produces internal Facade `reconcile_due_effects(limit)` and `process_webhook_inbox(inbox_id)`.
- Guarantees stable Ready/Blocked callback keys derived from Effect ID.
- Guarantees Webhook only schedules observation and never creates Binding directly.

- [ ] **Step 1: Write failing reconciliation matrix tests**

```python
@pytest.mark.parametrize(
    ("observed_sha", "expected_state", "binding_count"),
    [
        (BASE_SHA, EffectState.SUCCEEDED, 1),
        (OTHER_SHA, EffectState.BLOCKED, 0),
    ],
)
def test_reconciliation_converges_observed_branch(
    repository, dependencies, observed_sha, expected_state, binding_count
):
    effect = unknown_effect(repository, base_sha=BASE_SHA)
    dependencies.gitlab.branch_sha = observed_sha
    result = reconcile_due_effects(repository, limit=10, dependencies=dependencies)
    assert result.effects[0].state is expected_state
    assert repository.binding_count(work_item_id=effect.work_item_id) == binding_count


def test_requirement_callback_failure_does_not_undo_external_success(repository, dependencies):
    effect = succeeded_effect_with_binding(repository)
    dependencies.requirements.fail_next_ready = True
    reconcile_due_effects(repository, limit=10, dependencies=dependencies)
    assert repository.effect(effect.id).state is EffectState.SUCCEEDED
    assert repository.effect(effect.id).callback_state is RequirementCallbackState.FAILED

    reconcile_due_effects(repository, limit=10, dependencies=dependencies)
    assert repository.effect(effect.id).callback_state is RequirementCallbackState.ACKED
```

Also prove: missing branch retries create with the same name/base; a second unknown returns to `UNKNOWN` with a later due time; removed repository or ineligible owner stops new writes and blocks; webhook Push Hook only makes a matching unknown Effect due; stale WorkItem revision triggers context reread and revalidation; Ready after prior `RECONCILIATION_PENDING` clears Requirement blocked fields.

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run pytest tests/source_control/test_reconciliation.py -q`

Expected: failure because `reconcile_due_effects` and callback replay do not exist.

- [ ] **Step 3: Implement CAS claim and observation loop**

Claim due UNKNOWN rows using `FOR UPDATE SKIP LOCKED`, transition each to `RECONCILIATION`, commit, then query GitLab outside the transaction. Use this result mapping:

```python
if observed is not None and observed.commit_sha == effect.base_commit_sha:
    outcome = ReconciliationOutcome.CONFIRMED
elif observed is not None:
    outcome = ReconciliationOutcome.CONFLICT
elif eligibility.allowed and repository.status is RepositoryAuthorizationState.AUTHORIZED:
    outcome = ReconciliationOutcome.RETRY_SAME_EFFECT
else:
    outcome = ReconciliationOutcome.BLOCKED
```

Any query/create/readback uncertainty transitions back to UNKNOWN with `next_reconcile_at` from PolicyPort.

- [ ] **Step 4: Implement stable Requirement callbacks**

Use exact callback keys:

```python
ready_key = f"source-control:binding-ready:{effect.id}"
blocked_key = f"source-control:binding-blocked:{effect.id}:{effect.last_error_code}"
```

Ready calls the existing `record_repository_binding` Facade. Blocked maps internal error codes to the extended safe Requirement enum. If Effect is UNKNOWN, send `RECONCILIATION_PENDING`; a later Ready callback uses current WorkItem revision returned by `binding_context` and clears the blocked fact through the existing Requirement implementation.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run pytest tests/source_control/test_reconciliation.py tests/source_control/test_commands.py tests/requirement/test_binding.py -q
uv run ruff check control_plane/app/modules/source_control tests/source_control
uv run mypy control_plane/app/modules/source_control tests/source_control
git add control_plane/app/modules/source_control tests/source_control
git commit -m "feat(source-control): reconcile unknown branch effects"
```

---

### Task 8: Add Connector bootstrap, worker entrypoint, PostgreSQL E2E and full gates

**Files:**

- Create: `control_plane/app/bootstrap/source_control_connector.py`
- Create: `control_plane/tools/source_control_worker.py`
- Create: `tests/source_control/test_e2e.py`
- Modify: `tests/test_openapi_export.py`
- Modify: any Source Control files exposed by the E2E only when the observed contract requires it

**Interfaces:**

- Produces `create_source_control_connector_app(runtime_provider=...) -> FastAPI` with only the signed Webhook route and health checks.
- Produces one-shot worker commands `relay`, `process`, and `reconcile`; no scheduler or GitOps deployment.
- Proves exact baseline with real PostgreSQL and Fake GitLab/Secret/Policy adapters.

- [ ] **Step 1: Write failing end-to-end Happy Path and unknown-path tests**

Happy Path:

```text
Requirement create with an assigned test owner
→ Requirement Outbox PENDING
→ relay persists Source Control Binding Request Inbox and acknowledges Outbox
→ Requirement state PREPARING
→ register AUTHORIZED Workspace Repository
→ process validates owner and repository
→ Fake GitLab records GET main → POST exact SHA → GET task branch
→ Source Control Effect SUCCEEDED + immutable Branch Binding
→ Requirement Facade records BOUND with the same base SHA/branch
→ duplicate relay/process produces no additional Effect, branch or Binding
```

Unknown Path:

```text
POST branch times out and immediate GET is unavailable
→ Effect UNKNOWN, zero Binding, Requirement BLOCKED/RECONCILIATION_PENDING
→ signed Push Hook is accepted and deduplicated
→ Webhook processing only makes Effect due
→ Reconciler GET proves exact SHA
→ one immutable Binding, Effect SUCCEEDED, Requirement BOUND
```

Add assertions that no SQL role can cross-write schemas, Audit contains IDs/reason codes but no tokens/body, and Connector route is absent from `create_app().openapi()`.

- [ ] **Step 2: Run E2E and confirm RED**

Run: `uv run pytest tests/source_control/test_e2e.py -q`

Expected: failure because Connector bootstrap and worker orchestration are not assembled.

- [ ] **Step 3: Implement fail-closed Connector runtime assembly**

`create_source_control_connector_app` receives a runtime provider. The default runtime uses
`source_control_database_url`; unresolved GitLab credential/signing Secret or Source Control Policy raises a
structured unavailable error. It must not use local fake values or mount onto the browser app.

`control_plane.tools.source_control_worker` accepts one required subcommand:

```text
python -m control_plane.tools.source_control_worker relay --limit 50
python -m control_plane.tools.source_control_worker process --limit 50
python -m control_plane.tools.source_control_worker reconcile --limit 50
```

Each command returns non-zero when dependencies are unavailable, prints only counts/effect IDs/error codes, and never prints credentials or external response bodies.

- [ ] **Step 4: Make E2E green without real credentials**

Inject real SQL repositories and Fake GitLab/Secret/Policy/Eligibility adapters at the declared Port seams. Do not add runtime conditionals keyed on tests. Run:

```powershell
uv run pytest tests/source_control/test_e2e.py tests/requirement/test_e2e.py -q
```

Expected: both Source Control and existing Requirement E2E pass.

- [ ] **Step 5: Verify the approved public OpenAPI change is bounded**

Run:

```powershell
uv run python scripts/export_openapi.py --check
uv run pytest tests/test_openapi_export.py -q
git diff 631979b759944b7a95bc3cd380a573fbfc4b6aab -- openapi.json
```

Expected: exporter and test pass; the only `openapi.json` diff is the four approved
Requirement blocked reason values (`OWNER_UNASSIGNED`, `OWNER_INELIGIBLE`,
`REPOSITORY_NOT_AUTHORIZED`, `RECONCILIATION_PENDING`), while Connector Webhook ingress
remains absent because it is a separate app.

- [ ] **Step 6: Run the complete CI-equivalent gate**

Run exactly in this order:

```powershell
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run lint-imports
uv run alembic upgrade heads
uv run pytest -v
uv run python scripts/export_openapi.py --check
git diff --check
git status --short
```

Expected: every command exits 0; pytest reports zero failures; any environment-only skip already present on baseline must be listed and no new skip/xfail may be introduced.

- [ ] **Step 7: Commit E2E and assembly**

```powershell
git add control_plane/app/bootstrap/source_control_connector.py control_plane/tools/source_control_worker.py tests/source_control/test_e2e.py tests/test_openapi_export.py
git commit -m "test(source-control): prove binding and reconciliation flow"
```

- [ ] **Step 8: Request fixed-HEAD review before push or PR**

Use `superpowers:requesting-code-review` against the exact branch HEAD. Review must explicitly check:

- no Requirement private-table access or cross-schema grants;
- no database transaction held across GitLab/Facade calls;
- exact read-main/create-SHA/read-branch order;
- timeout/unknown never creates a Binding;
- webhook verifies raw bytes before JSON and has no token fallback;
- Binding is immutable and callback replay cannot undo an external fact;
- diff contains none of the excluded MR/Merge/Artifact/Frontend/GitOps scope.

Only after review findings and all gates are green may the branch be pushed and a PR proposed. Do not merge, tag, release, delete the worktree, or claim a real GitLab Smoke Test without a new explicit user decision.

---

## Plan Self-Review

- Spec coverage: Workspace Repository (Task 3/4), Requirement relay (Task 1/4), deterministic Branch Binding (Task 2/5), Effect Ledger and UNKNOWN (Task 3/5/7), signed Webhook Inbox (Task 3/6), Requirement-only callback (Task 4/7), Connector/E2E (Task 8).
- Scope check: MR、Merge、Artifact、Acceptance、Chat/Model、Frontend、GitOps 与真实凭据验证均只作为排除项，不产生实现文件。
- Type consistency: `BindingRequestEnvelope`、`RequirementBindingContext`、`SourceControlEffectDto`、`RepositoryBranchBindingDto`、`RequirementBindingPort` 与 Facade 名称在任务间一致。
- Transaction check: 所有外部调用和跨模块调用都位于 owner transaction 之外；Outbox/Inbox 与 Effect 通过重复投递收敛。
- Placeholder scan: plan contains concrete files, interfaces, tests, commands, expected failures and commit boundaries; no implementation placeholder remains.
