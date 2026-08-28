# V0.3 Requirement 领域基础 · 后端设计

> **路线图重分类（2026-08-28）**：本批的 Requirement/首个 WorkItem/Repository Binding 请求归 V0.3 Requirement & Branch Foundation；已提前实现的 SDD Baseline Gate/Decision 是 V0.4 的内部前置，不代表 V0.4 旅程完成。旧 V0.3“整个人工交付闭环”总括已被权威路线图取代。

- 日期：2026-08-25
- 状态：用户已确认并授权继续实施（2026-08-25）
- 仓库：`engineering-platform-backend`
- 目标分支：`codex/backend-requirement-v0.3`
- 代码基线：远端 `main@60d2eadb844734c6da108feba6c74e8ef9dda7b0`；本地从文件树等价的 `57225ccef07b7039c7760b1589e69ab70f2ccb88` 分叉，两者 tree 均为 `ef1bf27de0dafdcc9e4c52e1c721141761a6ab10`
- 架构依据：`engineering-platform-docs@541b186` 的 `architecture/{01,02,05,06,07,08,10,12}`

## 目标与版本边界

V0.3 的 Release 目标是让用户经前端创建 Requirement，并在负责人、授权仓库与 base commit 校验后获得经过远端回读验证的确定性任务分支；Requirement、首个 WorkItem、Outbox/Inbox、Effect Ledger、Binding 与最小 UI 形成首段纵向链。它不再承担整个人工交付闭环。

本设计覆盖该链的后端领域基础：建立 Requirement 深模块、首个 WorkItem、Repository Binding 请求，以及提前形成的 SDD 基线 Gate/`READY` 状态语义。GitLab Branch Binding 由同版本 Source Control 批次完成；前端最小接入仍是 V0.3 未完成范围。

后续能力重新归类为：

- V0.3：Requirement、首个 WorkItem、Repository/Branch Binding 与最小前后端旅程。
- V0.4：SDD/Route、WorkItem 拆分/Assignment、人工基线 Gate 与 UI。
- V0.5：人工 Integration MR 与精确 SHA Merge。
- V0.6：Artifact、Integration Baseline Selection、Acceptance 与 Formal MR。
- V0.7～V0.13：Chat/Model、Agent、Sandbox、专业 Agent、编排与 Team Trial；运维从 V0.14 开始。

开发可以在上游公开 Contract 稳定后重叠，不再为普通 overlap 登记 DEV deviation；但代码、CI、tag 或后一版本均不能替代 V0.3 的完整前后端旅程证据与明确 Release Acceptance。

## 方案选择

采用“后端领域基础优先”：先建立稳定领域语言、状态机、持久化与 Application Facade，再接 GitLab、Artifact、Chat/Model 和前端。

未采用的方案：

- UI-first：会重复 V0.2 已退役的运行时 Mock 路径，无法证明服务端 Gate 与授权。
- Integration-first：先接 GitLab 或 Model 会让外部 Adapter 反向定义 Requirement 业务状态。
- 一次性实现完整人工交付链：范围跨 Requirement、Source Control、Artifact、Model、前后端与运行证据，不利于逐版本验证和故障定位。

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

`type` 固定为 `feat | fix | refactor | chore`，创建后不得就地变更。首批只允许 `recordState=ACTIVE`，归档、删除和恢复留到后续 Requirement 版本。

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
- `repositoryState=WAITING_REPOSITORY | BLOCKED | BOUND`
- `repositoryBlockedReasonCode`、`repositoryBlockedAt`（仅 `BLOCKED` 时存在）
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
- `record_repository_binding(command)`：供后续 Source Control Adapter 回传 Binding Ready 事实；不暴露为浏览器可调用 API。
- `record_repository_binding_blocked(command)`：保存受控 Adapter 回传的结构化 blocked reason；同一 WorkItem 后续可由 Binding Ready 回传恢复。

所有写命令使用稳定 Idempotency Key；并发写使用 expected revision/`If-Match`。重复相同命令返回同一结果；相同 key 不同 payload 返回 Conflict。

## 外部边界

### Source Control

创建请求必须提供一个初始 GitLab Repository 标识，但本批次不实现 GitLab 协议调用、Branch 或 MR。Requirement 保存选择事实，首个 WorkItem 处于 `WAITING_REPOSITORY`；后续 `SourceControlPort` Adapter 只能回传 `BindingReady` 或结构化 `BindingBlocked`，不能直接推进 Requirement 状态。`BindingBlocked` 必须持久化安全 reason code 与发生时间，不保存外部错误正文；同一 Repository 的后续 `BindingReady` 清除 blocked 字段并按 `ASSIGNED + BOUND` 重新推导 WorkItem 状态。

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

`work_item.assign` 在第一批仅作为授权词汇和后续批次的路由种子保留。本批次不提供浏览器或内部手工分配命令；生产自动分配 Guard 固定 Fail Closed，因此真实生产装配创建的 WorkItem 保持 `UNASSIGNED`。手工分配与 Repository Guard 接入属于 V0.4 WorkItem/Assignment。

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

## 后续版本映射

1. V0.3：GitLab Connector、Repository/Branch Binding、Webhook、Effect Ledger、Reconciliation 与最小前端。
2. V0.4：SDD/Route、Assignment、人工 Gate 与任务详情前端。
3. V0.5：WorkItem 人工实现与 Integration MR。
4. V0.6：Artifact/Evidence、Integration Baseline Selection、Acceptance、Formal MR Review/Merge。
5. V0.7：Chat/SDD、Model Gateway、Model Route Policy 与 promptfoo Evaluation Evidence。

V0.6 通过后才可宣称人工交付闭环；V0.3 后端基础或 V0.5 Integration MR 代码完成均不能提前形成该结论。
