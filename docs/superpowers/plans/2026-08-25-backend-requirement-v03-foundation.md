# V0.3 Requirement 领域基础实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在后端建立 V0.3 第一批 Requirement 深模块，使用真实 PostgreSQL 持久化 Requirement、首个 WorkItem、SDD Baseline Gate、Assignment 与 Decision，并跑通 `CREATED → PREPARING → AWAITING_CONFIRMATION → READY` 的人工基线确认链。

**Architecture:** 新模块遵守 `api → adapters → application → ports → domain` 五层和包根 Facade 规则。领域层拥有状态机与不可变量，Application 层拥有命令编排和事务内 Audit/Outbox，SQLAlchemy Adapter 只实现持久化 Port；浏览器 API 只接收用户可声明字段，Artifact、Policy、Assignment 资格与 Repository Binding 状态均由受控 Port 或内部命令提供。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy Core、PostgreSQL 18、Alembic、pytest、mypy strict、Ruff、import-linter。

**Design spec:** `docs/superpowers/specs/2026-08-25-backend-requirement-v03-foundation-design.md`

---

## 执行前置与全局约束

- 在架构仓先登记 V0.3 开发与 V0.1/V0.2 验收重叠的治理例外；登记前不在成员仓引用新的 `DEV-xxx`。
- 本计划只实现第一批领域底座，不实现 GitLab 协议、对象上传/扫描、Chat/Model、MR、Acceptance 或前端页面。
- 每个实现步骤严格按 `RED → GREEN → REFACTOR`：先运行新增测试并确认按预期失败，再写最小实现，再运行定向测试。
- 每个写命令都必须在同一数据库事务内保存业务事实、Audit 和必要的 Outbox；不得用内存队列替代可靠记录。
- `CREATED` 必须可读取；只有初始 Repository Binding 请求已可靠入队后，内部命令才能进入 `PREPARING`。
- Requirement 的基线批准可以进入 `READY`，不以首个 WorkItem 已分配或 Repository 已绑定为前提；WorkItem 自身只有 `ASSIGNED + BOUND` 时才进入 `READY`。
- 未收到 `【同步进度】`，不得修改 `docs/superpowers/progress/current.md`。
- 不手改 `openapi.json`；只通过应用导出脚本生成。

## File Map

- Create: `control_plane/app/modules/requirement/{api,application,domain,ports,adapters}/__init__.py`
- Create: `control_plane/app/modules/requirement/domain/models.py`
- Create: `control_plane/app/modules/requirement/domain/transitions.py`
- Create: `control_plane/app/modules/requirement/application/{dependencies,commands,queries,common}.py`
- Create: `control_plane/app/modules/requirement/ports/{repository,runtime,artifact,policy}.py`
- Create: `control_plane/app/modules/requirement/adapters/sqlalchemy.py`
- Create: `control_plane/app/modules/requirement/api/{dto,routes}.py`
- Create: `migrations/requirement/0001_requirement_base.py`
- Create: `migrations/authorization/0006_authorization_v03_requirement_routes.py`
- Modify: `control_plane/app/bootstrap/app.py`
- Modify: `control_plane/app/shared/db/settings.py`
- Modify: `pyproject.toml`
- Modify (generated): `openapi.json`
- Create: `tests/requirement/{__init__,conftest,helpers,test_domain,test_commands,test_queries,test_migration,test_api}.py`
- Modify: `tests/authorization/test_migration.py`
- Modify: `tests/test_contract_guard.py`
- Modify: `tests/test_openapi_export.py`
- Modify: `tests/test_e2e_access_governance.py`

### Task 1: 建立 Requirement 模块边界和纯领域状态机

**Files:**

- Create: `control_plane/app/modules/requirement/domain/models.py`
- Create: `control_plane/app/modules/requirement/domain/transitions.py`
- Create: `control_plane/app/modules/requirement/domain/__init__.py`
- Create: `control_plane/app/modules/requirement/{api,application,ports,adapters}/__init__.py`
- Create: `control_plane/app/modules/requirement/__init__.py`
- Modify: `pyproject.toml`
- Create: `tests/requirement/test_domain.py`
- Modify: `tests/test_contract_guard.py`

**Interfaces:**

- Produces: `RequirementType`、`RequirementState`、`RecordState`、`WorkItemState`、`AssignmentState`、`RepositoryState`、`DecisionOutcome`。
- Produces: frozen `RequirementDto`、`WorkItemDto`、`SddBaselineDto`、`GateInstanceDto`、`GateAssignmentDto`、`DecisionDto`。
- Produces: `transition_requirement(...)`、`derive_work_item_state(...)` 和确定性领域错误。

- [x] **Step 1: 写状态枚举、冻结 DTO 和拒绝非法转换的失败测试**

在 `tests/requirement/test_domain.py` 覆盖完整首批矩阵：

```python
@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RequirementState.CREATED, RequirementState.PREPARING),
        (RequirementState.PREPARING, RequirementState.AWAITING_CONFIRMATION),
        (RequirementState.AWAITING_CONFIRMATION, RequirementState.READY),
        (RequirementState.AWAITING_CONFIRMATION, RequirementState.PREPARING),
        (RequirementState.AWAITING_CONFIRMATION, RequirementState.CANCELED),
    ],
)
def test_requirement_allows_first_batch_transitions(current, target):
    assert transition_requirement(current, target) is target

def test_requirement_rejects_ready_to_preparing():
    with pytest.raises(InvalidRequirementTransition):
        transition_requirement(RequirementState.READY, RequirementState.PREPARING)

def test_work_item_is_ready_only_when_assigned_and_bound():
    assert derive_work_item_state(AssignmentState.ASSIGNED, RepositoryState.BOUND) \
        is WorkItemState.READY
    assert derive_work_item_state(AssignmentState.UNASSIGNED, RepositoryState.BOUND) \
        is WorkItemState.DRAFT
    assert derive_work_item_state(
        AssignmentState.ASSIGNED,
        RepositoryState.WAITING_REPOSITORY,
    ) is WorkItemState.DRAFT
```

- [x] **Step 2: 运行测试确认 RED**

Run: `python -m pytest tests/requirement/test_domain.py -q`

Expected: collection 因 `control_plane.app.modules.requirement` 不存在而失败。

- [x] **Step 3: 写最小领域模型和显式转换表**

核心实现固定为显式表，不使用字符串前缀或任意状态回退：

```python
_FIRST_BATCH_TRANSITIONS = {
    RequirementState.CREATED: {RequirementState.PREPARING, RequirementState.CANCELED},
    RequirementState.PREPARING: {
        RequirementState.AWAITING_CONFIRMATION,
        RequirementState.CANCELED,
    },
    RequirementState.AWAITING_CONFIRMATION: {
        RequirementState.READY,
        RequirementState.PREPARING,
        RequirementState.CANCELED,
    },
    RequirementState.READY: {RequirementState.CANCELED},
    RequirementState.CANCELED: set(),
}

def transition_requirement(current: RequirementState, target: RequirementState) -> RequirementState:
    if target not in _FIRST_BATCH_TRANSITIONS[current]:
        raise InvalidRequirementTransition(f"{current.value}->{target.value}")
    return target
```

- [x] **Step 4: 加入 import-linter 容器和深层导入契约**

将 `control_plane.app.modules.requirement` 加入 layers `containers`、domain/shared API 契约，并让既有模块和 Requirement 互相只能经包根 Facade 使用。

- [x] **Step 5: 运行领域与架构测试确认 GREEN**

Run: `python -m pytest tests/requirement/test_domain.py tests/test_contract_guard.py -q`

Expected: PASS。

- [x] **Step 6: 提交领域骨架**

```bash
git add control_plane/app/modules/requirement pyproject.toml tests/requirement tests/test_contract_guard.py
git commit -m "feat(requirement): establish domain state model"
```

### Task 2: 建立独立 schema、最小权限和 SQL Repository

**Files:**

- Create: `migrations/requirement/0001_requirement_base.py`
- Create: `control_plane/app/modules/requirement/ports/repository.py`
- Create: `control_plane/app/modules/requirement/ports/runtime.py`
- Create: `control_plane/app/modules/requirement/adapters/sqlalchemy.py`
- Modify: `control_plane/app/shared/db/settings.py`
- Create: `tests/requirement/conftest.py`
- Create: `tests/requirement/helpers.py`
- Create: `tests/requirement/test_migration.py`

**Interfaces:**

- Produces: independent Alembic head `0001_requirement_base` with branch label `requirement`。
- Produces: `RequirementRepository` / `RequirementRepositoryFactory` Protocol。
- Produces: `SqlAlchemyRequirementRepository`，只访问 `requirement` schema 与受控 Audit append。

- [ ] **Step 1: 写 migration 生命周期和权限失败测试**

测试必须验证八张表、所有 CHECK/FK/unique 约束、`requirement_rw` 的最小权限，以及 downgrade 后其他 schema 事实仍存在：

```python
EXPECTED_TABLES = {
    "requirement",
    "work_item",
    "sdd_baseline",
    "gate_instance",
    "gate_assignment",
    "decision",
    "idempotency_record",
    "outbox_message",
}

assert table_names(owner_engine, "requirement") == EXPECTED_TABLES
assert privileges(owner_engine, "requirement_rw", "requirement") == {
    "requirement": {"SELECT", "INSERT", "UPDATE"},
    "work_item": {"SELECT", "INSERT", "UPDATE"},
    "sdd_baseline": {"SELECT", "INSERT"},
    "gate_instance": {"SELECT", "INSERT"},
    "gate_assignment": {"SELECT", "INSERT", "UPDATE"},
    "decision": {"SELECT", "INSERT"},
    "idempotency_record": {"SELECT", "INSERT", "UPDATE"},
    "outbox_message": {"SELECT", "INSERT", "UPDATE"},
}
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest tests/requirement/test_migration.py -q`

Expected: 缺少 `requirement` migration/head。

- [ ] **Step 3: 实现 schema 与数据库约束**

迁移关键约束：

```sql
CONSTRAINT ck_requirement_type CHECK (type IN ('feat', 'fix', 'refactor', 'chore')),
CONSTRAINT ck_requirement_state CHECK (
  state IN ('CREATED', 'PREPARING', 'AWAITING_CONFIRMATION', 'READY', 'CANCELED')
),
CONSTRAINT ck_requirement_record_state CHECK (record_state = 'ACTIVE'),
CONSTRAINT ck_work_item_executor CHECK (executor_type = 'HUMAN'),
CONSTRAINT uq_requirement_gate_assignment UNIQUE (gate_instance_id, revision),
CONSTRAINT uq_requirement_decision_gate UNIQUE (gate_instance_id)
```

`outbox_message` 保存 `topic`、`aggregate_type/id/version`、`payload JSONB`、`state=PENDING|PUBLISHED|FAILED`、重试时间与错误码；不得保存凭据或完整 SDD 正文。

- [ ] **Step 4: 写 Repository Protocol 与 SQLAlchemy Adapter**

Protocol 使用领域词汇，不暴露 SQL：

```python
class RequirementRepository(Protocol):
    db: Connection
    def insert_requirement(self, **values: object) -> Any: ...
    def requirement_by_id(self, requirement_id: str, *, for_update: bool = False) -> Any: ...
    def list_requirements(self, *, workspace_id: str, after_id: str | None, limit: int) -> list[Any]: ...
    def insert_work_item(self, **values: object) -> Any: ...
    def insert_outbox(self, **values: object) -> Any: ...
    def update_requirement_state(self, requirement_id: str, *, expected_revision: int, state: str) -> Any: ...
    def insert_sdd_baseline(self, **values: object) -> Any: ...
    def insert_gate(self, **values: object) -> Any: ...
    def insert_assignment(self, **values: object) -> Any: ...
    def insert_decision(self, **values: object) -> Any: ...
```

- [ ] **Step 5: 运行 migration 测试与 Alembic head 检查**

Run: `python -m pytest tests/requirement/test_migration.py tests/integration/test_migration.py -q`

Run: `python -m alembic heads`

Expected: 新增且仅新增 `0001_requirement_base (requirement) (head)`，所有测试 PASS。

- [ ] **Step 6: 提交持久化基础**

```bash
git add migrations/requirement control_plane/app/modules/requirement/ports control_plane/app/modules/requirement/adapters control_plane/app/shared/db/settings.py tests/requirement
git commit -m "feat(requirement): persist requirement foundations"
```

### Task 3: 以单事务创建 Requirement、首个 WorkItem 和 Binding Outbox

**Files:**

- Create: `control_plane/app/modules/requirement/application/dependencies.py`
- Create: `control_plane/app/modules/requirement/application/common.py`
- Create: `control_plane/app/modules/requirement/application/commands.py`
- Modify: `control_plane/app/modules/requirement/application/__init__.py`
- Modify: `control_plane/app/modules/requirement/__init__.py`
- Create: `tests/requirement/test_commands.py`

**Interfaces:**

- Produces: `RequirementDependencies`，注入 repository、clock、random、audit、assignment guard 与 route snapshot provider。
- Produces: `create_requirement(...)` 与 `start_requirement_preparation(...)` Facade。
- Consumes: current actor、Workspace ID、initial Repository ID 和创建输入；不接收客户端提供的状态/版本/hash。

- [ ] **Step 1: 写创建原子性、负责人解析和 Outbox 的失败测试**

关键断言：

```python
created = create_requirement(
    db,
    workspace_id=WORKSPACE_ID,
    requirement_type=RequirementType.FEAT,
    title="Add governed delivery",
    description="Create the first manual delivery flow",
    acceptance_criteria=("baseline is approved",),
    initial_repository_id=REPOSITORY_ID,
    actor=principal,
    idempotency_key="requirement-create-0001",
    dependencies=dependencies,
)
assert created.requirement.state is RequirementState.CREATED
assert created.work_item.state is WorkItemState.DRAFT
assert created.work_item.repository_state is RepositoryState.WAITING_REPOSITORY
assert created.work_item.executor_type is ExecutorType.HUMAN
assert repository.pending_topics() == ["requirement.repository-binding.requested"]
```

另测资格无效时 `UNASSIGNED`、相同 key 重放返回同一结果、相同 key 不同 payload 冲突，以及 Outbox/Audit 任一步抛错时事实全部回滚。

- [ ] **Step 2: 运行命令测试确认 RED**

Run: `python -m pytest tests/requirement/test_commands.py -q`

Expected: 缺少 Application 命令。

- [ ] **Step 3: 实现创建命令和稳定 fingerprint**

创建命令生成确定性 payload fingerprint，只保存必要元数据：

```python
payload = {
    "workspaceId": workspace_id,
    "type": requirement_type.value,
    "title": title.strip(),
    "description": description.strip(),
    "acceptanceCriteria": list(acceptance_criteria),
    "initialRepositoryId": initial_repository_id,
}
fingerprint = sha256(canonical_json(payload)).hexdigest()
```

同一事务依次 claim idempotency、插入 Requirement、首个 WorkItem、Binding request Outbox、Audit、sealed result；任何异常由上层 `engine.begin()` 回滚。

- [ ] **Step 4: 实现内部 preparation 命令**

`start_requirement_preparation` 锁定 Requirement，确认相同 aggregate/version 的 Binding request 已存在，再以 CAS 推进：

```python
if not repository.has_binding_request(requirement_id, requirement_version):
    raise RepositoryBindingRequestMissing(requirement_id)
transition_requirement(RequirementState(row["state"]), RequirementState.PREPARING)
updated = repository.update_requirement_state(
    requirement_id,
    expected_revision=expected_revision,
    state=RequirementState.PREPARING.value,
)
```

- [ ] **Step 5: 运行命令、回滚和并发测试确认 GREEN**

Run: `python -m pytest tests/requirement/test_commands.py -q`

Expected: PASS；同 key 并发只产生一组事实和一条 Outbox。

- [ ] **Step 6: 提交创建链**

```bash
git add control_plane/app/modules/requirement/application control_plane/app/modules/requirement/__init__.py tests/requirement/test_commands.py
git commit -m "feat(requirement): create governed requirements"
```

### Task 4: 实现详情、Workspace cursor 列表和 Repository Binding 内部回传

**Files:**

- Create: `control_plane/app/modules/requirement/application/queries.py`
- Modify: `control_plane/app/modules/requirement/application/commands.py`
- Modify: `control_plane/app/modules/requirement/__init__.py`
- Modify: `control_plane/app/modules/requirement/ports/repository.py`
- Modify: `control_plane/app/modules/requirement/adapters/sqlalchemy.py`
- Create: `tests/requirement/test_queries.py`
- Modify: `tests/requirement/test_commands.py`

**Interfaces:**

- Produces: `get_requirement(...)`、`list_requirements(...)`、`record_repository_binding(...)`。
- Cursor: base64url 编码的不可解释 `{createdAt,id}` 游标；查询固定 `ORDER BY created_at, id`。
- Internal binding command: 只接受受控 Adapter identity，不暴露 HTTP route。

- [ ] **Step 1: 写 Workspace 隔离、稳定 cursor 和绑定状态失败测试**

覆盖其他 Workspace 不可见、无效 cursor Fail Closed、相同时间按 ID 稳定翻页、重复 Binding 回传幂等、冲突 base commit/branch 拒绝。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest tests/requirement/test_queries.py tests/requirement/test_commands.py -q`

- [ ] **Step 3: 实现查询和内部绑定回传**

Binding Ready 保存不可变 `repositoryId/baseCommitSha/taskBranch`；只有负责人有效时 WorkItem 才进入 READY：

```python
work_item_state = derive_work_item_state(
    AssignmentState(row["assignment_state"]),
    RepositoryState.BOUND,
)
repository.bind_work_item(
    work_item_id,
    expected_revision=expected_revision,
    base_commit_sha=base_commit_sha,
    task_branch=task_branch,
    state=work_item_state.value,
)
```

- [ ] **Step 4: 运行测试确认 GREEN 并提交**

Run: `python -m pytest tests/requirement/test_queries.py tests/requirement/test_commands.py -q`

```bash
git add control_plane/app/modules/requirement tests/requirement
git commit -m "feat(requirement): query and bind work items"
```

### Task 5: 绑定 SDD Artifact、创建 Gate 并追加人工 Decision

**Files:**

- Create: `control_plane/app/modules/requirement/ports/artifact.py`
- Create: `control_plane/app/modules/requirement/ports/policy.py`
- Modify: `control_plane/app/modules/requirement/application/dependencies.py`
- Modify: `control_plane/app/modules/requirement/application/commands.py`
- Modify: `control_plane/app/modules/requirement/{domain,__init__}.py`
- Modify: `control_plane/app/modules/requirement/{ports,adapters}/...`
- Create: `tests/requirement/test_baseline_gate.py`

**Interfaces:**

- Consumes: `ArtifactSnapshot(id, version, sha256, state=AVAILABLE, media_type, trust)`。
- Consumes: `GatePolicySnapshot(version, default_reviewer_id)`。
- Produces: `register_sdd_baseline`、`submit_baseline_confirmation`、`decide_baseline`。

- [ ] **Step 1: 写 Artifact/subject/assignee/CAS 的失败测试**

必须覆盖 unavailable/untrusted Artifact、Artifact hash 变化、过期 Requirement revision、非当前 reviewer、决策时资格失效、Decision 重复和新 Artifact 新 Gate。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest tests/requirement/test_baseline_gate.py -q`

- [ ] **Step 3: 实现 baseline 与 Gate 精确快照**

Gate 插入字段必须完整：

```python
gate = repository.insert_gate(
    id=gate_id,
    gate_type="REQUIREMENT_BASELINE_CONFIRMATION",
    requirement_id=requirement_id,
    requirement_version=requirement.requirement_version,
    artifact_id=artifact.id,
    artifact_version=artifact.version,
    artifact_hash=artifact.sha256,
    route_snapshot_version=requirement.route_snapshot_version,
    route_snapshot_hash=requirement.route_snapshot_hash,
    policy_version=policy.version,
)
```

- [ ] **Step 4: 实现追加式 Decision 与状态推进**

结论语义：

```python
target = {
    DecisionOutcome.APPROVED: RequirementState.READY,
    DecisionOutcome.CHANGES_REQUESTED: RequirementState.PREPARING,
    DecisionOutcome.REJECTED: RequirementState.CANCELED,
}[outcome]
```

插入 Decision 与 Requirement CAS 更新在同一事务中；旧 Decision 永不更新或删除。

- [ ] **Step 5: 运行测试确认 GREEN 并提交**

Run: `python -m pytest tests/requirement/test_baseline_gate.py tests/requirement/test_commands.py -q`

```bash
git add control_plane/app/modules/requirement tests/requirement
git commit -m "feat(requirement): govern sdd baseline decisions"
```

### Task 6: 接入显式 Capability、HTTP Contract 和生产 Fail-Closed 装配

**Files:**

- Create: `migrations/authorization/0006_authorization_v03_requirement_routes.py`
- Modify: `tests/authorization/test_migration.py`
- Create: `control_plane/app/modules/requirement/api/dto.py`
- Create: `control_plane/app/modules/requirement/api/routes.py`
- Modify: `control_plane/app/modules/requirement/api/__init__.py`
- Modify: `control_plane/app/bootstrap/app.py`
- Modify: `control_plane/app/shared/db/settings.py`
- Create: `tests/requirement/test_api.py`

**Interfaces:**

- Capabilities: `requirement.create`、`requirement.read`、`requirement.baseline.submit`、`requirement.baseline.decide`、`work_item.assign`。
- HTTP: 六个设计内 `/api/v1/requirements` endpoints。
- Production adapters: Artifact/Policy/Binding 尚未配置时返回依赖不可用 Problem，不以 Fake 或静态成功代替。

- [ ] **Step 1: 写路由授权与 HTTP 合同失败测试**

测试 camelCase、`{items,nextCursor}`、ETag、缺少/错误 `If-Match`、缺少 `Idempotency-Key`、RFC 9457、未知 Capability 和跨 Workspace 拒绝。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest tests/requirement/test_api.py tests/authorization/test_migration.py -q`

- [ ] **Step 3: 注册导航路由和显式 Capability**

只登记 V0.3 Requirement 页面入口；不把 V0.3 能力加入 V0.2 Super Admin 自动集合：

```sql
('requirements', 'requirement.read', 'WORKSPACE', 20,
 jsonb_build_object('name', 'Requirements', 'order', 20))
```

- [ ] **Step 4: 实现 DTO/route 和 Problem 映射**

浏览器创建体仅包含：

```python
class CreateRequirementRequestDto(CamelModel):
    workspace_id: str = Field(min_length=1)
    type: RequirementType
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)
    acceptance_criteria: list[str] = Field(min_length=1)
    initial_repository_id: str = Field(min_length=1)
```

不得接收 `state`、`createdBy`、Route hash、Policy version、Artifact availability 或 assignee 资格。

- [ ] **Step 5: 在 bootstrap 建立 runtime engine/dependencies/router**

新增 `requirement_database_url` 和 `requirement_runtime_engine()`；未配置真实 Artifact/Policy Adapter 的命令由明确 unavailable Port Fail Closed，但创建/读取和内部 preparation 可运行。

- [ ] **Step 6: 运行 API、授权、OpenAPI 测试确认 GREEN 并提交**

Run: `python -m pytest tests/requirement/test_api.py tests/authorization/test_migration.py tests/test_openapi_export.py -q`

```bash
git add migrations/authorization control_plane/app/modules/requirement control_plane/app/bootstrap/app.py control_plane/app/shared/db/settings.py tests/requirement tests/authorization/test_migration.py
git commit -m "feat(requirement): expose governed requirement api"
```

### Task 7: 真实 PostgreSQL 端到端、OpenAPI 生成与完整门禁

**Files:**

- Create: `tests/requirement/test_e2e.py`
- Modify: `tests/test_e2e_access_governance.py`
- Modify: `tests/test_openapi_export.py`
- Modify (generated): `openapi.json`

- [ ] **Step 1: 写完整 Happy Path 与拒绝路径 E2E**

Happy Path 必须从 HTTP 创建开始，并用真实内部 Application 命令模拟可靠 Worker/受控 Adapter 回传：

```text
POST Requirement
→ GET state=CREATED + WorkItem DRAFT/WAITING_REPOSITORY
→ internal start preparation
→ register AVAILABLE SDD snapshot
→ submit baseline gate
→ current human reviewer APPROVED
→ GET Requirement state=READY
→ WorkItem 仍 DRAFT（直到 ASSIGNED + BOUND）
```

拒绝路径覆盖 `CHANGES_REQUESTED → PREPARING`、`REJECTED → CANCELED`、资格失效和 subject hash/revision 冲突。

- [ ] **Step 2: 运行 E2E 确认 RED，补齐最小缺口后确认 GREEN**

Run: `python -m pytest tests/requirement/test_e2e.py -q`

- [ ] **Step 3: 生成并验证 OpenAPI**

Run: `python scripts/export_openapi.py`

Run: `python -m pytest tests/test_openapi_export.py tests/requirement/test_api.py -q`

- [ ] **Step 4: 运行静态和结构门禁**

Run: `python -m ruff check control_plane migrations tests scripts`

Run: `python -m mypy control_plane migrations scripts tests`

Run: `python -m lint_imports`

Run: `python scripts/check_migrations.py`

- [ ] **Step 5: 运行完整 PostgreSQL 测试**

Run: `python -m pytest --basetemp .pytest-basetemp -q`

Expected: 当前分支全部测试 PASS，且无 skip/xfail、弱断言或测试专用业务分支。

- [ ] **Step 6: 检查差异、生成交付证据并提交**

Run: `git diff --check`

Run: `git status --short`

```bash
git add openapi.json tests/requirement/test_e2e.py tests/test_e2e_access_governance.py tests/test_openapi_export.py
git commit -m "test(requirement): verify baseline confirmation flow"
```

- [ ] **Step 7: 请求代码评审并按证据决定是否推送/开 PR**

在固定 HEAD 上执行 `superpowers:requesting-code-review`；只有评审结论和全部门禁均通过后，才推送 `codex/backend-requirement-v0.3` 并创建 PR。不得自动打 tag，不得宣称 V0.3 Release Gate 已通过。
