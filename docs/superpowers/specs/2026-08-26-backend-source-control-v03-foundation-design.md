# V0.3 Source Control Foundation · 后端设计

- 日期：2026-08-26
- 状态：待用户确认，尚未进入 TDD 实现
- 仓库：`engineering-platform-backend`
- 目标分支：`codex/backend-source-control-v0.3`
- 代码基线：`main@631979b759944b7a95bc3cd380a573fbfc4b6aab`
- 第一批等价性：合并前 `4fb108837452829c74aff95df74f54bb2f4382df` 与合并后
  `631979b759944b7a95bc3cd380a573fbfc4b6aab` 的 tree 均为
  `63de8ed76f4bdd46fd36cf6443515bf088b7b188`
- 架构依据：`engineering-platform-docs@541b186` 的
  `architecture/{02,05,06,07,08,12,appendix-parameters}.md`
- Provider 依据：[GitLab Branches API](https://docs.gitlab.com/api/branches/) 与
  [GitLab Webhooks](https://docs.gitlab.com/user/project/integrations/webhooks/) 当前官方文档

## 目标与批次边界

本批建立 V0.3 的 Source Control 基础：消费 Requirement 模块已经产生的
`requirement.repository-binding.requested` Outbox，以独立 `source_control` 深模块保存
Workspace Repository、Requirement Binding Request Inbox、不可变 Repository Branch Binding、
GitLab Webhook Inbox 与 Source Control Effect Ledger；在校验当前负责人、Workspace Repository
授权与外部仓库事实后，从 GitLab `main` 当前 commit 创建确定性任务分支，并把 Binding Ready 或
Binding Blocked 只经 Requirement 包根公开 Facade 回传。

本批明确不包含：

- Integration/Formal Merge Request、Review、Merge 或 source branch 删除。
- Commit Push、Agent Credential Broker、Sandbox、Attempt 或 Artifact/Evidence。
- Acceptance、Chat/Model、前端页面、公开 Workspace Repository 管理 API。
- GitOps 部署、Capability 激活、真实凭据配置与真实 GitLab Smoke Test。
- 自动分配 WorkItem 负责人；无负责人或负责人资格失效时保持 Fail Closed。

真实 GitLab 凭据和环境 Smoke Test 延后，不降低代码门禁。生产依赖未配置时，分支创建与 Webhook
均 Fail Closed；Reconciliation 代码仍可由 Fake Adapter 与真实 PostgreSQL 完整验证。本批不是 V0.3
Release Gate，也不自动提升 API 版本、创建 tag 或宣称能力已部署。

## 方案选择

采用“单一 Source Control owner + 独立 schema + 稳定 Facade/Port”的方案。

- `source_control` 拥有仓库授权投影、分支命名、外部 Effect、Webhook Inbox、Reconciliation 与
  不可变 Binding；Requirement 不复制 GitLab 状态。
- Requirement 继续拥有 WorkItem、Outbox 与 `repositoryState`，只新增公开的 Outbox relay/query
  Facade 和现有 Binding Ready/Blocked 回调；Source Control 不直读或直写 `requirement` schema。
- GitLab Adapter 只做协议转换与外部效果观察，不拥有平台 Capability、Assignment 或 WorkItem 状态。
- 外部 HTTP 调用不持有数据库事务；每次 Source Control 写事务只改 `source_control` schema，
  Requirement 回调在独立事务中以稳定幂等键收敛。

未采用的方案：

- 把 GitLab 字段继续加到 Requirement：会让 Workflow owner 同时拥有 Provider 协议、外部副作用与
  对账，删除 `source_control` 后复杂度不会消失，只会散到 Requirement。
- 让独立 Connector 直接读写两个 schema：短期代码少，但破坏模块 Facade、最小权限与单模块事务，
  恢复时无法证明哪个 owner 写下了事实。
- 仅依赖 Webhook 推进状态：Webhook 会重复、乱序、缺失或被禁用，只能作为 Reconciliation 提示，
  不能成为 Binding 的权威事实源。

### 深模块删除测试

`source_control` 的外部接口保持在少量用例：接纳 Binding Request、处理 Request、对账 Effect、接纳
已签名 Webhook、查询 Binding。内部隐藏确定性命名、两类 Inbox、GitLab read-create-read、Effect
状态机、回调重放与错误归一。若删除该模块，这些规则会分别重新出现在 Requirement、bootstrap、
Webhook route 与 worker 中，因此该模块具有不可替代的独立责任和足够深度。

## 统一领域语言

- **Workspace Repository**：Workspace 已授权 GitLab Project 的受控投影；不是第二套代码 Project。
- **Binding Request Inbox**：Requirement Outbox 消息在 Source Control 中的幂等消费事实。
- **Repository Branch Binding**：任务分支经远端回读验证后形成的不可变
  `repositoryId + baseCommitSha + branchName` 事实。
- **Source Control Effect**：一次 GitLab 外部写操作从计划、发出、未知到收敛结果的 Effect Ledger。
- **Webhook Inbox**：签名验证通过后按 `webhook-id` 去重的受控事件摘要；不是领域状态。
- **Reconciliation**：通过 GitLab 当前 Project/Branch/Commit 查询，把未知外部效果收敛为已证明的
 成功或结构化阻塞。

## 模块与 Deployable 边界

新增模块：

```text
control_plane/app/modules/source_control/
├── api/          GitLab 签名 Webhook ingress；不挂主业务 OpenAPI
├── application/  Outbox relay、Binding Saga、Effect/Reconciliation 与回调编排
├── domain/       名称、状态机、不可变量、结果与结构化错误
├── ports/        Repository、Requirement、Eligibility、GitLab、Secret/Policy、Clock/Random
└── adapters/     SQLAlchemy、Requirement Facade、授权资格、HTTPX GitLab、Webhook 验签
```

Source Control 仍是 Control Plane 模块化单体中的业务 owner。GitLab Connector 的进程入口放在
`control_plane/app/bootstrap/source_control_connector.py`，只装配 Webhook ingress 与 worker 用例，
不把 Webhook route 挂进 `create_app()` 的浏览器业务 API，也不把 Connector 描述为已部署。后续 GitOps
可以用同一代码构建独立 Deployable；本批不创建部署清单。

其他模块只能经 `control_plane.app.modules.source_control` 包根调用公开 Facade。Source Control 对
Identity、Workspace、Authorization 与 Requirement 的调用只经各自包根或由 Adapter 满足的 Port；
import-linter 必须证明所有模块不能深层互导。

## Requirement 公开 Seam

当前 Outbox payload 只有 `workItemId` 与 `repositoryId`，必须兼容已存在的消息，不能靠扩展 payload
假装历史消息拥有新字段。Requirement 包根新增以下内部 Facade：

- `claim_repository_binding_requests(...) -> tuple[RepositoryBindingRequestMessage, ...]`：按
  `availableAt,id` 领取 `PENDING|FAILED` 消息，原子增加 attempts 并把 `availableAt` 推到 lease 截止；
  不把未消费消息标成成功。
- `acknowledge_repository_binding_request(...) -> RequirementDto`：Source Control Inbox 已持久化后，
  幂等把对应 Outbox 标为 `PUBLISHED`；Requirement 仍为 `CREATED` 时同事务进入 `PREPARING`，已经
  前进的状态保持不回退。
- `release_repository_binding_request(...)`：受理失败时保存安全 error code 与下一次可用时间；不保存
  Connector 原始错误正文。
- `get_repository_binding_context(...) -> RepositoryBindingContext`：返回当前 WorkItem revision、
  Requirement/Workspace、类型/标题、负责人、Assignment、required capabilities 与 repositoryId；
  不暴露 ORM Row 或 Repository。

现有 `record_repository_binding(...)` 与 `record_repository_binding_blocked(...)` 继续是唯一的
Ready/Blocked 写入口。Source Control 回调使用由 Effect ID 派生的稳定幂等键；回调 revision 冲突时
重新读取 Binding Context 并重新验证，绝不跨模块强写或 Last-write-wins。

Requirement 的 Outbox Relay 顺序固定为：

```text
Requirement claim（短事务）
→ Source Control accept_binding_request（独立短事务，Inbox unique message ID）
→ Requirement acknowledge（独立短事务）
```

Source Control 已接纳但 Requirement ack 前进程崩溃时，原消息会再次投递，Binding Request Inbox
去重后返回同一结果，再次 ack 即可收敛；不需要分布式事务。

## Source Control 聚合与持久化

新增独立 `source_control` schema、`source_control_rw` NOLOGIN role 和 Alembic head。角色只获得本
schema 必要权限，不获得 `requirement`、`authorization`、`workspace` 或 `identity` 表权限。

### `source_control.workspace_repository`

至少保存：

- `repositoryId`、`workspaceId`、`provider=GITLAB`
- GitLab `projectId`、`projectPath`、`defaultBranch=main`
- `connectionRef`、`credentialSecretRef`、可选 `webhookSigningSecretRef`
- `status=AUTHORIZED | REMOVED`、`revision`、创建/更新时间

只保存 Secret Reference，不保存 PAT、Webhook signing token 或其他凭据。注册与移除只提供内部
Facade；移除后拒绝新 Binding，但历史 Binding、Effect、Inbox 与 Audit 保留。

### `source_control.binding_request_inbox`

以 Requirement Outbox `messageId` 为主键，保存 payload hash、WorkItem/Repository ID、
`RECEIVED | PROCESSING | PROCESSED | FAILED`、attempts、availableAt、lastErrorCode 与时间戳。
同 message ID 同 hash 幂等成功；同 ID 不同 hash 是安全冲突并 Fail Closed。

### `source_control.source_control_effect`

本批只允许 `operation=CREATE_TASK_BRANCH`。每个 WorkItem 唯一 Effect 保存：

- Effect ID、稳定 effect key、WorkItem/Requirement/Repository ID
- 全局递增 `workItemNumber`、确定性 `branchName`
- `baseCommitSha`、request fingerprint、attempts、nextReconcileAt
- `state=PLANNED | IN_FLIGHT | UNKNOWN | RECONCILIATION | SUCCEEDED | BLOCKED`
- `lastErrorCode`、`requirementCallbackState=PENDING | ACKED | FAILED`
- 创建、更新时间、完成时间

外部调用前必须先持久化 `PLANNED`；发出 POST 前提交 `IN_FLIGHT`。任何已经可能到达 GitLab、但
不能证明结果的响应、异常或超时都进入 `UNKNOWN` 并安排 Reconciliation，不能改成成功或直接创建
Binding。

### `source_control.repository_branch_binding`

只在 GitLab 回读证明目标分支准确指向 Effect 的 `baseCommitSha` 后插入，至少保存 Binding ID、
WorkItem/Requirement/Workspace/Repository、workItemNumber、baseCommitSha、branchName、effectId 与
createdAt。对 `source_control_rw` 只授 `SELECT/INSERT`，数据库唯一约束保证一个 WorkItem 只有一个
Binding、同一 Repository 的 branchName 不重复；不提供 UPDATE/DELETE。

### `source_control.webhook_inbox`

签名通过后以 `(repositoryId, webhookId)` 去重，保存 webhookTimestamp、payload SHA-256、
Provider Event UUID、event type、object kind、ref、before/after SHA、安全处理状态与时间戳。不得保存
签名、signing token、PAT、完整原始 body 或源码内容。同 ID 同 digest 返回相同接纳结果；同 ID 不同
digest 记录安全冲突并拒绝。

## 负责人和仓库准入

处理 Binding Request 时重新读取 `RepositoryBindingContext`，并同时满足：

```text
assignmentState = ASSIGNED
AND humanOwnerId 存在且当前账号可用
AND humanOwnerId 是当前 Workspace Formal Member
AND humanOwnerId 当前具备 WorkItem requiredCapabilities 的 Workspace Scope Grant
AND Workspace Repository 状态为 AUTHORIZED 且 workspaceId/repositoryId 匹配
AND Provider Project Profile 与本地授权投影匹配
```

Source Control 只验证已有 Assignment，不在本批自动分配或改派。生产 Requirement 当前默认的
Fail-Closed Assignment Guard 会让真实 WorkItem 保持 UNASSIGNED；在后续 Assignment 批次接入前，
真实请求会得到结构化 `OWNER_UNASSIGNED` Blocked，而测试通过受控 Fake/fixture 验证 Happy Path。

## 确定性分支创建 Saga

分支名固定为 `type/wi-<全局递增号>-<semantic-slug>`。`workItemNumber` 在创建 Effect 的数据库事务中
从 `source_control.work_item_number_seq` 分配并持久化，重试不重新取号。slug 使用 NFKC 归一化，
保留 Unicode 字母与数字，把其他连续字符压成 `-`，限制长度并拒绝 Git ref 禁止序列；归一后为空
时使用 Requirement type。相同 WorkItem 始终得到相同 branchName。

外部流程固定为：

```text
重新验证 Binding Context 与 Workspace Repository
→ GitLab GET main，记录准确 baseCommitSha
→ 持久化 Effect 的 base SHA、branchName 与 PLANNED
→ 提交 IN_FLIGHT
→ GitLab POST branch，ref 使用准确 commit SHA，不使用移动的 main 名称
→ GitLab GET task branch 回读
→ commit.id == baseCommitSha：Effect SUCCEEDED + 插入不可变 Binding
→ 不同 SHA：Effect BLOCKED / BINDING_CONFLICT
→ 结果不可得：Effect UNKNOWN，进入 Reconciliation
→ 通过 Requirement Facade 幂等回传 Ready 或 Blocked
```

GitLab Branches API 允许 `ref` 使用 branch 名或 commit SHA。本设计先读取 `main` 的 commit ID，再把
准确 SHA 作为 create `ref`，防止两次请求间 `main` 移动导致 Binding 记录与远端事实不一致。若同名
分支已存在，只在回读证明 SHA 完全一致且 Effect/WorkItem 归属一致时复用；不同 SHA 或无法证明归属
一律 Blocked。

## UNKNOWN 与 Reconciliation

Reconciler 只领取 due 的 `UNKNOWN` Effect，将其 CAS 到 `RECONCILIATION` 后查询 GitLab：

- 分支存在且 SHA 等于 base：完成 Effect、插入 Binding、重放 Ready 回调。
- 分支存在但 SHA 不同：`BLOCKED/BINDING_CONFLICT`，重放 Blocked 回调。
- 分支不存在：使用同一 effect key、branchName 与 base SHA再次执行 create-readback；不分配新名称。
- Project/授权被移除或 owner 已失效：停止新外部写，`BLOCKED` 并回传安全 reason。
- 查询仍超时或结果未知：回到 `UNKNOWN`，按 PolicyPort 提供的 schedule 安排下一次对账。

Webhook 只把匹配 Repository/branch 的 UNKNOWN Effect 提前变为 due；最终仍由 GET Branch 证明，
Webhook payload 不直接创建 Binding 或推进 Requirement。

## GitLab Webhook 安全 Contract

仅接受 GitLab Standard Webhooks signing token：

- 必须同时存在 `webhook-id`、`webhook-timestamp`、`webhook-signature`。
- 使用 Secret Reference 解析 `whsec_` signing token，按
  `{webhook-id}.{webhook-timestamp}.{raw-body}` 计算 HMAC-SHA256。
- `webhook-signature` 可能含多个空格分隔的 `v1,<base64>` 值，逐个常量时间比较。
- timestamp 必须满足 `SourceControlPolicyPort` 提供的 replay window；Policy 不可用时拒绝。
- 先验签，再解析 JSON，再持久化去重；验签失败不保存 body。
- 不接受明文 `X-Gitlab-Token` fallback。

GitLab signing token 在 GitLab 19.0 引入、19.1 GA。实际 GitLab 未启用该能力、版本不兼容或 signing
token 未配置时，不注册/启用 Webhook；Binding 正确性完全依靠 Reconciliation，不用较弱 token
降级。

## 错误与回调映射

Source Control 保存细粒度内部错误码，例如：

- `OWNER_UNASSIGNED`、`OWNER_INELIGIBLE`
- `REPOSITORY_NOT_AUTHORIZED`、`REPOSITORY_REMOVED`
- `PROJECT_NOT_FOUND`、`ACCESS_DENIED`、`DEFAULT_BRANCH_NOT_FOUND`
- `BINDING_CONFLICT`、`PROVIDER_UNAVAILABLE`、`EXTERNAL_RESULT_UNKNOWN`
- `WEBHOOK_SIGNATURE_INVALID`、`WEBHOOK_REPLAYED`、`WEBHOOK_ID_CONFLICT`

Requirement 回调只使用其公开、安全的 `RepositoryBindingBlockedReason`。本批扩展该枚举与 DB CHECK
以表达 `OWNER_UNASSIGNED`、`OWNER_INELIGIBLE`、`REPOSITORY_NOT_AUTHORIZED`、
`RECONCILIATION_PENDING`；Provider 细节通过固定映射归一，不把 URL、PAT、HTTP body 或 GitLab 原始
错误写入 Requirement、Audit 或响应。

`UNKNOWN` 首次形成时回传 `RECONCILIATION_PENDING`，WorkItem 可见为 Blocked；后续确认成功允许现有
`record_repository_binding` 清除 blocked 字段并恢复 BOUND。Source Control Binding 已成立但
Requirement 回调暂时失败时，Effect 保持 `SUCCEEDED` 且 callbackState 非 ACKED，由 worker 继续重放；
不能回滚已证明的 GitLab 事实。

## 事务、并发与安全

- 不在数据库事务内执行 GitLab HTTP 或 Requirement Facade 调用。
- 所有命令使用稳定 idempotency/effect key；所有状态推进使用 revision/CAS。
- Binding 表不可更新；Effect 和 Inbox 只允许显式状态转换。
- GitLab access token 与 webhook signing token 只经 Secret Port 短期解析，不进 DB、日志、Audit、
  Exception detail 或测试 fixture 明文快照。
- GitLab endpoint/project/branch/path 均服务端解析；Webhook route 的 repositoryId 只作查找键，仍
  校验 payload project ID 与授权投影一致。
- 外部 error body 只转为 allowlist reason code；调试日志最多记录 Effect ID、Repository ID、HTTP
  status class、request/correlation ID 与 payload digest。
- Connection/Repository 变更、Binding、base SHA、Effect、Webhook 接纳/拒绝、Reconciliation 与回调
  都写安全 Audit；不记录源码或凭据。

## 测试策略

1. Domain：Effect 状态矩阵、确定性 Unicode branch name、Binding 不变量、错误映射。
2. Requirement Facade：Outbox claim/lease/ack/release、历史 payload 兼容、Binding Context 与 CAS 回调。
3. Migration/SQL：五张表、sequence、独立 head、约束、最小权限、Binding 不可更新、downgrade 保留
   其他 schema。
4. Application：重复消息、重复处理、owner/repository guard、崩溃窗口、回调重放与并发 claim。
5. GitLab Adapter：HTTPX MockTransport 验证 GET main → POST exact SHA → GET branch；创建超时、409、
   401/403/404/5xx 与 readback 收敛。
6. Webhook：官方算法生成签名；缺头、错签、过期、明文 token-only、重复 ID 与同 ID 不同 digest。
7. Reconciliation：存在同 SHA、不同 SHA、不存在后重试、再次未知、仓库移除与 owner 失效。
8. PostgreSQL E2E：Requirement Outbox → Source Control Inbox/Effect/Binding → Requirement Ready/Blocked
   回调；跨 schema 零直写，以 Fake GitLab 隔离真实凭据。
9. 完整门禁：Ruff format/check、mypy、import-linter、Alembic heads、完整 pytest、OpenAPI check。

真实 GitLab Smoke Test 是后续 GitOps/Secret/网络就绪后的独立 Gate：必须验证目标 GitLab 版本和
signing token 能力、授权 Project、main SHA readback、创建/回读任务分支、重复请求与故障注入；缺少
该证据不阻塞本批代码完成，但禁止宣称真实 Provider 已通过。

## 验收标准

1. `source_control` 深模块、独立 schema/head、最小角色与 import-linter 契约成立。
2. 相同 Requirement Outbox message 重复投递只形成一个 Inbox、一个 Effect 和一个 Binding。
3. 分支名与 base SHA 在外部调用前持久化，GitLab 调用严格 read main → create exact SHA → read branch。
4. 超时/结果未知从不形成 Binding，进入 UNKNOWN/RECONCILIATION 并可重放收敛。
5. Webhook 只接受 Standard Webhooks HMAC-SHA256，按 `webhook-id` 去重，无明文 token fallback。
6. Source Control 不访问 Requirement 私有表；Ready/Blocked 只经 Requirement 包根 Facade。
7. MR、Merge、Artifact、Acceptance、Chat/Model、前端与 GitOps 不进入 diff。
8. 全部门禁通过；无 skip/xfail、弱断言、测试专用业务分支或凭据泄漏。
