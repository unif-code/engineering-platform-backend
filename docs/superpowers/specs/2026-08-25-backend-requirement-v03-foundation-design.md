# V0.3 Requirement 领域基础 · 后端设计

- 日期：2026-08-25
- 状态：设计已获口头确认，待书面复核
- 仓库：`engineering-platform-backend`
- 目标分支：`codex/backend-requirement-v0.3`
- 代码基线：远端 `main@60d2eadb844734c6da108feba6c74e8ef9dda7b0`；本地从文件树等价的 `57225ccef07b7039c7760b1589e69ab70f2ccb88` 分叉，两者 tree 均为 `ef1bf27de0dafdcc9e4c52e1c721141761a6ab10`
- 架构依据：`engineering-platform-docs@541b186` 的 `architecture/{01,02,05,06,07,08,10,12}`

## 目标与版本边界

V0.3 的 Release 目标是跑通人工交付闭环：人员创建 Requirement，形成并确认 SDD，拆分并分配 WorkItem，人工完成代码交付，绑定精确 Git/MR/Artifact 证据，完成 Acceptance、Formal MR Review 与合并。V0.3 不执行 Agent 代码，不创建 Kata Sandbox，也不允许 Model、Connector 或 UI 代替人工 Gate。

本设计只覆盖 V0.3 的第一批后端领域基础：建立 Requirement 深模块、首个 WorkItem、SDD 基线 Gate 以及到 `READY` 为止的状态语义。GitLab Connector、对象存储、Chat/Model Gateway、MR/Acceptance 全闭环与前端接入在后续 V0.3 批次完成。

因此版本能力边界固定为：

- V0.3：人工 Requirement/SDD/Git/MR/Artifact 交付流程可端到端运行。
- V0.4：在 V0.3 人工责任链之上增加 Agent Attempt、Kata Sandbox、Pydantic AI Runtime 与有界 Child Execution。
- V0.5：在 DEV 完成生产候选级集成、运维、安全、容量、回滚与真实恢复验收。

V0.3 开发可以与 V0.1/V0.2 的环境验收收尾并行，但不得据此提前宣称 V0.3 Release Gate 通过或激活 V0.3 Capability。正式实现前由架构仓登记该开发时序偏离；在登记完成前，本仓不铸造或引用新的 `DEV-xxx` 编号。

## 方案选择

采用“后端领域基础优先”：先建立稳定领域语言、状态机、持久化与 Application Facade，再接 GitLab、Artifact、Chat/Model 和前端。

未采用的方案：

- UI-first：会重复 V0.2 已退役的运行时 Mock 路径，无法证明服务端 Gate 与授权。
- Integration-first：先接 GitLab 或 Model 会让外部 Adapter 反向定义 Requirement 业务状态。
- 一次性实现完整 V0.3：范围跨 Requirement、Source Control、Artifact、Model、前后端与运行证据，不利于逐批验证和故障定位。

## 统一领域语言

- **Requirement**：整体交付与验收聚合，拥有 Route Snapshot、必需 WorkItem 集合、Gate、Decision、Artifact 引用和主状态。避免称为“任务”或“需求单”。
- **WorkItem**：单一仓库、单一任务分支上的可分配交付单元。避免与 Requirement 或 Agent Attempt 混称为 Task。
- **Route Snapshot**：Requirement 创建时冻结的交付方法与所需 Artifact/Gate 基线，不是前端路由。
- **Gate Instance**：绑定准确 subject 版本或 hash 的一次人工确认责任，不是自动化检查结果。
- **Assignment**：当前责任人事实，不授予 Capability，也不扩大 Scope。
- **Decision**：当前 Gate assignee 在资格有效时作出的 `APPROVED`、`CHANGES_REQUESTED` 或 `REJECTED` 结论。
- **Repository Binding**：WorkItem 与一个 GitLab Project、base commit 和任务分支的不可变绑定；仓库选择不是第二份代码项目模型。
- **Artifact**：不可变版本元数据及其准确对象或外部引用；源码和移动分支不是 Artifact。

## 模块边界

新增 `control_plane/app/modules/requirement/`，遵循现有五层：

```text
requirement/
├── api/          HTTP DTO、路由、Problem 映射与授权依赖
├── application/  命令、查询、事务边界与公开 Facade
├── domain/       聚合、值对象、状态机、Decision 与领域错误
├── ports/        Repository、Audit、Policy、Artifact 与 Source Control Port
└── adapters/     SQLAlchemy Repository；外部 Adapter 后续批次提供
```

其他模块只能从 `control_plane.app.modules.requirement` 包根使用公开 Facade。Requirement 模块只通过公开 Facade 消费 Identity/Authorization/Audit，不导入其他模块的 ORM、Repository 或内部 Entity。新增模块必须同步加入 import-linter 层级与深层导入禁止契约。

## 聚合与状态

### Requirement

首批至少保存：

- `id`、`workspaceId`、`type`、`title`、`description`、`acceptanceCriteria`
- `createdBy`、`initialRepositoryId`
- `routeSnapshotVersion/hash`
- `state`、`recordState`
- `requirementVersion`
- `requiredWorkItemSetVersion/hash`
- `revision` 与创建/更新时间

`type` 固定为 `feat | fix | refactor | chore`，创建后不得就地变更。首批只允许 `recordState=ACTIVE`，归档、删除和恢复留到后续 V0.3 批次。

状态机首批覆盖：

```text
CREATED → PREPARING → AWAITING_CONFIRMATION → READY
AWAITING_CONFIRMATION --CHANGES_REQUESTED--> PREPARING
AWAITING_CONFIRMATION --REJECTED--> CANCELED
除 READY 外的首批非终态 --受控取消且无活动副作用--> CANCELED
```

`CREATED` 是可读取、可审计的持久化状态。创建事务原子保存 Requirement、首个 WorkItem、初始 Repository Binding 请求与 Outbox；后续初始化命令只有在绑定请求已被可靠接纳后才将 Requirement 推进到 `PREPARING`。初始化 Worker 不可用时记录结构化 blocked reason 并保持 `CREATED`，不能假装准备已开始。

当前 SDD Artifact 可用且当前 Gate Decision 为 `APPROVED` 时，Requirement 进入 `READY`。首个 WorkItem 未分配或 Repository Binding 未就绪只阻塞该 WorkItem 保持 `DRAFT`，不阻塞已经完成的 Requirement 基线确认；只有 WorkItem 同时 `ASSIGNED` 且 `BOUND` 后，它自身才进入 `READY`。

### WorkItem

创建 Requirement 时在同一事务中创建首个必需 WorkItem，并继承 `initialRepositoryId`。首批保存：

- `id`、`requirementId`、`createdBy`
- `humanOwnerId`、`executorType=HUMAN`、`executorId`
- `requiredCapabilities`
- `assignmentState=UNASSIGNED | ASSIGNED`
- `repositoryState=WAITING_REPOSITORY | BOUND`
- `state=DRAFT | READY | CANCELED`
- `revision`

只有创建人同时满足当前 Capability、Workspace Scope、Membership 与 Repository Guard 时才自动成为负责人；否则 WorkItem 保持 `UNASSIGNED`。首批不创建任务分支，也不进入 `IN_PROGRESS`。

### SDD Baseline Gate

首批 Gate Type 固定为 `REQUIREMENT_BASELINE_CONFIRMATION`。Gate Instance 必须绑定：

- Requirement 与当前 `requirementVersion`
- SDD Artifact 的准确 `artifactId/version/hash`
- Route Snapshot version/hash
- Effective Gate Policy version
- `defaultReviewerId`、`currentReviewerId`
- assignment revision 与创建时间

Gate 创建后 Requirement 进入 `AWAITING_CONFIRMATION`。Decision 不可修改；新的 Artifact 版本必须形成新的 Gate，旧 Gate/Decision 完整保留。

## 命令与查询

公开 Application Facade 首批提供：

- `create_requirement(command)`：幂等创建 Requirement 与首个 WorkItem。
- `start_requirement_preparation(command)`：只在初始 Binding 请求已可靠入队后把持久化的 `CREATED` 推进到 `PREPARING`；供内部 Worker 调用。
- `get_requirement(query)`：按当前授权读取详情。
- `list_requirements(query)`：Workspace 范围 cursor 分页。
- `register_sdd_baseline(command)`：绑定已经由 Artifact Port 验证为 `AVAILABLE` 的准确 SDD Artifact 版本。
- `submit_baseline_confirmation(command)`：CAS 创建 Gate 并进入等待确认。
- `decide_baseline(command)`：校验当前 assignee、实时资格、subject 版本与 revision 后追加 Decision。
- `record_repository_binding(command)`：供后续 Source Control Adapter 回传 Binding Ready/Blocked 事实；不暴露为浏览器可调用 API。

所有写命令使用稳定 Idempotency Key；并发写使用 expected revision/`If-Match`。重复相同命令返回同一结果；相同 key 不同 payload 返回 Conflict。

## 外部边界

### Source Control

创建请求必须提供一个初始 GitLab Repository 标识，但本批次不实现 GitLab 协议调用、Branch 或 MR。Requirement 保存选择事实，首个 WorkItem 处于 `WAITING_REPOSITORY`；后续 `SourceControlPort` Adapter 只能回传 `BindingReady` 或结构化 `BindingBlocked`，不能直接推进 Requirement 状态。

本批次不使用临时运行时 Mock 冒充 GitLab。测试使用进程内 Fake Port 验证领域和 Application Contract；生产装配在真实 Adapter 尚未激活时保持 Fail Closed。

### Artifact

Requirement 不接收浏览器声明的“可信”状态。`ArtifactPort` 只返回已经完成 Object Version、hash、策略与扫描/可信纯文本判定的不可变 Artifact Snapshot。本批次不实现上传、Presigned URL、对象存储或扫描 Worker；这些缺失时 SDD baseline 注册 Fail Closed。

### Gate Policy 与授权

Gate Policy 通过 Port 读取有效快照；Requirement 不拥有 Configuration 生命周期。首批不得用硬编码 Super Admin 绕过。V0.3 Capability 必须显式注册并通过普通 Grant 生效，未知 Capability 默认拒绝。

## HTTP API

首批公开：

- `POST /api/v1/requirements`
- `GET /api/v1/requirements`
- `GET /api/v1/requirements/{requirementId}`
- `POST /api/v1/requirements/{requirementId}/sdd-baselines`
- `POST /api/v1/requirements/{requirementId}/baseline-confirmations`
- `POST /api/v1/requirements/{requirementId}/baseline-decisions`

成功 DTO 使用 camelCase；列表为 `{items, nextCursor}`；写请求强制 `Idempotency-Key`，CAS 命令强制 `If-Match`。错误统一为 RFC 9457 Problem Details，至少区分：未授权、当前 assignee 不匹配、资格失效、revision 冲突、Artifact 不可用、subject 已变化、Repository Binding 未就绪、状态转换非法与依赖不可用。

浏览器不能调用 Repository Binding 回传接口，也不能提交 actor、资格快照、Artifact 可用状态或 Gate Policy version。

## 持久化与一致性

新增独立 `requirement` schema、`requirement_rw` 运行角色和独立 Alembic head。首批表至少包括：

- `requirement.requirement`
- `requirement.work_item`
- `requirement.gate_instance`
- `requirement.gate_assignment`
- `requirement.decision`
- `requirement.sdd_baseline`
- `requirement.idempotency_record`
- `requirement.outbox_message`

领域写入、Decision、Audit 与必要的后续 Effect/Outbox 记录必须处于同一 PostgreSQL 事务。Audit 保持追加式；`requirement_rw` 不获得 Audit 表 DML 权限，只能调用既有受控追加函数。迁移升级不得改写 V0.1/V0.2 业务事实，降级只移除 Requirement 自有对象和授权种子。

## 授权与审计

首批 Capability 至少包括：

- `requirement.create`
- `requirement.read`
- `requirement.baseline.submit`
- `requirement.baseline.decide`
- `work_item.assign`

所有判定同时验证 Capability、Workspace Scope、Membership 和当前对象关系。Requirement 创建人不是隐式管理员；Gate assignee 只有在决策时资格仍有效才能签署。

Audit 记录创建、首项初始化、负责人解析、SDD baseline 绑定、Gate 创建、Decision、拒绝、冲突、取消和 Repository Binding 结果，并贯穿 request/correlation ID。Audit 摘要不得包含完整 SDD 正文、Secret、Token、凭据或外部源码。

## 测试策略

按 TDD 分层：

1. Domain：状态转换、版本/hash、集合不变量、Decision 不可变、拒绝与取消。
2. Application：幂等、CAS、授权、负责人解析、Port 失败、Audit 原子性与重放。
3. SQL/迁移：独立 schema/head、最小权限、upgrade/downgrade、V0.2 数据保留。
4. API：camelCase、cursor、Problem Details、ETag/If-Match、Idempotency-Key、OpenAPI security。
5. 架构：新模块 import-linter 覆盖、深层导入反例失败。
6. 端到端：真实 PostgreSQL 下创建 Requirement、可靠入队后进入 `PREPARING`、绑定 SDD、提交 Gate、人工 Decision 后 Requirement 进入 `READY`；Repository 未 Ready 时首个 WorkItem 保持 `DRAFT` 并显示 blocked reason，回传 Binding Ready 且负责人有效后 WorkItem 进入 `READY`。

不得通过 skip、弱断言、测试专用业务分支、运行时 Mock API 或降低现有门禁获得通过。

## 验收标准

1. 新模块边界与公开 Facade 通过 import-linter 和守护测试。
2. 真实 PostgreSQL 可持久化 Requirement、首个 WorkItem、Gate、Assignment 与 Decision。
3. SDD baseline 与 Decision 精确绑定 Artifact version/hash、Requirement version、Route hash 和 Policy version。
4. 未分配、Repository 未绑定、Artifact 不可用、资格失效或版本冲突均 Fail Closed，且原因可审计。
5. 相同 Idempotency Key 重放稳定，不同 payload 冲突；并发写不发生覆盖。
6. OpenAPI 与代码一致，breaking 版本策略由后续发布计划单独确认；本设计不自动打 tag。
7. Ruff、mypy、import-linter、Alembic heads、完整 pytest 与 OpenAPI check 全绿。

## 后续 V0.3 批次

1. GitLab Connector、Repository/Branch Binding、Webhook、Effect Ledger 与 Reconciliation。
2. Artifact 上传、对象存储、扫描/可信纯文本策略与 Evidence Snapshot。
3. Chat/SDD、Model Gateway、Model Route Policy 与 promptfoo 一次性评测证据。
4. WorkItem 交付、Integration Baseline Selection、Acceptance、Formal MR Review/Merge。
5. 前端 Requirement/Tasks/Messages 原型接入版本化 OpenAPI Artifact并完成真实人工交付旅程。

这些批次全部完成并通过 V0.3 Release Gate 后，才可宣称人工交付流程跑通；Agent 自动研发流程仍属于 V0.4。
