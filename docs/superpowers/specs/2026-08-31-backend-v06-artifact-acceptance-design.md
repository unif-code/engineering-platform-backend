# V0.6 Evidence, Acceptance & Formal Delivery · 后端设计

- 状态：已授权实现设计；本分支只交付后端纵切片，不代表 V0.6 Release Acceptance
- 基线：`9b71b82602bb4231f6ccc2622c0f49a91287a366`
- 权威 owner：架构 02（Requirement Workflow）、05（Source Control 与交付）、07（数据、消息与存储）

## 1. 目标

本批把当前已经到达 `WorkItem.integrationDeliveryState=INTEGRATED` 的人工交付继续推进为：

```text
精确外部验证引用
→ RequirementDeliverySnapshot
→ IntegrationBaselineEvidence
→ RequirementIntegrationBaselineSelection
→ Acceptance Gate / Decision
→ task → main Formal MR
→ Formal Review / 准确 head SHA Merge
→ WorkItem / Requirement 完成
```

所有结论都绑定不可变 ID、Version 与 Hash。重复命令返回同一结果；版本、集合、Artifact、验证引用或
Git/MR head 变化后，旧 Selection、Acceptance 与 Review 不能继续放行。

## 2. 范围

### 2.1 本批实现

- 外部人工验证引用：提交人、提交时间、稳定引用、目标 Commit、说明与精确 Artifact References。
- Requirement 自有不可变 Delivery Snapshot 与请求 Outbox。
- Source Control 自有不可变 Integration Baseline Evidence、规范化 Hash 与变化判定。
- Requirement 自有 Selection CAS、Requirement Version 提升、失效历史与 Audit。
- Requirement 自有 Acceptance Gate、Assignment、Decision、资格快照与有效性判定。
- Source Control 自有 Formal MR Binding、Effect/Reconciliation、默认 Review Assignment 与准确 SHA Merge。
- Requirement 自有 Formal Review Gate/Decision、WorkItem/Requirement 完成条件。
- PostgreSQL migration、HTTP Contract、Idempotency、ETag/CAS、Problem Details、Audit、Outbox/Inbox、真实
  PostgreSQL + HTTP E2E、OpenAPI 与完整仓库门禁。

### 2.2 Artifact 的本批边界

架构允许 Artifact 表示对象或外部引用。本人工闭环首先消费的是“不可变外部验证引用”：平台不调用、
查询或复制 Jenkins 状态，只保存稳定 URL/ID、目标 Commit、SHA-256、大小/媒体元数据（存在时）、来源、
提交人和时间。现有 V0.4 SDD Artifact 继续作为可精确引用的内部纯文本 Artifact。

本分支不启用二进制上传、Presigned S3、扫描器或对象存储部署。它们属于 07 owner 的运行时能力，必须在
有效 Storage Binding、双账本准入和 File Security Adapter 都存在时才可启用；本批不会用本地文件、内联
Base64 或伪 Presigned URL 绕过该 Contract。Evidence 的 Artifact Reference 是稳定 Port 值对象，后续对象
实现只替换 Port/Adapter，不改变 Selection/Acceptance 语义。

### 2.3 明确排除

- Agent、Model、Chat、Sandbox 与自动 Jenkins 集成。
- 前端、浏览器验收、部署、备份、HA、环境 Promotion、Release/tag。最新用户授权仅限本地：主任务可在独立 review 后将 V0.6 整合到本地 main，并安全清理已合并的 V0.6 分支/工作树；执行子任务仅做范围内本地提交，不合并、清理或操作其他工作树。禁止推送、创建 PR、触发远端 CI；此前远端交付/main push CI 步骤为历史计划，须另获明确授权。保留现有必需 CI 配置，不据本地 focused 测试宣称已发布或完成部署验收。
- V0.5 工作树中的任何实现；本分支不读取、复制或合并该工作树。

## 3. 深模块边界

### 3.1 Requirement（业务 owner）

拥有 Requirement/WorkItem 主状态、必需集合、Delivery Snapshot、Selection、Acceptance、Formal Review Gate、
Decision、失效规则和完成判定。它只消费 Source Control 包根 Facade 暴露的 Evidence/交付事实，不读取
`source_control` 表或内部类型。

### 3.2 Source Control（外部事实 owner）

拥有外部验证引用、IntegrationBaselineEvidence、Integration/Formal MR Binding/Observation、Review 默认路由
快照、GitLab Effect 与 Reconciliation。它不写 Requirement 状态，不决定 Selection/Acceptance 是否有效；
结果只经 Requirement 包根 Facade/Outbox 回传。

### 3.3 Artifact / Acceptance seam

- Artifact Reference 是不可变值：`artifactId + version + sha256`，可附媒体/大小/稳定外部引用；只允许
  `AVAILABLE` 的内部 Artifact 或已完整记录的外部引用进入 Evidence。
- Acceptance 是 Requirement 内的深子模块：一个入口接收当前 Selection，一个出口给出“当前是否仍有效”
  的受保护命令证据。Source Control 只消费这个窄证据，不复制 Gate/Decision。

模块间禁止跨 schema 双写。每个外部写通过 Effect Ledger；每个跨模块异步写通过 Outbox/Inbox；同步调用
只做只读验证或包根 Facade 命令。

## 4. V0.5 基线上的默认 V0.6 activation

V0.5 稳定基线已提供 Integration MR Binding/Observation、Requirement 的 `INTEGRATED` 回调和当前
Delivery Snapshot 查询。V0.6 保留当前查询，并通过以下窄边界冻结不可变交付证据：

1. Requirement 冻结 `RequirementDeliverySnapshot` 并发布
   `requirement.integration-baseline.requested`；
2. Source Control 只消费该消息的稳定 envelope，不猜测必需 WorkItem；
3. Source Control 生成 Evidence 后，Requirement 通过 Evidence Reader Facade 选择它；
4. Formal MR 继续复用现有 V0.5 Integration Binding/Observation，不重建第二套 Integration 接口。

默认 `create_app()` 挂载 V0.6 全部路由；生产装配使用 Requirement owner Policy、共享 Authorization
qualification 与真实 Evidence/Requirement callbacks。worker 只保留统一 relay/process/reconcile 公共入口，
分别覆盖 4/5/3 条 lane，按总 limit 均分预算；不保留隐藏路由、旧 V0.6 批次入口或兼容 shim。
冻结在 Requirement 锁内校验当前集合 Hash。普通 Gate 转派独立要求 WORKSPACE
`requirement.delivery_gate.assign`，不自动赋予 SuperAdmin 业务能力；版本为 `0.6.0`。
这些生产入口已接通不代表发布验收通过；完整 CI、集成闭环和发布验收分别提供证据。

## 5. 数据模型

### 5.1 Requirement schema

`requirement.requirement` 新增：

- `acceptance_criteria_version/hash`
- `current_integration_baseline_selection_id`
- `current_acceptance_gate_id`

新事实：

- `requirement_delivery_snapshot`
  - Requirement Version、必需集合 Version/Hash、稳定排序 WorkItem IDs、snapshot hash、actor/time。
- `integration_baseline_selection`
  - Evidence ID/Hash、Evidence 的 Requirement/集合版本、选择前后 Requirement Version、current/invalidated。
- `delivery_gate`
  - `REQUIREMENT_ACCEPTANCE | FORMAL_MR_REVIEW`、精确 subject ID/version/hash、Policy Snapshot、state/revision。
- `delivery_gate_assignment`
  - default/current reviewer、组织解析快照、revision、superseded history。
- `delivery_decision`
  - outcome/reason、Selection/criteria/head 绑定、决策时资格快照、current/invalidated history。

既有 V0.4 baseline gate 表不做兼容性重构；新的通用 Delivery Gate 只服务 V0.6，避免把 nullable Formal 字段
塞进已经受复合 FK 保护的 SDD Gate。

### 5.2 Source Control schema

- `external_validation_reference`
  - WorkItem、Integration MR Binding、目标 task head/merge commit、稳定 reference、notes、Artifact refs、
    submitter/time、规范化 hash；append-only。
- `integration_baseline_evidence`
  - snapshot identity/version/hash、集合 version/hash、evidence hash、generated time。
- `integration_baseline_evidence_item`
  - WorkItem、Repository、task commit、Integration MR/merge commit、executor、Artifact refs、外部验证引用。
- 扩展 `merge_request_binding` 为 `INTEGRATION | FORMAL`，唯一性按 kind + WorkItem，Formal target 固定 `main`。
- 扩展 Effect 为 `CREATE_FORMAL_MR | MERGE_FORMAL_MR`；Formal create/merge payload 总是带准确 head SHA。
- `formal_review_assignment`
  - Formal Binding、default/current reviewer、组织解析与 Policy Snapshot、准确 head SHA、历史。

所有 Evidence 与外部验证记录不可更新/删除；变化形成新版本。MR Observation 继续 append-only。

## 6. 领域不变量

### 6.1 Delivery Snapshot / Evidence

- Snapshot 只包含当前 required set，按 WorkItem UUID 排序；集合为空、重复或状态非 `INTEGRATED` 时拒绝。
- Source Control 必须逐项证明 Integration MR 为 `MERGED`、task head 与绑定一致、外部验证引用精确指向当前
  Commit；缺失、重复或额外项均 Fail Closed。
- Evidence Hash 覆盖 snapshot header 与规范化 item 全部字段；任何引用变化创建新 Evidence，不覆盖历史。

### 6.2 Selection

- 命令同时携带 ETag revision 与 `expectedRequirementVersion`；二者分别保护并发写和业务语义。
- Evidence 的 Requirement ID、snapshot Requirement Version、集合 Version/Hash、WorkItem 一一覆盖和当前性
  必须全部匹配。
- 成功选择原子插入 Selection、提升 Requirement Version、进入 `AWAITING_ACCEPTANCE`、写 Audit/Outbox。
- 相关输入变化时旧 Selection/Acceptance/Review 追加失效时间与原因，历史不改写。

### 6.3 Acceptance

- 默认 assignee 为 Requirement 创建人；Assignment 不授予资格。
- Decision 必须由 current assignee 且实时具备 Capability/Scope/Membership 的人员提交，并保存资格快照。
- Decision 绑定 Requirement Version、Acceptance Criteria Version/Hash、Selection 与 Evidence ID/Hash。
- `APPROVED` 进入 `AWAITING_MERGE`；`CHANGES_REQUESTED`/`REJECTED` 回到 `IN_PROGRESS`，不删除历史。
- 状态推进本身不提升 Requirement Version，因此有效 Acceptance 不因 `AWAITING_MERGE/COMPLETED` 失效。

### 6.4 Formal MR / Review / Merge

- 只有当前有效 Acceptance 才能请求 `task → main` Formal MR。
- Formal MR source 是原 task branch，不是 `dev`；create 幂等绑定准确 head SHA。
- 默认 reviewer 按组织关系解析并保存快照；Requirement 创建 Formal Review Gate，Decision 绑定准确 head SHA。
- Merge 前重新校验 Acceptance、Review、actor 资格、MR head/检查/保护；Effect unknown 只经 reconciliation 收敛。
- Formal merge 使用 squash 并请求删除 source branch；删除失败不撤销已证明 merge 事实。
- 全部 required WorkItem Formal MR merged 且 Acceptance 仍有效时，WorkItem/Requirement 才进入 `COMPLETED`。

### 6.5 Rework cycle 与 task head

- 当前版本中，非批准 Acceptance（`CHANGES_REQUESTED`/`REJECTED`）或否定 Formal Review 触发的每一轮返工，
  都必须先产生新的 task head，才能创建下一条 Integration MR Effect/Binding。
- 如果新一轮 create 请求读取到的 source head 已被该 WorkItem 任一历史 Integration Binding 的最新
  `MERGED` Observation 证明，Source Control 在 acquire Effect 前以 `NO_DELIVERY_COMMIT` 终结请求，
  不创建 Effect/MR；Requirement 清除 delivery binding/blocked reason，并回到可重试的
  `IN_PROGRESS/IMPLEMENTING`，等待新 head 后由新幂等命令重新请求。
- “只更新 Evidence、复用同一 head”是尚未设计的未来产品能力；不得从 Acceptance/Formal Review 的自由文本
  reason 推断该意图，也不得据此绕过新 head 不变量。

## 7. HTTP 与错误

受保护写统一需要 Session、Workspace capability、`Idempotency-Key`；会改变现有聚合的命令还需要 `If-Match`。
核心端点：

- `POST .../work-items/{workItemId}/external-validations`
- `POST .../{requirementId}:request-integration-baseline`
- `GET .../{requirementId}/delivery-snapshots/{snapshotId}/integration-baseline`
- `POST .../{requirementId}/integration-baseline-selections`
- `POST .../{requirementId}/acceptance-confirmations`
- `POST .../{requirementId}/acceptance-decisions`
- `POST .../work-items/{workItemId}:request-formal-mr`
- `POST .../{requirementId}/formal-review-decisions`
- `POST .../work-items/{workItemId}:request-formal-merge`

错误继续使用 RFC 9457。新增领域冲突至少区分 snapshot conflict、evidence unavailable/stale、selection stale、
acceptance stale、review stale 与 formal delivery blocked；不把 Provider 私有状态暴露为业务 Contract。

Evidence 发现查询复用 Session 与精确 WORKSPACE `requirement.read`。Requirement 在读取 Source Control 前验证已存 Snapshot 属于路径 Requirement，并校验自身规范化 Hash；只把该 Snapshot ID/Hash 经现有 Evidence Port 传给 Source Control 包根只读 Facade。响应返回既有 Evidence ID/Hash、Snapshot/Requirement/必需集合版本与 Hash、WorkItem 精确任务/集成 SHA、Artifact 引用及真实 currentness/reasons。不存在或跨 Requirement 的 Snapshot 返回 404；尚未生成的 Evidence 返回既有 `EVIDENCE_UNAVAILABLE_OR_STALE` 409。查询不写入、不自动选择、不新增 Capability，不把历史 Evidence 伪装为 CURRENT；Selection 仍独立校验当前性和冻结绑定。

## 8. Audit、幂等与安全

- 成功与拒绝都记录 actor/action/target/reason/correlation；Audit 不保存凭据、URL query、源码或 Jenkins 内容。
- 外部 reference URL 入库前删除 query/fragment；完整临时 URL 不进入数据库、日志或 Audit。
- 相同 actor/operation/key + 同 fingerprint 回放同一 sealed response；不同 fingerprint 返回冲突。
- 外部验证、Evidence、Selection、Decision、MR Binding/Observation 全部 append-only；只允许受控失效字段更新。
- GitLab 外部写仍使用稳定 Effect key、精确 head SHA 与 read-after-write/reconciliation。

## 9. 验收证据

- Domain：规范化 Hash、集合覆盖、状态转换、失效与完成判定。
- Repository/Migration：约束、append-only runtime grants、upgrade/downgrade safety、多个 Alembic heads。
- API：camelCase、ETag、Idempotency replay/conflict、authorization、Problem Details、OpenAPI。
- Real PG/HTTP E2E：从 integrated WorkItem + 外部验证到 Evidence/Selection/Acceptance，再到 Formal merge callback；
  stale head/evidence 与重复请求反例。
- 完整 CI 等价命令与独立代码审查。
