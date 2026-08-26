# V0.3 Human Integration MR Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让已确认基线且已绑定任务分支的人工 WorkItem，通过受保护 Requirement 命令创建并合并 `task → dev` Integration MR，并以准确 SHA、Effect Ledger、Webhook Inbox 与 Reconciliation 收敛 GitLab 外部事实。

**Architecture:** 一个纵向批次跨 `requirement` 与 `source_control` 两个深模块：Requirement 拥有人工责任、业务命令、状态与 Outbox；Source Control 拥有 MR Binding、Observation、GitLab Effect 与对账。模块间只经包根 Facade 和 Outbox/Inbox 协作，不跨 schema 双写。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy Core、PostgreSQL 18、Alembic 多 head、HTTPX、pytest、Ruff、mypy、import-linter。

**Spec:** `docs/superpowers/specs/2026-08-26-backend-integration-mr-v03-design.md`

## Global Constraints

- 代码基线固定为 `main@f03f95277acdc3fc8b50cba0e28ad136afb672e5`；设计提交为 `4c1ed0f`。
- 事实 owner 以 `engineering-platform-docs@541b186878d1e28e1aa9308111a2962cdfefb91b` 的 `architecture/README.md` 所有权矩阵为准。
- Requirement 不读取或写入 `source_control` schema；Source Control 不读取或写入 `requirement` schema。
- 所有跨模块调用只能经过 `control_plane.app.modules.<module>` 包根 Facade；import-linter 必须继续通过。
- GitLab 写操作必须先持久化 Effect，再发 HTTP，再 GET 回读；可能已到达 Provider 的未知结果只能进入 `UNKNOWN/RECONCILIATION`。
- MR source/target 只能从 Repository Branch Binding 与服务端常量 `dev` 推导；HTTP 调用者不能提交分支名、GitLab project ID、MR IID 或 head SHA。
- Integration Merge 固定 `sha=<准确 headSha>`、`squash=false`、`should_remove_source_branch=false`、`auto_merge=false`；Project `merge_method` 必须为 `merge`。
- Webhook 只触发回查，不能直接更新 MR Observation 或 Requirement 状态。
- 不记录或输出 PAT、Webhook signing token、完整 Provider error body、源码、commit message、用户邮箱或头像。
- 本批不实现 Jenkins/Artifact/Evidence/Acceptance/Formal MR/main Merge/Agent/Chat/Model/前端/GitOps。
- 每个任务先写失败测试并观察预期失败，再写最小实现；不得使用 skip/xfail 或测试专用业务分支。
- 执行前必须恢复 `uv` 并运行 `uv sync --locked`；本工作树现有 `.venv` 引用了已清理的临时 Python，不能作为有效基线。
- PostgreSQL 测试必须先运行 `docker compose up -d`，设置 `MIGRATION_DATABASE_URL=postgresql+psycopg://platform_owner:localdev@localhost:5432/platform` 与 `REQUIRE_INTEGRATION_DB=1`，避免静默 skip。

## File Structure

### Requirement owner

- `domain/delivery.py`：Integration Delivery 枚举、DTO、错误与纯状态转换。
- `application/delivery.py`：开始实现、请求 MR、请求 Merge、Source Control 回调用例。
- `application/delivery_relay.py`：两类 Outbox 的 claim/ack/release 与 Context 查询。
- `adapters/sqlalchemy.py`：Requirement 聚合、WorkItem 投影、Outbox 的 SQL 持久化；不出现 Source Control SQL。
- `api/dto.py`、`api/routes.py`：三个受保护业务命令与 Problem Details 映射。
- `migrations/requirement/0004_requirement_integration_delivery.py`：状态枚举 CHECK 与 WorkItem delivery 投影。

### Source Control owner

- `domain/integration.py`：MR Binding/Observation/Effect DTO 与稳定错误。
- `ports/merge_requests.py`：GitLab MR/Profile Port。
- `ports/integration_repository.py`：Integration Inbox/Effect/Binding/Observation Port。
- `ports/delivery_requirement.py`：Requirement Delivery Facade Port。
- `adapters/gitlab_merge_requests.py`：GitLab Projects/MR REST Adapter。
- `adapters/integration_sqlalchemy.py`：Source Control integration 表的 SQL Adapter。
- `adapters/requirement_delivery.py`：只调用 Requirement 包根 Facade。
- `application/delivery_relay.py`：Requirement Outbox → Source Control Inbox。
- `application/integration.py`：创建 MR 与 Merge Saga。
- `application/integration_reconciliation.py`：UNKNOWN Effect、回调与外部 merge drift 收敛。
- `migrations/source_control/0005_source_control_integration_delivery.py`：Effect 通用化与 MR 表。
- 既有 `application/webhooks.py`、`api/webhooks.py`、worker：只增加 MR 摘要和调度，不复制 Saga。

---

### Task 1: Requirement Integration Delivery Domain 与 schema

**Files:**
- Create: `control_plane/app/modules/requirement/domain/delivery.py`
- Modify: `control_plane/app/modules/requirement/domain/models.py`
- Modify: `control_plane/app/modules/requirement/domain/transitions.py`
- Modify: `control_plane/app/modules/requirement/domain/__init__.py`
- Create: `migrations/requirement/0004_requirement_integration_delivery.py`
- Create: `tests/requirement/test_delivery_domain.py`
- Modify: `tests/requirement/test_migration.py`
- Modify: `tests/requirement/test_migration_lifecycle.py`

**Interfaces:**
- Consumes: existing `RequirementState`、`WorkItemState`、`RepositoryState`、`AssignmentState`。
- Produces: `IntegrationDeliveryState`、`IntegrationDeliveryBlockedReason`、`IntegrationDeliveryRequestKind`、`IntegrationDeliveryContext`、`IntegrationDeliveryRequestMessage`、`transition_human_work_started()`、`transition_integration_mr_ready()`。

- [ ] **Step 1: 写失败的纯领域状态测试**

```python
def test_human_start_requires_ready_requirement_and_work_item() -> None:
    assert transition_human_work_started(
        RequirementState.READY,
        WorkItemState.READY,
    ) == (RequirementState.IN_PROGRESS, WorkItemState.IN_PROGRESS)
    with pytest.raises(InvalidRequirementTransition):
        transition_human_work_started(
            RequirementState.PREPARING,
            WorkItemState.READY,
        )


def test_mr_ready_keeps_requirement_in_progress_until_all_required_items_verify() -> None:
    assert transition_integration_mr_ready(
        (WorkItemState.VERIFYING, WorkItemState.IN_PROGRESS),
    ) is RequirementState.IN_PROGRESS
    assert transition_integration_mr_ready(
        (WorkItemState.VERIFYING, WorkItemState.VERIFYING),
    ) is RequirementState.VERIFYING
```

- [ ] **Step 2: 运行领域测试并确认缺少新类型**

Run: `uv run pytest tests/requirement/test_delivery_domain.py -q`

Expected: collection FAIL，明确报告 `IntegrationDeliveryState` 或转换函数尚未定义。

- [ ] **Step 3: 实现领域类型与转换**

```python
class IntegrationDeliveryState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IMPLEMENTING = "IMPLEMENTING"
    MR_PENDING = "MR_PENDING"
    MR_OPEN = "MR_OPEN"
    MERGE_PENDING = "MERGE_PENDING"
    INTEGRATED = "INTEGRATED"
    BLOCKED = "BLOCKED"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


class IntegrationDeliveryRequestKind(StrEnum):
    CREATE_MR = "CREATE_MR"
    MERGE_MR = "MERGE_MR"


class IntegrationDeliveryBlockedReason(StrEnum):
    OWNER_MISMATCH = "OWNER_MISMATCH"
    OWNER_INELIGIBLE = "OWNER_INELIGIBLE"
    MERGE_ACTOR_INELIGIBLE = "MERGE_ACTOR_INELIGIBLE"
    REPOSITORY_NOT_AUTHORIZED = "REPOSITORY_NOT_AUTHORIZED"
    BRANCH_BINDING_MISSING = "BRANCH_BINDING_MISSING"
    TARGET_BRANCH_NOT_FOUND = "TARGET_BRANCH_NOT_FOUND"
    TARGET_BRANCH_NOT_PROTECTED = "TARGET_BRANCH_NOT_PROTECTED"
    NO_DELIVERY_COMMIT = "NO_DELIVERY_COMMIT"
    HEAD_SHA_CHANGED = "HEAD_SHA_CHANGED"
    MR_CONFLICT = "MR_CONFLICT"
    MR_CLOSED = "MR_CLOSED"
    MR_CHECKS_BLOCKED = "MR_CHECKS_BLOCKED"
    MERGE_CONFLICT = "MERGE_CONFLICT"
    PROJECT_PROFILE_UNSUPPORTED = "PROJECT_PROFILE_UNSUPPORTED"
    SOURCE_BRANCH_MISSING_AFTER_INTEGRATION = "SOURCE_BRANCH_MISSING_AFTER_INTEGRATION"
    EXTERNAL_MERGE_DRIFT = "EXTERNAL_MERGE_DRIFT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
```

同时把 `IN_PROGRESS`、`VERIFYING` 加入 Requirement/WorkItem 状态，DTO 增加四个 integration 字段；纯转换只接收枚举和必需 WorkItem 状态 tuple，不访问数据库或 Provider。

- [ ] **Step 4: 写失败的迁移测试**

```python
def test_requirement_integration_delivery_columns_and_checks_exist(
    requirement_owner_engine: Engine,
) -> None:
    columns = {
        column["name"]: column
        for column in inspect(requirement_owner_engine).get_columns(
            "work_item", schema="requirement"
        )
    }
    assert columns["integration_delivery_state"]["nullable"] is False
    assert columns["integration_merge_request_binding_id"]["nullable"] is True
    assert columns["integration_blocked_reason_code"]["nullable"] is True
    assert columns["integration_updated_at"]["nullable"] is True
```

- [ ] **Step 5: 实现 Requirement 迁移**

迁移 revision 固定为 `0004_req_int_delivery`，down revision 为 `0003_req_sc_relay`。SQL 必须：

```sql
ALTER TABLE requirement.work_item
  ADD COLUMN integration_delivery_state TEXT NOT NULL DEFAULT 'NOT_STARTED',
  ADD COLUMN integration_merge_request_binding_id UUID,
  ADD COLUMN integration_blocked_reason_code TEXT,
  ADD COLUMN integration_updated_at TIMESTAMPTZ;
```

替换 Requirement/WorkItem state CHECK，加入 `IN_PROGRESS/VERIFYING`；增加 delivery shape CHECK：

```sql
CHECK (
  (integration_delivery_state IN ('NOT_STARTED','IMPLEMENTING','MR_PENDING')
    AND integration_merge_request_binding_id IS NULL)
  OR
  (integration_delivery_state IN ('MR_OPEN','MERGE_PENDING','INTEGRATED')
    AND integration_merge_request_binding_id IS NOT NULL)
  OR
  (integration_delivery_state IN ('BLOCKED','RECONCILIATION_PENDING'))
)
```

`BLOCKED` 必须有 allowlist reason；非 Blocked 状态 reason 必须为空。Downgrade 在存在新状态或 integration 引用时拒绝，不删除可解释的业务事实。

- [ ] **Step 6: 运行 Task 1 测试**

Run: `uv run pytest tests/requirement/test_delivery_domain.py tests/requirement/test_migration.py tests/requirement/test_migration_lifecycle.py -q`

Expected: PASS，0 skipped。

- [ ] **Step 7: 提交 Task 1**

```bash
git add control_plane/app/modules/requirement/domain migrations/requirement tests/requirement/test_delivery_domain.py tests/requirement/test_migration.py tests/requirement/test_migration_lifecycle.py
git commit -m "feat(requirement): model human integration delivery"
```

### Task 2: Requirement 受保护人工命令与 HTTP seam

**Files:**
- Create: `control_plane/app/modules/requirement/application/delivery.py`
- Modify: `control_plane/app/modules/requirement/application/__init__.py`
- Modify: `control_plane/app/modules/requirement/application/dependencies.py`
- Modify: `control_plane/app/modules/requirement/ports/repository.py`
- Modify: `control_plane/app/modules/requirement/adapters/sqlalchemy.py`
- Modify: `control_plane/app/modules/requirement/__init__.py`
- Modify: `control_plane/app/modules/requirement/api/dto.py`
- Modify: `control_plane/app/modules/requirement/api/routes.py`
- Create: `tests/requirement/test_delivery_commands.py`
- Modify: `tests/requirement/test_api.py`

**Interfaces:**
- Consumes: Task 1 delivery enums/transitions、existing idempotency、Audit、actor `account_id`、Repository/Assignment state。
- Produces: `start_work_item()`、`request_integration_merge_request()`、`request_integration_merge()` 与 `WorkItemDeliveryResult`。

- [ ] **Step 1: 写失败的开始实现与请求 MR 命令测试**

在新测试文件复用 `Actor`、`_dependencies`、`_create`、`_gate_dependencies`，增加 `_ready_bound_requirement()` helper：完成 SDD approval 后调用现有 `record_repository_binding()`，返回 READY Requirement/WorkItem。

```python
def test_current_owner_starts_ready_bound_work_item_atomically(isolated_requirement_database):
    ready = _ready_bound_requirement(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        result = start_work_item(
            db,
            requirement_id=ready.requirement.id,
            work_item_id=ready.work_item.id,
            expected_revision=ready.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="start-human-work-1",
            dependencies=_dependencies(),
        )
    assert result.requirement.state is RequirementState.IN_PROGRESS
    assert result.work_item.state is WorkItemState.IN_PROGRESS
    assert result.work_item.integration_delivery_state is IntegrationDeliveryState.IMPLEMENTING


def test_request_integration_mr_persists_intent_and_outbox(isolated_requirement_database):
    started = _started_work_item(isolated_requirement_database)
    with isolated_requirement_database.runtime.begin() as db:
        result = request_integration_merge_request(
            db,
            requirement_id=started.requirement.id,
            work_item_id=started.work_item.id,
            expected_revision=started.requirement.revision,
            actor=Actor("employee-1"),
            idempotency_key="request-integration-mr-1",
            dependencies=_dependencies(),
        )
    assert result.work_item.integration_delivery_state is IntegrationDeliveryState.MR_PENDING
    assert result.outbox_topic == "requirement.integration-merge-request.requested"
```

另写 owner mismatch、UNASSIGNED、Repository 非 BOUND、stale ETag、相同 key 重放、不同 payload 同 key 冲突测试。

- [ ] **Step 2: 运行命令测试确认失败**

Run: `uv run pytest tests/requirement/test_delivery_commands.py -q`

Expected: collection FAIL，报告三个命令尚未导出。

- [ ] **Step 3: 增加 Repository 精确写接口**

```python
def update_work_item_delivery(
    self,
    work_item_id: str,
    *,
    expected_revision: int,
    state: str,
    delivery_state: str,
    binding_id: str | None,
    blocked_reason: str | None,
    now: datetime,
) -> Any: ...

def required_work_item_states(self, requirement_id: str) -> tuple[str, ...]: ...
```

SQL `UPDATE` 必须带 `WHERE id=:id AND revision=:expected_revision`，只更新 Requirement schema；0 行映射 `StaleWorkItemRevision`。

- [ ] **Step 4: 实现三个应用命令**

命令流程固定为：锁 Requirement → 锁 WorkItem → 校验关系/状态/actor → claim idempotency → 写 WorkItem/Requirement → 写 Outbox（MR/Merge 请求）→ Audit → seal idempotency。MR/Merge Outbox payload 分别固定为：

```python
{"kind": "CREATE_MR", "requirementId": requirement.id,
 "requirementRevision": requirement.revision, "workItemId": work_item.id,
 "workItemRevision": work_item.revision, "repositoryId": work_item.repository_id,
 "actorId": actor.account_id}
```

```python
{"kind": "MERGE_MR", "requirementId": requirement.id,
 "requirementRevision": requirement.revision, "workItemId": work_item.id,
 "workItemRevision": work_item.revision,
 "integrationMergeRequestBindingId": work_item.integration_merge_request_binding_id,
 "repositoryId": work_item.repository_id, "actorId": actor.account_id}
```

不接受 body 中的 branch、MR IID 或 SHA。

- [ ] **Step 5: 写失败的 HTTP 授权与契约测试**

```python
def test_integration_routes_require_server_capabilities_and_concurrency_headers(...):
    start = client.post(start_url, headers=versioned_headers("start-1", 4))
    request_mr = client.post(mr_url, headers=versioned_headers("mr-1", 5))
    request_merge = client.post(merge_url, headers=versioned_headers("merge-1", 7))
    assert start.status_code == 200
    assert request_mr.status_code == 202
    assert request_merge.status_code == 202
    assert capability_calls == [
        ("work_item.execute", WORKSPACE_ID),
        ("work_item.execute", WORKSPACE_ID),
        ("merge_request.merge", WORKSPACE_ID),
    ]
```

同时断言缺 `Idempotency-Key`/`If-Match` 为 422、跨 Requirement WorkItem 为 404/409、响应不包含 Provider 字段。

- [ ] **Step 6: 实现 DTO 与三个 routes**

路由使用设计规格中的三个路径、existing `_versioned_preflight()`、`authorized_details()` 与 `_problem()`；`start` 返回 200，两个异步请求返回 202，均返回最新 ETag。

- [ ] **Step 7: 运行 Task 2 测试**

Run: `uv run pytest tests/requirement/test_delivery_commands.py tests/requirement/test_api.py -q`

Expected: PASS，0 skipped。

- [ ] **Step 8: 提交 Task 2**

```bash
git add control_plane/app/modules/requirement tests/requirement/test_delivery_commands.py tests/requirement/test_api.py
git commit -m "feat(requirement): protect human integration commands"
```

### Task 3: Requirement Delivery Outbox Facade 与回调

**Files:**
- Create: `control_plane/app/modules/requirement/application/delivery_relay.py`
- Modify: `control_plane/app/modules/requirement/application/__init__.py`
- Modify: `control_plane/app/modules/requirement/ports/repository.py`
- Modify: `control_plane/app/modules/requirement/adapters/sqlalchemy.py`
- Modify: `control_plane/app/modules/requirement/__init__.py`
- Create: `tests/requirement/test_integration_delivery_relay.py`

**Interfaces:**
- Consumes: Task 2 Outbox topics与 Task 1 delivery projection。
- Produces: `claim_integration_delivery_requests()`、`acknowledge_integration_delivery_request()`、`release_integration_delivery_request()`、`get_integration_delivery_context()`、五个 `record_*` callback。

- [ ] **Step 1: 写失败的 relay 与 Context 测试**

```python
def test_claim_delivery_requests_only_returns_two_allowlisted_topics(isolated_requirement_database):
    _seed_outbox_topics(isolated_requirement_database, (
        "requirement.repository-binding.requested",
        "requirement.integration-merge-request.requested",
        "requirement.integration-merge.requested",
    ))
    with isolated_requirement_database.runtime.begin() as db:
        claimed = claim_integration_delivery_requests(
            db, limit=10, available_before=NOW, lease_until=NOW + timedelta(minutes=1),
            dependencies=_dependencies(),
        )
    assert [item.kind for item in claimed] == [
        IntegrationDeliveryRequestKind.CREATE_MR,
        IntegrationDeliveryRequestKind.MERGE_MR,
    ]


def test_mr_ready_callback_moves_work_item_to_verifying_without_provider_fields(...):
    result = record_integration_mr_ready(
        db, work_item_id=WORK_ITEM_ID, binding_id=BINDING_ID,
        expected_revision=EXPECTED_REVISION, actor=SYSTEM_ACTOR,
        idempotency_key=f"effect:{EFFECT_ID}:mr-ready", dependencies=dependencies,
    )
    assert result.work_item.state is WorkItemState.VERIFYING
    assert result.work_item.integration_delivery_state is IntegrationDeliveryState.MR_OPEN
    assert result.work_item.integration_merge_request_binding_id == BINDING_ID
```

增加相同消息同 payload 重放、同 ID 不同 payload 拒绝、lease 并发不重叠、旧 callback 不回退、merged 保持 VERIFYING、external drift 进入 BLOCKED 的测试。

- [ ] **Step 2: 运行测试确认 Facade 缺失**

Run: `uv run pytest tests/requirement/test_integration_delivery_relay.py -q`

Expected: collection FAIL，报告 relay/callback 未定义。

- [ ] **Step 3: 实现 Outbox relay**

`claim_delivery_requests()` SQL 必须过滤两个精确 topic，使用 `FOR UPDATE SKIP LOCKED`，原子增加 attempts 并把 `available_at` 推到 lease 截止。解析 payload 时使用冻结 Pydantic DTO；未知 kind、缺字段或 message ID/hash 冲突抛 `IntegrationDeliveryMessageInvalid`。

- [ ] **Step 4: 实现 Context 与回调**

```python
class IntegrationDeliveryContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    requirement_id: str
    requirement_state: RequirementState
    workspace_id: str
    work_item_id: str
    work_item_revision: int
    work_item_state: WorkItemState
    repository_id: str
    repository_state: RepositoryState
    human_owner_id: str | None
    required_capabilities: tuple[str, ...]
    base_commit_sha: str | None
    task_branch: str | None
    integration_delivery_state: IntegrationDeliveryState
    integration_merge_request_binding_id: str | None
    request_actor_id: str
```

Callbacks 只接收稳定 Source Control binding ID 和 allowlist reason；所有 Provider 字段留在 Source Control。

- [ ] **Step 5: 运行 Requirement 全部 focused tests**

Run: `uv run pytest tests/requirement -q`

Expected: PASS，0 skipped。

- [ ] **Step 6: 提交 Task 3**

```bash
git add control_plane/app/modules/requirement tests/requirement/test_integration_delivery_relay.py
git commit -m "feat(requirement): expose integration delivery facade"
```

### Task 4: Source Control Integration Domain、Effect 深化与 schema

**Files:**
- Create: `control_plane/app/modules/source_control/domain/integration.py`
- Modify: `control_plane/app/modules/source_control/domain/models.py`
- Modify: `control_plane/app/modules/source_control/domain/__init__.py`
- Create: `control_plane/app/modules/source_control/ports/integration_repository.py`
- Modify: `control_plane/app/modules/source_control/ports/__init__.py`
- Create: `control_plane/app/modules/source_control/adapters/integration_sqlalchemy.py`
- Modify: `control_plane/app/modules/source_control/adapters/__init__.py`
- Modify: `control_plane/app/modules/source_control/adapters/sqlalchemy.py`
- Create: `migrations/source_control/0005_source_control_integration_delivery.py`
- Create: `tests/source_control/test_integration_domain.py`
- Create: `tests/source_control/test_integration_repository.py`
- Modify: `tests/source_control/test_migration.py`
- Modify: `tests/source_control/test_repository.py`

**Interfaces:**
- Consumes: existing `EffectState`、Workspace Repository、Repository Branch Binding。
- Produces: `EffectOperation`、`MergeRequestBindingDto`、`MergeRequestObservationDto`、`DeliveryRequestEnvelope`、`SourceControlIntegrationRepository`。

- [ ] **Step 1: 写失败的 Integration Domain 测试**

```python
def test_merge_effect_subject_is_exact_binding_and_head() -> None:
    assert merge_effect_subject(BINDING_ID, "a" * 40) == f"mr:{BINDING_ID}:" + "a" * 40


def test_observation_requires_merge_commit_only_when_merged() -> None:
    MergeRequestObservationDto(
        id=OBSERVATION_ID, binding_id=BINDING_ID, head_sha="a" * 40,
        state=MergeRequestState.OPEN, merge_commit_sha=None,
        external_merge_user_id=None, merged_at=None,
        observation_digest="sha256:open", observed_at=NOW,
    )
    with pytest.raises(ValidationError):
        MergeRequestObservationDto(
            id=OBSERVATION_ID, binding_id=BINDING_ID, head_sha="a" * 40,
            state=MergeRequestState.MERGED, merge_commit_sha=None,
            external_merge_user_id="42", merged_at=NOW,
            observation_digest="sha256:merged", observed_at=NOW,
        )
```

- [ ] **Step 2: 运行 Domain 测试确认失败**

Run: `uv run pytest tests/source_control/test_integration_domain.py -q`

Expected: collection FAIL，报告 Integration DTO 未定义。

- [ ] **Step 3: 实现 Domain 类型**

```python
class EffectOperation(StrEnum):
    CREATE_TASK_BRANCH = "CREATE_TASK_BRANCH"
    CREATE_INTEGRATION_MR = "CREATE_INTEGRATION_MR"
    MERGE_INTEGRATION_MR = "MERGE_INTEGRATION_MR"


class MergeRequestKind(StrEnum):
    INTEGRATION = "INTEGRATION"


class MergeRequestCreationOrigin(StrEnum):
    PLATFORM_CREATED = "PLATFORM_CREATED"
    EXTERNAL_ADOPTED = "EXTERNAL_ADOPTED"


class MergeRequestState(StrEnum):
    OPEN = "OPEN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"
    LOCKED = "LOCKED"
```

`SourceControlEffectDto` 增加 `subject_key` 与冻结 `payload: dict[str, object]`，branch-only 字段改可空并以 model validator 校验 operation shape。

- [ ] **Step 4: 写失败的 migration/repository 测试**

断言新表精确集合、`source_control_rw` 权限、Binding 不可更新、Observation 只插入、Effect `(operation,subject_key)` 唯一、两个不同 operation 可共享同一 WorkItem，以及旧 Branch Effect DTO/查询仍工作。

- [ ] **Step 5: 实现 Source Control 迁移**

revision `0005_sc_int_delivery`，down revision `0004_sc_secret_reference`。迁移必须：

```sql
ALTER TABLE source_control.source_control_effect
  ADD COLUMN subject_key TEXT,
  ADD COLUMN payload JSONB NOT NULL DEFAULT '{}'::jsonb;
UPDATE source_control.source_control_effect
SET subject_key = 'work-item:' || work_item_id::text
WHERE subject_key IS NULL;
ALTER TABLE source_control.source_control_effect
  ALTER COLUMN subject_key SET NOT NULL,
  ALTER COLUMN work_item_number DROP NOT NULL,
  ALTER COLUMN branch_name DROP NOT NULL,
  ALTER COLUMN base_commit_sha DROP NOT NULL;
```

删除旧 `uq_source_control_effect_work_item` 与 number constraint，新增 `(operation,subject_key)` unique、branch number partial unique 与 operation-shape CHECK。创建：

- `delivery_request_inbox`
- `merge_request_binding`
- `merge_request_observation`

Runtime role 对 Inbox/Effect/Observation 为 SELECT/INSERT/UPDATE，Binding 为 SELECT/INSERT；不授 DELETE，也不授其他 schema 权限。Downgrade 仅在 Integration rows/effects 不存在时恢复旧 shape。

- [ ] **Step 6: 实现 Integration Repository Adapter**

Port 至少提供：accept/claim/complete delivery inbox、effect by operation+subject、insert/transition/claim effect、branch binding query、insert/get MR binding、append/latest observation、pending callbacks。所有 claim 返回 lease attempts，用 CAS fencing 完成。

- [ ] **Step 7: 运行 Task 4 测试**

Run: `uv run pytest tests/source_control/test_integration_domain.py tests/source_control/test_integration_repository.py tests/source_control/test_migration.py tests/source_control/test_repository.py -q`

Expected: PASS，0 skipped，现有 Branch Foundation 测试不回归。

- [ ] **Step 8: 提交 Task 4**

```bash
git add control_plane/app/modules/source_control/domain control_plane/app/modules/source_control/ports control_plane/app/modules/source_control/adapters migrations/source_control tests/source_control
git commit -m "feat(source-control): model integration merge requests"
```

### Task 5: GitLab Projects 与 Merge Requests Adapter

**Files:**
- Create: `control_plane/app/modules/source_control/ports/merge_requests.py`
- Modify: `control_plane/app/modules/source_control/ports/__init__.py`
- Create: `control_plane/app/modules/source_control/adapters/gitlab_merge_requests.py`
- Modify: `control_plane/app/modules/source_control/adapters/__init__.py`
- Create: `tests/source_control/test_gitlab_merge_request_adapter.py`

**Interfaces:**
- Consumes: existing `GitLabRepositoryProfile`、`SecretReferencePort`、HTTPX client/connection ref。
- Produces: `GitLabMergeRequestPort`、`GitLabProjectDeliveryProfile`、`GitLabMergeRequestSnapshot`、`HttpxGitLabMergeRequestAdapter`。

- [ ] **Step 1: 写失败的 Project Profile 与 MR 创建测试**

```python
def test_adapter_lists_then_creates_and_reads_exact_integration_mr() -> None:
    result = run_create_integration_mr(
        adapter,
        repository=_profile(),
        source_branch=TASK_BRANCH,
        expected_head_sha=HEAD_SHA,
        title="feat: integrate WI-42",
        description="Platform-Work-Item: 42",
    )
    assert result.source_branch == TASK_BRANCH
    assert result.target_branch == "dev"
    assert result.head_sha == HEAD_SHA
    assert calls == [
        ("GET", "/projects/platform%2Fbackend"),
        ("GET", "/projects/platform%2Fbackend/repository/branches/dev"),
        ("GET", "/projects/platform%2Fbackend/merge_requests"),
        ("POST", "/projects/platform%2Fbackend/merge_requests"),
        ("GET", "/projects/platform%2Fbackend/merge_requests/17"),
    ]
```

Fixture 对 Project 返回 `path_with_namespace`、`default_branch=main`、`merge_method=merge`；dev branch 返回 `protected=true`。

- [ ] **Step 2: 运行 Adapter 测试确认失败**

Run: `uv run pytest tests/source_control/test_gitlab_merge_request_adapter.py -q`

Expected: collection FAIL，报告 MR Port/Adapter 未定义。

- [ ] **Step 3: 定义稳定 Port DTO**

```python
class GitLabProjectDeliveryProfile(BaseModel):
    model_config = ConfigDict(frozen=True)
    project_id: str
    project_path: str
    default_branch: str
    merge_method: Literal["merge", "rebase_merge", "ff"]


class GitLabMergeRequestSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    project_id: str
    iid: int
    source_branch: str
    target_branch: str
    head_sha: str
    state: Literal["opened", "merged", "closed", "locked"]
    detailed_merge_status: str
    has_conflicts: bool
    blocking_discussions_resolved: bool
    head_pipeline_status: str | None
    merge_commit_sha: str | None
    merge_user_id: str | None
    merged_at: datetime | None
```

Port 方法签名固定为 get profile/branch、list/create/get MR、merge MR(expected SHA)。

- [ ] **Step 4: 实现 decode 与错误归一**

只接受 40–64 hex SHA、正 IID、精确 source/target。401/403 → access denied；404 → project/branch/MR not found；409 SHA mismatch → head changed；405/422 → merge blocked；5xx/timeout/POST 或 PUT 后畸形响应 → result unknown。Exception message 不拼接 response body。

- [ ] **Step 5: 写并通过准确 SHA Merge 测试**

```python
def test_adapter_merges_with_exact_sha_without_squash_or_source_removal() -> None:
    merged = adapter.merge_merge_request(
        _profile(), iid=17, expected_head_sha=HEAD_SHA,
    )
    assert merge_request.url.params["sha"] == HEAD_SHA
    assert merge_request.url.params["squash"] == "false"
    assert merge_request.url.params["should_remove_source_branch"] == "false"
    assert "auto_merge" not in merge_request.url.params
    assert merged.state == "merged"
```

再覆盖 empty `diff_refs`、多个 list 候选、branch protected=false、merge_method!=merge、pipeline/check blocked、401/403/404/405/409/422/5xx/timeout 与 credential 不进 URL/异常。

- [ ] **Step 6: 运行 GitLab Adapter 测试**

Run: `uv run pytest tests/source_control/test_gitlab_adapter.py tests/source_control/test_gitlab_merge_request_adapter.py -q`

Expected: PASS。

- [ ] **Step 7: 提交 Task 5**

```bash
git add control_plane/app/modules/source_control/ports control_plane/app/modules/source_control/adapters tests/source_control/test_gitlab_merge_request_adapter.py
git commit -m "feat(source-control): add GitLab merge request adapter"
```

### Task 6: Requirement Delivery Adapter 与 Source Control relay

**Files:**
- Create: `control_plane/app/modules/source_control/ports/delivery_requirement.py`
- Modify: `control_plane/app/modules/source_control/ports/__init__.py`
- Create: `control_plane/app/modules/source_control/adapters/requirement_delivery.py`
- Modify: `control_plane/app/modules/source_control/adapters/__init__.py`
- Create: `control_plane/app/modules/source_control/application/delivery_relay.py`
- Modify: `control_plane/app/modules/source_control/application/__init__.py`
- Modify: `control_plane/app/modules/source_control/application/dependencies.py`
- Create: `tests/source_control/test_delivery_relay.py`
- Modify: `tests/source_control/test_inter_module_adapters.py`

**Interfaces:**
- Consumes: Task 3 Requirement package-root Facade、Task 4 Delivery Inbox Repository。
- Produces: `RequirementDeliveryPort`、`RequirementFacadeDeliveryAdapter`、`relay_integration_delivery_requests()`。

- [ ] **Step 1: 写失败的跨模块 Adapter 测试**

```python
def test_requirement_delivery_adapter_uses_package_root_facade(monkeypatch) -> None:
    monkeypatch.setattr(requirement, "claim_integration_delivery_requests", fake_claim)
    adapter = RequirementFacadeDeliveryAdapter(engine=requirement_engine, dependencies=req_deps)
    messages = adapter.claim_requests(limit=10, lease_until=LEASE_UNTIL)
    assert messages[0].kind is DeliveryRequestKind.CREATE_MR
    assert messages[0].actor_id == "employee-1"
```

测试文件禁止导入 Requirement 的 application/domain/ports/adapters；只导入包根。

- [ ] **Step 2: 定义 Port 与 result DTO**

```python
class RequirementDeliveryPort(Protocol):
    def claim_requests(self, *, limit: int, lease_until: datetime) -> tuple[DeliveryRequestEnvelope, ...]: ...
    def acknowledge_request(self, message_id: str) -> None: ...
    def release_request(self, message_id: str, *, error_code: str, retry_at: datetime) -> None: ...
    def delivery_context(self, work_item_id: str) -> RequirementDeliveryContext: ...
    def record_mr_ready(self, result: IntegrationMrReadyResult) -> None: ...
    def record_blocked(self, result: IntegrationDeliveryBlockedResult) -> None: ...
    def record_pending(self, result: IntegrationReconciliationPendingResult) -> None: ...
    def record_merged(self, result: IntegrationMergedResult) -> None: ...
    def record_external_merge_drift(self, result: ExternalMergeDriftResult) -> None: ...
```

- [ ] **Step 3: 写失败的 relay 原子性测试**

证明顺序为 Requirement claim → Source Control accept → Requirement ack；Source Control 已接纳但 ack 失败时重复投递只保留一个 Inbox，相同 message ID 不同 payload hash 冲突并 release safe code。

- [ ] **Step 4: 实现 Adapter 与 relay**

`SourceControlDependencies` 增加 `delivery_repository_factory`、`requirement_delivery`、`gitlab_merge_requests` 三个可空依赖；对应命令被调用而依赖为空时 Fail Closed 为 `SourceControlDependencyUnavailable`。

- [ ] **Step 5: 运行跨模块测试和 import-linter**

Run: `uv run pytest tests/source_control/test_delivery_relay.py tests/source_control/test_inter_module_adapters.py -q`

Run: `uv run lint-imports`

Expected: 两条命令 PASS。

- [ ] **Step 6: 提交 Task 6**

```bash
git add control_plane/app/modules/source_control tests/source_control/test_delivery_relay.py tests/source_control/test_inter_module_adapters.py
git commit -m "feat(source-control): relay integration delivery requests"
```

### Task 7: Integration MR 创建 Saga

**Files:**
- Create: `control_plane/app/modules/source_control/application/integration.py`
- Modify: `control_plane/app/modules/source_control/application/__init__.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Create: `tests/source_control/test_integration_mr_saga.py`

**Interfaces:**
- Consumes: Task 4 repository/domain、Task 5 GitLab Port、Task 6 Requirement Port。
- Produces: `process_integration_mr_request(message_id, dependencies) -> ProcessIntegrationRequestResult`。

- [ ] **Step 1: 写失败的创建 MR Happy Path**

```python
def test_create_mr_saga_persists_effect_before_post_and_reads_back(...):
    result = process_integration_mr_request(message_id=MESSAGE_ID, dependencies=dependencies)
    assert result.effect.operation is EffectOperation.CREATE_INTEGRATION_MR
    assert result.effect.state is EffectState.SUCCEEDED
    assert result.binding.source_branch == TASK_BRANCH
    assert result.binding.target_branch == "dev"
    assert result.observation.head_sha == HEAD_SHA
    assert requirement.ready[0].binding_id == result.binding.id
    assert gitlab.calls == ["profile", "source_branch", "dev_branch", "list_mr", "create_mr", "get_mr"]
```

Fake GitLab 的 `create_mr` 前查询数据库，断言 Effect 已是 `IN_FLIGHT`，从而证明外部调用前已提交 Ledger。

- [ ] **Step 2: 运行 Saga 测试确认失败**

Run: `uv run pytest tests/source_control/test_integration_mr_saga.py -q`

Expected: collection FAIL，报告处理函数未定义。

- [ ] **Step 3: 实现准入与确定性模板**

准入重读 Requirement Context、Branch Binding、Workspace Repository、owner eligibility；验证 WorkItem `MR_PENDING`、repository BOUND、branch binding 匹配。标题固定 `"{type}: integrate {workItemId}"`，description 只含稳定 Requirement/WorkItem ID 和 effect marker，不含源码或 secrets。

- [ ] **Step 4: 实现 Create MR Effect 状态机**

`subject_key=work-item:<workItemId>`；先 GET source/dev/profile，再 PLANNED → IN_FLIGHT。LIST 返回：0 个则 POST；1 个精确候选则 `EXTERNAL_ADOPTED`；多于 1 个或 project/source/target 不匹配则 `BLOCKED/MR_CONFLICT`。GET readback 成功后 Binding+Observation+Effect SUCCEEDED 同事务写入，Requirement 回调独立事务执行。

- [ ] **Step 5: 增加不确定与并发测试**

覆盖 POST timeout/5xx/畸形 response → UNKNOWN、同消息重复、两个 worker lease fencing、MR 创建后本地事务失败、回调失败重放、source head 在创建期间变化形成新 Observation、无 delivery commit (`head==base`) blocked。

- [ ] **Step 6: 运行 Task 7 测试**

Run: `uv run pytest tests/source_control/test_integration_mr_saga.py tests/source_control/test_commands.py -q`

Expected: PASS，Branch Saga 不回归。

- [ ] **Step 7: 提交 Task 7**

```bash
git add control_plane/app/modules/source_control/application/integration.py control_plane/app/modules/source_control/application/__init__.py control_plane/app/modules/source_control/__init__.py tests/source_control/test_integration_mr_saga.py
git commit -m "feat(source-control): create integration merge requests"
```

### Task 8: 准确 SHA Integration Merge Saga

**Files:**
- Modify: `control_plane/app/modules/source_control/application/integration.py`
- Modify: `control_plane/app/modules/source_control/application/__init__.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Create: `tests/source_control/test_integration_merge_saga.py`

**Interfaces:**
- Consumes: Task 7 MR Binding/Observation 与 Task 5 exact-SHA merge method。
- Produces: `process_integration_merge_request(message_id, dependencies) -> ProcessIntegrationRequestResult`。

- [ ] **Step 1: 写失败的受保护 Merge Happy Path**

```python
def test_merge_saga_uses_current_exact_sha_and_preserves_source_branch(...):
    result = process_integration_merge_request(message_id=MERGE_MESSAGE_ID, dependencies=dependencies)
    assert gitlab.merge_calls == [(MR_IID, HEAD_SHA, False, False)]
    assert result.observation.state is MergeRequestState.MERGED
    assert result.observation.merge_commit_sha == MERGE_COMMIT_SHA
    assert gitlab.get_branch(TASK_BRANCH).commit_sha == HEAD_SHA
    assert requirement.merged[0].binding_id == MR_BINDING_ID
```

- [ ] **Step 2: 运行 Merge 测试确认失败**

Run: `uv run pytest tests/source_control/test_integration_merge_saga.py -q`

Expected: collection FAIL，报告 Merge 处理函数未定义。

- [ ] **Step 3: 实现 Merge 前实时校验**

Context 必须为 `VERIFYING/MERGE_PENDING`，binding ID 一致，actor 当前具备 merge eligibility。GET MR/source/dev/profile 后校验：MR open、project/source/target 匹配、MR SHA==source HEAD==请求读取的准确 SHA、dev protected、merge method=merge、无 conflict、discussions resolved、Project Policy 要求的 pipeline/check 已满足。

- [ ] **Step 4: 实现 MERGE Effect 与回读**

`subject_key=mr:<bindingId>:<headSha>`。提交 IN_FLIGHT 后调用 PUT merge，始终传 SHA、`squash=false`、`should_remove_source_branch=false`。GET MR 证明 merged、merge commit SHA 和 mergedAt；GET source branch 证明仍存在，才 SUCCEEDED 并回调 INTEGRATED。

- [ ] **Step 5: 增加失败与 drift 测试**

覆盖 head stale、checks blocked、merge conflict、MR closed、405/409/422、PUT timeout/5xx unknown、merge 已发生但 source branch 缺失、无有效 Merge Effect 却已 merged 的 `EXTERNAL_MERGE_DRIFT`。已发生 merge 必须保存 Observation，不能回滚或删除记录。

- [ ] **Step 6: 运行 Task 8 测试**

Run: `uv run pytest tests/source_control/test_integration_merge_saga.py tests/source_control/test_integration_mr_saga.py -q`

Expected: PASS。

- [ ] **Step 7: 提交 Task 8**

```bash
git add control_plane/app/modules/source_control/application/integration.py control_plane/app/modules/source_control/application/__init__.py control_plane/app/modules/source_control/__init__.py tests/source_control/test_integration_merge_saga.py
git commit -m "feat(source-control): merge integration MRs at exact SHA"
```

### Task 9: MR Reconciliation、Webhook 摘要与 fencing

**Files:**
- Create: `control_plane/app/modules/source_control/application/integration_reconciliation.py`
- Modify: `control_plane/app/modules/source_control/application/reconciliation.py`
- Modify: `control_plane/app/modules/source_control/application/webhooks.py`
- Modify: `control_plane/app/modules/source_control/api/webhooks.py`
- Modify: `control_plane/app/modules/source_control/domain/models.py`
- Modify: `control_plane/app/modules/source_control/adapters/sqlalchemy.py`
- Modify: `control_plane/app/modules/source_control/adapters/integration_sqlalchemy.py`
- Modify: `control_plane/app/modules/source_control/__init__.py`
- Create: `tests/source_control/test_integration_reconciliation.py`
- Modify: `tests/source_control/test_webhooks.py`
- Modify: `tests/source_control/test_reconciliation.py`

**Interfaces:**
- Consumes: Task 7/8 UNKNOWN effects与 existing signed Webhook Inbox。
- Produces: `reconcile_due_integration_effects()`、MR event safe envelope、pending callback replay。

- [ ] **Step 1: 写失败的 Create/Merge UNKNOWN 收敛测试**

```python
def test_unknown_create_reconciles_unique_existing_mr_without_second_post(...):
    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)
    assert result.effects[0].state is EffectState.SUCCEEDED
    assert gitlab.create_calls == 0
    assert repository.binding_by_work_item(WORK_ITEM_ID) is not None


def test_unknown_merge_reconciles_only_exact_merged_head(...):
    result = reconcile_due_integration_effects(limit=10, dependencies=dependencies)
    assert result.effects[0].state is EffectState.SUCCEEDED
    assert result.observations[0].head_sha == REQUESTED_HEAD_SHA
    assert result.observations[0].merge_commit_sha == MERGE_COMMIT_SHA
```

- [ ] **Step 2: 实现 operation dispatch 与 lease fencing**

领取 UNKNOWN 时原子转 `RECONCILIATION` 并增加 attempts；完成时必须同时匹配 effect ID、expected state 和 claimed attempts。旧 worker 结果为 no-op/lease lost，不得完成新 lease。

- [ ] **Step 3: 实现收敛矩阵**

Create：唯一兼容 MR → bind；多个 → conflict；无 MR → 同 Effect 再创建；查询未知 → UNKNOWN。Merge：exact head merged + commit + source exists → success；open → 同 Effect 重试 merge；closed/head changed → blocked；source missing → 保存 merged Observation 后 blocked；无平台 Effect的 merged Observation → external drift callback。

- [ ] **Step 4: 写失败的 MR Webhook 安全摘要测试**

```python
def test_signed_merge_request_webhook_only_makes_matching_effect_due(...):
    body = mr_webhook_body(action="merge", iid=17, source=TASK_BRANCH,
                           target="dev", head_sha=HEAD_SHA)
    accepted = client.post(webhook_url, content=body, headers=_headers(body))
    assert accepted.status_code == 202
    process_webhook_inbox(inbox_id=accepted.json()["inboxId"], dependencies=dependencies)
    assert effect.next_reconcile_at == NOW
    assert repository.latest_observation(MR_BINDING_ID) is None
```

再断言 `changes={}`、乱序 update/merge、未知 IID、错 project、旧 Standard Webhook 签名拒绝规则、同 webhook ID 不同 digest 冲突。

- [ ] **Step 5: 扩展 Webhook Envelope 与解析**

只保存 `mr_iid`、`mr_action`、`source_branch`、`target_branch`、`mr_state`、`old_head_sha`、`head_sha` 与 digest；不保存完整 body 或 user object。Webhook 处理仅调用 `make_integration_effect_due()`。

- [ ] **Step 6: 运行 Task 9 测试**

Run: `uv run pytest tests/source_control/test_integration_reconciliation.py tests/source_control/test_reconciliation.py tests/source_control/test_webhooks.py -q`

Expected: PASS。

- [ ] **Step 7: 提交 Task 9**

```bash
git add control_plane/app/modules/source_control tests/source_control/test_integration_reconciliation.py tests/source_control/test_reconciliation.py tests/source_control/test_webhooks.py
git commit -m "feat(source-control): reconcile integration MR effects"
```

### Task 10: Worker 装配、PostgreSQL E2E、OpenAPI 与全量门禁

**Files:**
- Modify: `control_plane/tools/source_control_worker.py`
- Modify: `control_plane/app/bootstrap/source_control_connector.py`
- Modify: `control_plane/app/bootstrap/app.py`
- Modify: `tests/source_control/test_e2e.py`
- Create: `tests/source_control/test_integration_e2e.py`
- Modify: `tests/test_openapi_export.py`
- Modify: `openapi.json`

**Interfaces:**
- Consumes: Tasks 1–9 public Facades and worker functions。
- Produces: end-to-end human Integration MR backend slice and versioned OpenAPI artifact。

- [ ] **Step 1: 写失败的 worker dispatch 测试**

证明 `relay` 同时接纳 Binding 与 Delivery Outbox；`process` 在同一 limit 内公平处理 branch inbox、delivery inbox、webhook inbox；`reconcile` 同时处理 branch/MR effects；报告只含 IDs 与 allowlist error codes。

- [ ] **Step 2: 实现 worker/connector 装配**

扩展 `WorkerRunReport` 的现有字段，不打印 Provider body。生产依赖未装配时继续明确 Fail Closed；不把 Webhook route 挂到浏览器业务 app。Requirement 三个业务 endpoints 只挂现有 Requirement router。

- [ ] **Step 3: 写 PostgreSQL 跨模块 E2E**

E2E 使用真实 Requirement/Source Control schema 和 runtime roles、Fake GitLab、Fake eligibility：

```python
start_work_item → request_integration_merge_request
→ relay_integration_delivery_requests → process_integration_mr_request
→ request_integration_merge
→ relay_integration_delivery_requests → process_integration_merge_request
```

最终断言 Requirement `VERIFYING/INTEGRATED`、Source Control 一个 MR Binding、OPEN+MERGED Observations、两个 Integration Effects SUCCEEDED、source branch 仍存在、Audit correlation 完整。使用 `source_control_rw` 尝试 INSERT Requirement 表必须 DBAPIError，反向写 Source Control 也必须失败。

- [ ] **Step 4: 写并通过 OpenAPI 回归测试**

断言三个 operation ID、202/200 status、`Idempotency-Key`、`If-Match`、ETag、Capability security guard、枚举扩展与 DTO 无 GitLab/provider 私有字段。运行：

Run: `uv run python scripts/export_openapi.py`

Run: `uv run python scripts/export_openapi.py --check`

Expected: 两条命令 PASS，`openapi.json` 只有预期新增契约。

- [ ] **Step 5: 运行 focused integration suite**

Run: `uv run pytest tests/requirement tests/source_control tests/authorization/test_openapi_security.py tests/test_openapi_export.py -q`

Expected: PASS，0 skipped。

- [ ] **Step 6: 运行格式、类型与架构门禁**

Run: `uv run ruff format --check .`

Run: `uv run ruff check .`

Run: `uv run mypy .`

Run: `uv run lint-imports`

Expected: 全部 exit 0。

- [ ] **Step 7: 运行迁移与全量测试门禁**

Run: `uv run alembic upgrade heads`

Run: `uv run pytest -v`

Run: `uv run python scripts/export_openapi.py --check`

Expected: 全部 exit 0；pytest summary 无 skipped/failed/error。

- [ ] **Step 8: 检查范围与 secret 泄漏**

Run: `git diff --check origin/main...HEAD`

Run: `git diff --name-only origin/main...HEAD`

Run: `rg -n "glpat-|PRIVATE-TOKEN.*[A-Za-z0-9]|whsec_|password=" control_plane migrations tests docs/superpowers openapi.json`

Expected: diff 只包含本规格、计划与上述任务明确列出的实现/测试/生成构件；不得出现范围外文件。secret scan 不出现真实凭据，测试 sentinel 只能是显式 test-only 值。

- [ ] **Step 9: 提交 Task 10**

```bash
git add control_plane/app/bootstrap control_plane/tools control_plane/app/modules/requirement/api control_plane/app/modules/source_control tests/source_control tests/test_openapi_export.py openapi.json
git commit -m "feat(source-control): complete human integration MR flow"
```

- [ ] **Step 10: 固定 HEAD 复核**

记录 `git rev-parse HEAD`，重新运行 Task 10 Step 6–8；后续 review、PR 与 CI 必须针对该精确 SHA。测试通过不代表自动授权 merge、tag、release 或分支清理。

## Completion Evidence

完成实现时必须交付：

- 规格 commit、计划 commit、每个 TDD task commit 与最终固定 HEAD。
- Focused tests、全量 pytest 数量、Ruff、mypy、import-linter、Alembic heads、OpenAPI check 原始摘要。
- Requirement/Source Control 双向数据库拒绝测试。
- GitLab Fake Adapter 的 create/readback、exact-SHA merge、UNKNOWN/Reconciliation、Webhook 与 drift 证据。
- PR CI 与合并后独立 main push CI；二者是不同 Gate。
- 真实 GitLab Smoke 标记为 `NOT RUN / GitOps prerequisites pending`，不得写成已通过。
