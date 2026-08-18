# V0.2 Super Admin 与导航目录补丁设计

- 日期：2026-08-18
- 状态：设计已确认，待用户审阅与实施计划
- 仓库：`engineering-platform-backend`
- 基线：`api-v0.2.0`
- 目标发布：`api-v0.2.1`
- 跨仓设计：`engineering-platform-docs/docs/superpowers/specs/2026-08-18-v02-super-admin-navigation-and-mock-retirement-design.md`

## 目标

为真实 `/me` 与 `/navigation` 补齐 V0.2 已激活治理能力，使前端能够删除全部 V0.2 Umi Mock。当前有效的 Super Admin 自动拥有显式、有限的 V0.2 Platform Capability 集合；Authorization 数据迁移补齐已完成页面的 route registry。实现不引入通配符、不按员工号授权，也不降低现有安全门禁。

首个 Super Admin 的目标环境 Bootstrap 员工号为 `00000000`，但它只作为运维 CLI 输入。若环境已经完成 Bootstrap，不重建、不覆盖账号。源码、迁移、测试、日志与提交不得包含固定密码或其他凭据。

## 有限能力投影

Authorization domain 定义唯一的 V0.2 Super Admin 自动能力集合：

```text
platform.home.read
platform.admin.access
audit.read
identity.account.manage
platform.organization.manage
platform.workspace.manage
platform.authorization.manage
platform.configuration.manage
platform.super_admin.manage
```

`platform.configuration.manage` 与 `platform.super_admin.manage` 仍属于不可普通授予的 reserved set；不得把其余七项加入 reserved set，否则会破坏普通 Grant。自动集合只在 Principal 的当前 `is_super_admin` 为真且 Scope 为 `PLATFORM` 时生效。

Principal 投影、请求级 `authorize` 与已有 Principal 的 `principal_has_capability` 共用同一集合和判断函数，避免 `/me` 显示能力但端点拒绝，或端点放行而导航缺失。自动能力不写入 `authorization.grant`；撤销 Super Admin 后立即停止派生，另行存在的普通 Grant 保持既有生命周期。

所有已有前置判定保持原顺序：Session、账号状态、授权版本与 convergence、Scope、Workspace Membership、资源条件及 Audit。未知 Capability、未来版本 Capability 与 Workspace Scope Capability 不在自动集合内，继续要求精确 Grant 并默认拒绝。

## 导航目录迁移

在 Authorization migration branch 新增 `0005_authorization_v02_routes` 迁移，依赖 `0004_authorization_pending_set`。保留既有 `home` 与 `admin`，新增六条确定记录：

| routeKey | name | order/sort | capability |
| --- | --- | ---: | --- |
| `audit` | 审计看板 | 7 | `audit.read` |
| `admin.workspaces` | 工作区管理 | 8 | `platform.workspace.manage` |
| `admin.organization` | 组织管理 | 9 | `platform.organization.manage` |
| `admin.users` | 用户管理 | 13 | `identity.account.manage` |
| `admin.grants` | Grant 管理 | 14 | `platform.authorization.manage` |
| `admin.policies` | Policy 发布 | 15 | `platform.configuration.manage` |

每行 `scope_type='PLATFORM'`，`sort` 与 `meta.order` 相同，`meta.name` 与表中 name 相同。升级不得插入 V0.3+ 原型路由，不得删除环境未知扩展行；若任一受管 routeKey 已存在但字段冲突，迁移应失败并要求人工核查，不能静默覆盖异常数据。降级只删除上述六条且仅在字段仍与本迁移管理值一致时删除，不触碰 `home`、`admin`。

`/navigation` 继续按 Principal Capability 与 route registry 的精确三元组过滤。有效 Super Admin 应得到八个已激活 routeKey：基线的 `home`、`admin` 加新增六条；前端静态 registry 会把非菜单的 `admin` 用作管理入口/分组，并渲染其余七个菜单。

## 版本与 OpenAPI

应用版本提升到 `0.2.1`，重导出并提交 `openapi.json`。本补丁不新增或改变 URL、DTO、状态码或安全声明；除 `info.version` 与导出器确定性输出外，不应出现契约差异。发布 tag 必须为 `api-v0.2.1`，使 CI 生成可验证 Artifact 与 SHA-256，供前端更新 lock。

## 测试策略

采用 TDD，先增加失败断言，再做最小实现：

1. domain/application 单测锁定精确九项集合，断言任意未来 Capability 不自动放行。
2. `resolve_principal`、`authorize`、`principal_has_capability` 对 Super Admin 一致；普通 Principal 的显式 Grant 行为保持不变。
3. Super Admin 撤销、账号停用、Session 无效、授权版本陈旧、projection/repository 异常继续 Fail Closed。
4. migration 测试验证升级新增六条、字段精确、未知扩展保留、冲突失败与降级边界。
5. API 测试验证 `/me` 返回九项能力，`/navigation` 返回精确八个 routeKey 且不含 `tasks`、`workspaces`、`admin.menus` 等原型项。
6. 版本与 OpenAPI drift 测试更新到 `0.2.1`。

本机运行受影响 pytest、Ruff、Mypy、import-linter 与 OpenAPI check；完整 pytest 与镜像构建由 CI 执行。合并、tag 与环境迁移前均不得用测试跳过或硬编码响应替代验证。

## 发布与验收

后端代码合并、CI 全绿并发布 `api-v0.2.1` 后，目标环境先运行 migration Job，再部署同一提交镜像。使用真实 Session 冒烟验证 `/me`、`/navigation` 和每项管理 API；只有这一步通过，前端才能删除运行时 Mock 并发布。

环境 Bootstrap 如尚未发生，由受限运维流程把 `00000000` 传给既有 CLI，临时凭据只在受控终端一次展示。验收证据记录账号标识、版本、routeKey、Capability、request ID 与 Audit 结果，不记录任何秘密。

## 非目标

- 不创建万能 Role、Capability wildcard、员工号特判或全局授权 bypass。
- 不新增菜单 CRUD、OpenAPI 路由或 V0.3+ 能力。
- 不删除历史普通 Grant，不修改 Workspace Membership、Owner 或 Policy 语义。
- 不修改 GitOps Desired State，不把代码或 Artifact 发布等同于环境已验收。
- 未收到 `【同步进度】`，不修改 `docs/superpowers/progress/current.md`。
