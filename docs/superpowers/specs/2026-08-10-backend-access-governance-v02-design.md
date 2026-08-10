# V0.2 访问治理闭环 · 后端设计

- 日期：2026-08-10　状态：设计已批准，待实施计划
- 仓库：`engineering-platform-backend`　对应前端 spec：`engineering-platform` 仓 `docs/superpowers/specs/2026-08-10-frontend-access-governance-v02-design.md`
- 契约依据：架构仓 `architecture/{01,06,07,08,10,12,appendix-parameters}`；例外 `DEV-003`（`architecture/deviations.md`）

## 目标

用户以本地身份（8 位员工号 + 正式密码 + 强制 TOTP）登录并持有服务端可撤销 Session；所有受保护 API 在服务端按 `Session → Security Floor → Capability + Scope → Membership → 资源门禁` 判定并 Fail Closed；Super Admin 受控管理账号、组织、Workspace、Grant 与 `PLATFORM_POLICY` 发布；撤销对新请求即时生效；全部治理动作与领域事实同事务留审计。收口发布 `api-v0.2.0`。

## 开工门禁与硬前置

V0.2 模块开发在 V0.1 `ACCEPTED` 后开始（12 篇里程碑顺序）。以下两项是 V0.1 骨架修缮，**立即开工，不属 V0.2 范围**：

1. **契约守护测试**：断言 pyproject 中 import-linter 契约的 containers 与 forbidden 模块集合 == 由 `modules/` 实际目录派生的集合；新增模块未登记契约即测试失败。
2. **错误契约簇**：`ProblemDetails` 组件化进 OpenAPI 并为现有全部操作声明 4xx/5xx；readyz 补 200 响应 schema；新增 `X-Request-ID` 中间件（生成/透传，写入响应头、Problem 扩展字段 `requestId` 与日志字段）；重导出 `openapi.json` 并验证前端 `openapi:check` 判定"新增响应声明非 breaking"。

## 范围裁剪（已批准决策）

- **纳入**：Break-glass 带外恢复（仅 GitOps 锁定的一次性 Job/CLI，不走 Web 页面与普通平台 API）。
- **缓（保留边界、能力关闭）**：有时效协作关系（V0.3）；Capability Template 预选；Workspace 级 Policy Override。
- **不建**：`audit-worm` archiver（对象存储 Capability 激活前 PostgreSQL 即权威审计事实）；Valkey / NATS（Session 与投影由 PostgreSQL 承载，见下）；OpenBao（DEV-003 过渡，`SecretManagerPort` 抽象）。
- **Passkey**：不建表、不留字段、不写 API（01 篇明令）。

## 架构

### 模块布局（各五层 + 独立 schema / 迁移目录 / 最小权限 runtime 角色）

```text
modules/
  identity/        扩展：账号、认证材料、Session、Super Admin 生命周期
  organization/    新增：经理 → Leader → 一层员工组织事实
  workspace/       新增：Workspace、Owner/Leader 治理、FormalMembers 投影
  authorization/   新增：Grant、判定链、授权版本、me/navigation 投影
  configuration/   新增：PLATFORM_POLICY 基础生命周期（10 篇基础契约全量，增强缓）
  audit/           扩展：接入 01 篇全部 Audit Trigger
shared/
  security/        Argon2id+pepper、TOTP 原语、CSRF Origin/Fetch-Metadata 校验、Cookie 签发
  observability/   结构化日志 + request id（兑现 V0.2 承诺）
```

横切规则照旧：模块间仅公开 Facade；单事务只写单模块；领域写入 + Audit + （将来）Outbox 同一 PostgreSQL 事务；API 层 CamelModel DTO，domain 层普通 BaseModel。

### 关键技术决策

- **Session 与授权投影承载 = PostgreSQL**：session 表（cookie 随机 token 的哈希、principal、过期与撤销态）、授权版本表（per principal 单调版本）。撤销 = 行更新 + 版本提升，每个受保护请求实时校验。接口藏在 `SessionStorePort` 后，将来切 Valkey 只换 Adapter。10 人规模性能充裕，且符合 06"未激活不建空数据服务"。
- **Secret 供给 = DEV-003**：pepper 与 TOTP 加密密钥由运维带外生成入 k8s Secret，文件卷挂载；应用经 `SecretManagerPort`（本地开发从 `.env` 指向的文件读取）。
- **认证密码学**：密码 Argon2id（argon2-cffi）+ 独立 salt + pepper；TOTP RFC 6238（30s 周期、±1 窗口、同码单次消费、每 Challenge 5 次上限）；TOTP Secret 以 AES-GCM 加密存储（密钥来自 SecretManagerPort），确认后不可回读明文。
- **CSRF**：Cookie `Secure + HttpOnly + SameSite=Lax`；写请求另校验 Origin/Sec-Fetch-Site（SameSite 不作唯一防线）。
- **写命令**：自 V0.2 起变更命令强制 `Idempotency-Key`，并发写走 ETag/`If-Match`。

## 领域要点

### identity

- 账号四态：初始化受限 / 启用 / 停用 / 受限；员工号为可含前导 0 的 8 位数字字符串，环境内唯一。
- 临时密码：创建账号 / 重置密码时签发，密码学随机、一次展示、默认 24h 有效；首次成功使用**原子消费**，换取仅能完成初始化的受限 Bootstrap Session；中断后必须重新签发。
- 正式密码 Security Floor：15～64 位、大小写与特殊字符复杂度、弱密码与已知泄露密码检查（以内置离线弱口令表起步）、账号上下文检查；只存派生结果。
- Session：空闲失效（默认 60min）、同账号上限（默认 3，超限逐出最旧）、仅认证 API 活动续期；密码/TOTP 重置、停用与安全事件级联撤销。
- 登录退避：同账号连续 5 次失败后指数退避 30s 起、上限 15min，成功或 24h 清零，服务端计数并写安全 Audit。
- Super Admin：环境唯一一次 bootstrap（CLI 命令，见 break-glass 同族工具）创建首个；保留能力 `platform.configuration.manage` / `platform.super_admin.manage` 不可普通授予；增删需当前 Super Admin + 全新 TOTP Challenge + 原因；至少保留一个有效 Super Admin。

### organization / workspace

- 组织为经理 → Leader → 一层普通员工的无环结构；变更先验目标状态、层级与无环，成功即触发投影失效与重算。
- `FormalMembers(W) = L(W) ∪ (∪ R(l))` 物化为投影表（稳定用户 ID 去重、可随时重建）；Workspace 创建/Owner 转让/Leader 变化/直属变化/账号状态变化同步触发重算（无消息链，同步执行）。
- Owner 门禁：每 Workspace 恰一个 Owner；名单治理要求 Capability + Scope + Owner 事实两道门禁。

### authorization

- `Grant = (principal, capability, scope, source, validFrom?, validTo?, status, version)` 成对保存；判定链顺序固定，任一环失败即 Deny + 必要 Audit。
- 授权版本：Grant / Membership / 账号状态 / 相关 Policy 变化即提升受影响 principal 的版本；缓存只能加速，不能延长失效授权。
- `me`（Principal、组织摘要、Workspace、有效 Capability）与 `navigation`（预注册 routeKey、Capability、Scope、排序、元数据）投影真实化；菜单可见性不是授权结论。
- FastAPI dependency `require_capability(capability, scope_resolver)` 统一施加；投影或策略不可读时 Fail Closed（503/403，不放行）。

### configuration（仅基础生命周期）

- Typed Schema 注册（identity namespace 起步）；Draft（Owner、ETag、`DRAFT → ARCHIVED`、Base 过期标 STALE）；服务端 Validation（类型/范围/跨字段/Security Floor）；Impact Preview（前后值 + 影响说明，不裸 JSON diff）；Publish = 同事务原子切 Active Pointer + 不可变 Policy Snapshot + 当前 Super Admin 重校验 + 全新 TOTP + 原因；乐观并发（Base 落后返回 Conflict）；Rollback = 从历史 Snapshot 开新 Draft 重走全程。Draft 自动归档（默认 30 天无 Meaningful Activity，后台任务幂等执行）。
- 增强契约（Takeover / Rebase / Promotion / Divergence）不实现，也不以简化实现绕过基础生命周期。
- 首批 Policy Key（默认值以 appendix 为准）：临时密码有效期 24h；密码过期周期（永不/90/180 天）；同账号 Session 上限 3（1～10）；Session 空闲 60min（15～240）；登录退避参数；TOTP 尝试上限 5；Draft 自动归档等待期 30 天。

### audit（扩展）

接入 01 篇全部 Trigger：账号创建与状态、临时密码签发/消费、密码与 TOTP 重置、Session 撤销、组织变化、Owner/Leader 变化、成员投影重算摘要、Grant 变化、Super Admin 生命周期、配置授权命令、带外恢复。摘要含目标稳定标识、动作、结果、原因、前后版本与授权版本；绝不含任何凭据材料。与领域写入同一事务提交。

### break-glass 带外恢复

后端提供 CLI（`python -m control_plane.tools.recovery`）：验证执行前提后为最后一个不可用 Super Admin 重新签发一次性受限资格（等价临时密码 + 强制重走初始化），双证据（平台 audit 表 + 进程日志）。gitops 仓持有一次性 Job 模板与 runbook（交 ③），平台 Web/API 不暴露任何入口。

## API 面（`/api/v1`，camelCase，Problem Details）

- 认证：`POST /auth/login`（员工号+密码 → 返回挑战态）→ `POST /auth/totp`（TOTP → Set-Cookie）；`POST /auth/logout`；Bootstrap：`POST /auth/bootstrap/password`、`POST /auth/bootstrap/totp/enroll`（返回 provisioning URI/二维码内容）、`POST /auth/bootstrap/totp/confirm`。
- 当前用户：`GET /me`、`GET /navigation`（真实投影替换 V0.1 stub，形状保持兼容并扩展）。
- 账号管理：`GET/POST /admin/accounts`、`POST /admin/accounts/{id}/reset-password`（响应一次性临时密码，仅此一次）、`.../enable`、`.../disable`、`.../totp-reset`。
- 组织：`GET /admin/organization/tree`；`PUT /admin/accounts/{id}/superior`（设经理/Leader 归属）。
- Workspace：`GET/POST /admin/workspaces`、`POST /admin/workspaces/{id}/leaders`、`DELETE .../leaders/{accountId}`、`POST .../transfer-owner`、`GET .../members`。
- Grant：`GET/POST /admin/grants`、`DELETE /admin/grants/{id}`。
- Policy：`GET /admin/policies`（catalog+active）、`POST /admin/policies/{ns}/drafts`、`PATCH .../drafts/{id}`（ETag）、`POST .../drafts/{id}/validate`、`GET .../drafts/{id}/preview`、`POST .../drafts/{id}/publish`（TOTP+原因）、`POST /admin/policies/{ns}/rollback`、`GET /admin/policies/{ns}/versions`。
- Super Admin：`GET/POST /admin/super-admins`、`DELETE /admin/super-admins/{id}`（均 TOTP+原因）。
- 审计：`GET /admin/audit-events`（actor/target/时间过滤，cursor 分页 `{items,nextCursor}`）。
- 错误：401 未认证 / 403 拒绝 / 409 并发与状态冲突 / 422 校验失败，全部 Problem Details 且带 `requestId`；登录退避与 TOTP 限次响应携带 `Retry-After` 头与等待信息（前端只展示、不自行推算）；DTO 与状态码细节在 plan 中随 OpenAPI 冻结。

## 数据与迁移

- 每模块独立 Alembic 目录与分支（沿 V0.1 audit 模式），各配最小权限 runtime 角色（如 `identity_rw`）；跨模块只经 Facade 查询，不跨 schema JOIN。
- 投影表归属：members 投影在 workspace schema；授权版本与 navigation 投影在 authorization schema。
- 敏感存储：密码派生结果、TOTP 密文（AES-GCM）在 identity schema；任何表不存明文凭据。

## 测试策略

TDD（沿 SDD 惯例）。关键回归：

1. 撤销即时性：revoke/停用后下一请求即 401/403。
2. 越权：跨 Workspace 访问、无 Grant 动作、Membership 失效——全部拒绝且留 Audit。
3. 临时密码原子消费：并发使用仅一次成功。
4. TOTP：同码重放拒绝、5 次失败作废 Challenge、退避生效。
5. Session：空闲失效、同账号上限逐出、logout 后 cookie 失效。
6. 投影：任意事实变更后重建结果与增量结果等价。
7. Policy：Publish 乐观并发冲突、Snapshot 不可变、Rollback 生成更高版本。
8. Audit 同事务：领域写入失败不留 Audit，成功必有。
9. Fail Closed：投影/策略/Secret 不可读时拒绝而非放行。
10. Bootstrap 唯一性：Super Admin bootstrap 全环境仅成功一次。

## 验收标准

1. 质量门全绿（ruff/mypy/lint-imports/pytest/openapi drift）。
2. 端到端演示：建号 → 临时密码 → 初始化（密码+TOTP）→ 登录 → 授权动作 → 撤销 → 即时拒绝，全程 Audit 可查且 requestId 贯通。
3. `openapi.json` 含完整错误声明，前端 `openapi:check` 通过（非 breaking 或按规则升版）。
4. `api-v0.2.0` Release 发布。
5. DEV-003 已登记（完成）；break-glass CLI + runbook 交 ③ 回执。

## 非目标

Requirement/交付 Workflow、Agent/Sandbox、Passkey、WORM archiver、Valkey/NATS/OpenBao/Temporal、协作关系、Workspace Policy Override、Capability Template、DEV→PROD Promotion。
