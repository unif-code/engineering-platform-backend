# V0.5 Human Integration MR Foundation · 后端设计

> **路线图重分类（2026-08-28）**：文件名保留原 V0.3 历史，但本批按新权威路线图归 V0.5 Human Integration Delivery。Artifact、IntegrationBaselineEvidence、Acceptance 与 Formal MR 仍不在本批，进入 V0.6；Chat/Model 进入 V0.7，Agent/Sandbox 进入 V0.8～V0.12。

- 日期：2026-08-26
- 状态：历史实现设计；对应 Human Integration MR 代码已合入后端 `main`，不代表 V0.5 Release 已验收
- 仓库：`engineering-platform-backend`
- 目标分支：`codex/backend-integration-mr-v0.3`
- 代码基线：`main@f03f95277acdc3fc8b50cba0e28ad136afb672e5`
- 架构依据：`engineering-platform-docs@541b186878d1e28e1aa9308111a2962cdfefb91b`
  的 `architecture/{README,00,01,02,05,06,08,12,appendix-parameters}.md`
- Provider 依据：GitLab 官方
  [Merge Requests API](https://docs.gitlab.com/api/merge_requests/)、
  [Projects API](https://docs.gitlab.com/api/projects/) 与
  [Webhook Events](https://docs.gitlab.com/user/project/integrations/webhook_events/)

## 目标与批次边界

本批承接已经合并的 Requirement Foundation 与 Source Control Foundation，让已完成 SDD
确认、已分配负责人且已有不可变任务分支的人工 WorkItem 进入实现与集成：负责人显式开始实现，
完成任务分支提交后请求 `task branch → dev` Integration MR；平台通过 GitLab 创建并回读 MR，
由有权限人员显式请求 merge，平台校验准确 `headSha`、确定性检查与 GitLab 保护后执行 merge，
最终回读并记录外部事实。任何可能已到达 GitLab、但无法证明结果的操作都进入
`UNKNOWN/RECONCILIATION`。

本批结束时，成功路径停在：

```text
Requirement READY / WorkItem READY
→ 人工负责人开始实现
→ Requirement IN_PROGRESS / WorkItem IN_PROGRESS
→ 人员在任务分支形成 GitLab commit
→ 请求 Integration MR
→ task branch → dev MR 已创建并绑定准确 headSha
→ Requirement VERIFYING / WorkItem VERIFYING
→ 有权限人员显式请求 Integration Merge
→ GitLab 以 merge commit 合并到 dev，source branch 保留
→ WorkItem.integrationDeliveryState = INTEGRATED
```

本批明确不包含：

- Jenkins 调用、状态读取、Webhook、日志复制或自动 Gate。
- 外部验证证据引用、Artifact、`RequirementDeliverySnapshot`、
  `IntegrationBaselineEvidence`、Selection 或 Acceptance。
- Formal MR、Formal Review、合并 `main` 或删除任务分支。
- Agent Commit Push、Credential Broker、Attempt、Sandbox、Chat/Model、前端与 GitOps。
- 真实 GitLab 凭据配置、环境 Smoke Test、Capability 激活、版本 tag 或 V0.5 Release Gate。

## 架构划分与方案选择

这是一条跨两个领域模块的纵向批次，不是把内容合并成一个模块。

| 事实或行为 | Owner | 不属于该 Owner 的内容 |
| --- | --- | --- |
| 人工开始 WorkItem、提交集成请求、Requirement/WorkItem 主状态、业务幂等与 Outbox | `requirement` | GitLab MR 字段、Provider 状态、HTTP 与外部对账 |
| Branch/MR Binding、准确 head SHA、GitLab Effect、MR Observation、Webhook Inbox 与 Reconciliation | `source_control` | Requirement 状态机、Assignment 选择、Acceptance |
| 当前人员 Capability、Scope、Membership 与账号有效性 | `authorization` / `identity` / `workspace` 的公开 Facade | MR 或 WorkItem 状态 |
| GitLab Project、Branch、MR 与 Merge 的协议事实 | GitLab，经 `GitLabPort` Adapter 访问 | 平台业务状态与人工 Decision |

模块间只通过包根 Facade、Port 与 Outbox/Inbox 协作。`source_control` 不读写
`requirement` 私有表，`requirement` 不读写 `source_control` 私有表；任何跨 schema join、共享 ORM、
双写事务或 Connector 直写两个 schema 都是设计违规。

采用“Requirement 记录受保护意图，Source Control 收敛外部效果”的方案：

1. 用户命令先在 Requirement 单模块事务中校验业务状态、责任人与服务端授权，写业务事实和 Outbox。
2. Source Control 把消息幂等接纳进自己的 Inbox，随后独立处理 GitLab Effect。
3. 外部事实成立后，Source Control 只经 Requirement 包根 Facade 回传稳定结果；回调失败可重放。
4. Webhook 只缩短对账延迟，最终结论始终来自 GitLab GET 回读。

未采用的方案：

- **全部放进 Source Control**：会让 GitLab 模块拥有 WorkItem 业务状态与人员责任，违反 02/05
  的事实所有权矩阵。
- **全部放进 Requirement**：会让 Workflow 模块拥有 Provider 协议、Effect Ledger 和对账，删除
  Source Control 后复杂度只会散落，不会消失。
- **由 API 同步跨模块调用 GitLab 并更新两个 schema**：请求超时和部分提交无法安全恢复，也破坏
  单模块事务。
- **用户直接在 GitLab 合并后由 Webhook 推进业务状态**：平台无法证明发起人具备当前平台
  Capability/Scope，Webhook 也可能重复、乱序或缺失。

### 深模块删除测试

删除 `requirement` 后，人工责任、业务状态、乐观并发、受保护命令和 Outbox 规则会散到 API 与
Source Control；删除 `source_control` 后，MR 唯一性、准确 SHA、GitLab 错误归一、Effect、Webhook
与 Reconciliation 会散到 Requirement、Connector 与 worker。两个模块都隐藏了调用方不应学习的
复杂实现，因此各自具有独立责任与足够深度。

## 统一领域语言

- **Integration Delivery Request**：Requirement 记录的人工作业意图，分为创建 Integration MR 与
  合并 Integration MR；不是 GitLab 已成功的事实。
- **Integration Merge Request Binding**：Source Control 对一个 WorkItem 的任务分支、`dev` 目标、
  GitLab MR IID 与创建 Effect 的稳定绑定。
- **MR Observation**：一次 GitLab 回读得到的 MR 状态、准确 head SHA 与 merge commit 事实；追加保存，
  不覆盖历史。
- **Integration Delivery State**：Requirement 对交付阶段的稳定业务投影，不复制 GitLab 私有状态。
- **External Merge Drift**：没有有效平台 Merge Request Effect，却观察到 MR 已被外部合并的事实；
  必须保留和报告，但不能伪装成平台受保护命令成功。

## Requirement 模块设计

### 状态与业务投影

扩展现有枚举：

- `RequirementState` 增加 `IN_PROGRESS`、`VERIFYING`。
- `WorkItemState` 增加 `IN_PROGRESS`、`VERIFYING`。
- 新增 `IntegrationDeliveryState`：
  `NOT_STARTED | IMPLEMENTING | MR_PENDING | MR_OPEN | MERGE_PENDING | INTEGRATED | BLOCKED | RECONCILIATION_PENDING`。

WorkItem 只保存稳定业务投影与 Source Control 引用：

- `integrationDeliveryState`
- `integrationMergeRequestBindingId`（Source Control 稳定 ID，可空）
- `integrationBlockedReasonCode`（allowlist，可空）
- `integrationUpdatedAt`

GitLab project ID/path、MR IID/URL、私有 merge status、HTTP body、pipeline payload、PAT 与源码均不进入
Requirement schema。准确 SHA 与外部 MR 生命周期由 Source Control 保存。

Requirement 聚合状态按必需 WorkItem 推导：第一个 WorkItem 开始实现时 Requirement 从 `READY` 进入
`IN_PROGRESS`；只有全部必需 WorkItem 都至少进入 `VERIFYING` 时 Requirement 才进入 `VERIFYING`。
本批仍只有创建时自动生成的首个必需 WorkItem，但实现不得把“只有一个”编码为长期不变量。

### 受保护命令

新增公开业务命令：

1. `start_work_item(...)`
   - Requirement 与 WorkItem 必须为 `READY`。
   - actor 必须是当前 `humanOwnerId`，Assignment、Membership、Scope、账号状态和
     `work_item.execute` Capability 当前有效。
   - Repository 必须 `BOUND`；未绑定、Blocked 或 stale revision 一律 Fail Closed。
   - 同一 idempotency key 返回同一结果，不重复推进 revision。
2. `request_integration_merge_request(...)`
   - WorkItem 必须为 `IN_PROGRESS/IMPLEMENTING`，actor 仍是当前负责人并具备执行资格。
   - 同事务把业务投影置为 `MR_PENDING` 并写
     `requirement.integration-merge-request.requested` Outbox。
   - Outbox payload 只含稳定 ID、Requirement/WorkItem revision、repository binding reference、
     actor 与命令证据摘要；不接受调用者传入 source/target branch。
3. `request_integration_merge(...)`
   - WorkItem 必须为 `VERIFYING/MR_OPEN`，绑定引用存在且没有待对账冲突。
   - actor 必须具备 `merge_request.merge`、有效 Workspace Scope/Membership；不要求 Formal Review，
     但不能由 Agent、Connector、Bot 或失效人员发起。
   - 同事务把业务投影置为 `MERGE_PENDING` 并写
     `requirement.integration-merge.requested` Outbox。

`work_item.execute` 与 `merge_request.merge` 作为 Authorization Grant 与路由检查使用的稳定
Capability 字符串，由路由调用现有服务端授权 Facade；当前实现不为此虚构独立 Capability Catalog
或新事实表，前端可见性也不是安全结论。

### Outbox relay 与公开 Facade

Requirement 包根提供少量内部 Facade：

- claim/ack/release 两类 Integration Delivery Request；继续使用 lease、attempts、allowlist error code
  与 `FOR UPDATE SKIP LOCKED`。
- `get_integration_delivery_context(work_item_id)`：返回稳定业务上下文、当前 revision、Assignment、
  repository/branch binding reference、delivery state 与受保护命令证据；不暴露 ORM。
- `record_integration_mr_ready(...)`：保存 Source Control binding ID，清除安全阻塞，WorkItem 进入
  `VERIFYING/MR_OPEN`，并按聚合规则推进 Requirement。
- `record_integration_delivery_blocked(...)`：保存稳定 reason；不把 Requirement/WorkItem 标成失败。
- `record_integration_reconciliation_pending(...)`：显式显示结果未知。
- `record_integration_merged(...)`：仅接受 Source Control 已证明的受保护 Merge Effect，业务投影进入
  `INTEGRATED`；WorkItem 保持 `VERIFYING`，等待下一批外部证据与 Evidence。
- `record_external_merge_drift(...)`：保留外部已合并事实的稳定引用并标记 Blocked，不把未授权外部动作
  当作平台命令成功。

所有回调使用由 Effect ID 派生的稳定幂等键和 expected WorkItem revision。revision 冲突时重新读取
Context 并重新验证，已前进状态不回退，旧回调不能覆盖新请求。

## Source Control 模块设计

### 持久化演进

在既有独立 `source_control` Alembic head 上新增迁移，保持 `source_control_rw` 最小权限。

#### `delivery_request_inbox`

接纳两类 Requirement Outbox topic，保存 message ID、topic、payload hash、Requirement/WorkItem/
Repository ID、受保护命令证据摘要、状态、attempts、lease、safe error code 与时间。相同 ID/相同 hash
幂等；相同 ID/不同 hash 安全冲突。

#### `merge_request_binding`

每个 WorkItem 只允许一个 `kind=INTEGRATION` Binding，至少保存：

- binding ID、WorkItem/Requirement/Workspace/Repository ID、Branch Binding ID
- external project ID、MR IID、source branch、固定 target branch `dev`
- create Effect ID、创建时 head SHA、`creationOrigin=PLATFORM_CREATED | EXTERNAL_ADOPTED`、createdAt

Binding 的 owner/branch/target/external IID 不可更新或删除。GitLab 可变状态不写回 Binding。

#### `merge_request_observation`

追加保存 binding ID、observed head SHA、稳定状态
`OPEN | MERGED | CLOSED | LOCKED`、merge commit SHA、可选 external merge user ID、mergedAt、
Provider observation digest 与 observedAt。相同 binding/observation digest 幂等；新 commit 或状态
变化形成新行，历史不覆盖。不保存 GitLab 用户姓名、邮箱或头像。

#### `source_control_effect`

把现有只允许 `CREATE_TASK_BRANCH` 的 Ledger 深化为支持：

- `CREATE_TASK_BRANCH`
- `CREATE_INTEGRATION_MR`
- `MERGE_INTEGRATION_MR`

增加通用 `subjectKey` 与 operation-specific payload/fingerprint；唯一约束改为
`(operation, subjectKey)`。历史 Branch Effect 回填 `subjectKey=work-item:<id>`，不得重写 Effect ID、
状态或时间。MR 创建 subject 是 WorkItem；Merge subject 是 `bindingId + requestedHeadSha`，同一准确
head 的重复命令收敛到同一 Effect，新 head 必须形成新命令证据。

### GitLab Port 与 Project Profile

扩展内部 `GitLabPort`：

- `get_project_profile`
- `get_branch`
- `list_merge_requests(source_branch, target_branch, state=all)`
- `create_integration_merge_request`
- `get_merge_request`
- `merge_integration_merge_request(expected_head_sha)`

Adapter 把 GitLab 私有 JSON 和状态映射为平台 DTO，Domain 不依赖 `httpx`、GitLab response type 或
私有字符串。Project Profile 至少验证：

- project ID/path 与 Workspace Repository 投影一致；default branch 为 `main`。
- `main` 与 `dev` 均存在且受保护。
- project `merge_method=merge`，保证 Integration Merge 产生 merge commit。
- MR 创建固定 `remove_source_branch=false`、`squash=false`、`allow_collaboration=false`。

任一 Profile 不满足都返回结构化 Blocked，不由平台偷偷修改 GitLab Project 设置。

### Integration MR 创建 Saga

```text
接纳 Requirement MR Request
→ 重新读取 Requirement Context、Branch Binding、Workspace Repository 与人员资格
→ GitLab GET task branch，记录当前 headSha
→ GitLab GET dev，验证存在且 protected
→ 持久化 CREATE_INTEGRATION_MR PLANNED/subject/fingerprint
→ 提交 IN_FLIGHT
→ GitLab LIST source=task,target=dev,state=all
→ 唯一兼容 MR：以 EXTERNAL_ADOPTED 记录后复用；多个或不兼容 MR：BLOCKED/MR_CONFLICT
→ 没有 MR：POST source=task,target=dev, remove_source=false, squash=false
→ GET MR + GET task branch 回读
→ 证明 project/source/target/head/state：插入 Binding 与 Observation，Effect SUCCEEDED
→ 通过 Requirement Facade 回传 MR_READY
```

MR 标题与描述采用服务端确定性模板，包含 Requirement/WorkItem 稳定引用，不调用 Model。创建请求
返回空 `diff_refs` 或异步准备状态不算失败；本批只依赖 GET MR 的 `sha`、source/target、state 与
external IID。创建超时、5xx、响应无法解析或 POST 后 GET 不可得都进入 `UNKNOWN`，不能直接重试
POST 生成重复 MR。

若初次读取 head SHA 后任务分支继续移动，MR 仍绑定同一 Branch Binding；每次回读追加新的
Observation，并以最新已证明 head SHA 回传。Evidence 尚未生成，因此该变化不失效 Acceptance；
但 Merge 命令必须显式绑定它读取时的准确 head SHA。

### Integration Merge Saga

```text
接纳 Requirement Merge Request
→ 重新读取业务 Context、当前人员 merge 权与 MR Binding
→ GET MR + GET source/dev branch
→ 校验 MR OPEN、source/target/project、requestedHeadSha == MR sha == source head
→ 校验 target protected、Project merge_method=merge、GitLab 确定性检查可合并
→ 持久化 MERGE_INTEGRATION_MR PLANNED
→ 提交 IN_FLIGHT
→ PUT merge，始终携带准确 sha、squash=false、should_remove_source_branch=false
→ GET MR + GET source branch 回读
→ 证明 state=merged、合并前 headSha 一致、mergeCommitSha 存在且 source branch 仍存在：
  追加 Observation，Effect SUCCEEDED
→ 回传 Requirement INTEGRATED
```

检查未完成、pipeline 不满足 Project Policy、冲突、branch protection 拒绝、MR 已关闭或 SHA 已变化
都返回结构化 Blocked，不启用 auto-merge、不绕过保护、不删除 source branch。超时、5xx、锁冲突或
返回无法解析时进入 `UNKNOWN`；Reconciler 通过 GET MR 判定已经合并、仍打开、已关闭或仍未知。

如果 GitLab 已完成 merge 但 source branch 被外部设置删除，已成立的 Merge Observation 仍完整保留，
但 Effect 以 `SOURCE_BRANCH_MISSING_AFTER_INTEGRATION` 形成可处置阻塞并回传 Requirement；不能回滚
GitLab 已发生的 merge，也不能宣称该 WorkItem 已具备后续 Formal MR 条件。

若没有有效 `MERGE_INTEGRATION_MR` Effect 却观察到 MR 已被外部合并，Source Control 保存真实
Observation 与 Audit，但回传 `EXTERNAL_MERGE_DRIFT`，不把它解释成受保护平台命令成功。

### Webhook 与 Reconciliation

沿用现有 Standard Webhooks HMAC-SHA256 验签、`webhook-id` 去重和无明文 token fallback。Webhook
Inbox 扩展保存 MR event 的安全摘要：project ID、MR IID、action、source/target branch、old/new
head SHA、state 与 payload digest，不保存完整 body、用户邮箱、源码或凭据。

Webhook `open/update/merge/close/reopen` 只把匹配 Binding/Effect 的对账提前为 due；worker 必须再
调用 GET MR/GET Branch。官方文档明确 MR Webhook 可在 source branch 新 commit 时触发，且 changes
可能为空，因此不能直接根据 action 或 payload 覆盖 Observation 与 Requirement 状态。

Reconciler 的收敛规则：

- Create MR UNKNOWN：按 source/target 列表查找；唯一兼容 MR 回读后绑定，多个候选冲突，未找到时
  使用同一 Effect 再次创建。
- Merge UNKNOWN：GET 已 merged 且 head/merge commit 可证明则成功；仍 open 时按同一 Effect 重试；
  closed 或 SHA 变化则 Blocked；查询不可得继续 UNKNOWN。
- Requirement 回调失败不回滚已证明的 GitLab 事实，Effect 保持 SUCCEEDED、callbackState 待重放。
- 领取、Saga 与 Reconciliation 都使用 lease token/attempt fencing，旧 worker 不能完成新 lease。

## 错误与安全映射

Source Control 内部可使用细粒度错误码；Requirement 只接收稳定 allowlist：

- `OWNER_MISMATCH`、`OWNER_INELIGIBLE`、`MERGE_ACTOR_INELIGIBLE`
- `REPOSITORY_NOT_AUTHORIZED`、`BRANCH_BINDING_MISSING`
- `TARGET_BRANCH_NOT_FOUND`、`TARGET_BRANCH_NOT_PROTECTED`
- `NO_DELIVERY_COMMIT`、`HEAD_SHA_CHANGED`
- `MR_CONFLICT`、`MR_CLOSED`、`MR_CHECKS_BLOCKED`、`MERGE_CONFLICT`
- `PROJECT_PROFILE_UNSUPPORTED`、`SOURCE_BRANCH_MISSING_AFTER_INTEGRATION`、
  `EXTERNAL_MERGE_DRIFT`
- `PROVIDER_UNAVAILABLE`、`RECONCILIATION_PENDING`

外部 URL、PAT、GitLab error body、源码、commit message 与用户隐私字段不进入 Requirement、Audit、
Problem Details 或日志。Audit 只记录 actor、Scope、Requirement/WorkItem/Repository/Binding/Effect ID、
准确 SHA、安全状态与 correlation ID。

## API 与兼容性

业务 HTTP 入口继续属于 Requirement API：

- `POST /api/v1/requirements/{requirementId}/work-items/{workItemId}:start`
- `POST /api/v1/requirements/{requirementId}/work-items/{workItemId}:request-integration-mr`
- `POST /api/v1/requirements/{requirementId}/work-items/{workItemId}:request-integration-merge`

所有写命令要求 `Idempotency-Key` 与 `If-Match`，错误继续使用 Problem Details。Connector Webhook 不挂
公开业务 OpenAPI。新增枚举与 endpoints 属向后兼容扩展，但必须更新锁定 OpenAPI Artifact；不提升
版本、tag 或宣称前端已接入。

## 测试策略

1. Requirement Domain：完整状态矩阵、聚合推导、owner/Capability/Scope guard、stale revision、
   幂等与 blocked 不变成 FAILED。
2. Requirement Outbox/Facade：两类消息 claim/lease/ack/release、历史消息兼容、回调重放与 CAS。
3. Source Control Domain：Effect subject key、MR Binding 不变量、Observation 追加、错误归一。
4. Migration/SQL：新表、约束、历史 Effect 回填、最小权限、跨 schema 零写入与安全 downgrade。
5. GitLab Adapter：Project Profile、LIST/POST/GET MR、准确 SHA merge、空 diff refs、401/403/404/
   409/422/5xx/timeout 与响应畸形。
6. MR Create Saga：重复请求、已有唯一 MR、多个 MR 冲突、POST 后事务失败、head 移动与回调失败。
7. Merge Saga：checks blocked、SHA stale、merge conflict、已合并回读、UNKNOWN 收敛与外部 merge drift。
8. Webhook：签名、去重、open/update/merge/close、空 changes、乱序与只触发回查。
9. PostgreSQL E2E：Requirement command/outbox → Source Control inbox/effect/MR → Requirement callback；
   使用 Fake GitLab，不使用真实凭据。
10. 完整门禁：Ruff format/check、mypy、import-linter、Alembic heads、完整 pytest、OpenAPI check；
    无 skip/xfail、弱断言、测试专用业务分支或 secret 泄漏。

真实 GitLab Smoke Test 延后到 GitOps/Secret/网络就绪后的独立 Gate，至少覆盖目标 GitLab 版本、
Project Profile、dev/main protection、MR 创建回读、准确 SHA merge、重复请求、Webhook 与超时故障注入。
缺少该证据不阻塞本批代码完成，但禁止宣称真实 Provider 已通过。

## 验收标准

1. 一个批次跨 Requirement 与 Source Control 两个 owner，但没有跨模块深层导入、跨 schema 写入或
   Connector 双写。
2. 只有当前负责人且资格有效时才能开始 WorkItem 和提交 Integration MR 请求；只有具备
   `merge_request.merge` 的当前有效人员能请求 Integration Merge。
3. MR source/target 只能由 Branch Binding 与服务端常量推导，调用者不能指定或覆盖。
4. MR 创建和 Merge 都先落 Effect、后发外部调用、再 GET 回读；结果未知绝不猜测成功。
5. Merge 始终绑定准确 head SHA、使用 merge commit、保留 source branch，并尊重 GitLab checks 与
   branch protection；merge 已发生但 source branch 缺失时保留事实并阻塞后续 Formal MR。
6. Webhook 只触发 Reconciliation；没有有效平台 Merge Effect 的外部合并形成 Drift，而不是业务成功。
7. 成功后 WorkItem 保持 `VERIFYING/INTEGRATED`，等待下一批外部验证证据与
   `IntegrationBaselineEvidence`；Artifact、Acceptance、Formal MR 与 main Merge 不进入 diff。
8. 架构门禁、数据库门禁、完整测试与 OpenAPI check 全部通过。
