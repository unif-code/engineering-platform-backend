# V0.2 Super Admin 与导航补丁 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布 `api-v0.2.1`，让有效 Super Admin 获得精确的 V0.2 Platform 能力集，并让真实 `/navigation` 返回全部已完成 V0.2 路由。

**Architecture:** Authorization domain 拥有唯一有限能力集合，Principal 投影与两条服务端授权入口复用同一判定函数；普通 Grant 与 reserved Capability 语义保持不变。Authorization `0005` migration 只管理六条新增 V0.2 route registry 数据，`/navigation` 继续通过 Capability 三元组过滤，不增加 API 或授权 bypass。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL 18、pytest、Ruff、Mypy

**Spec:** `docs/superpowers/specs/2026-08-18-v02-super-admin-navigation-design.md`

## Global Constraints

- 基线为 `api-v0.2.0`，目标 tag 必须为 `api-v0.2.1`，`control_plane.app.__version__` 与 OpenAPI `info.version` 必须一致。
- 自动能力只对当前 `is_super_admin=true`、`ScopeType.PLATFORM` 和精确九项集合生效。
- 不使用 wildcard、前缀匹配、员工号判断或全局 bypass；`00000000` 只由部署后的 Bootstrap 运维步骤使用。
- `platform.configuration.manage` 与 `platform.super_admin.manage` 保持 reserved；其余七项不能加入 `RESERVED_PLATFORM_CAPABILITIES`。
- 自动能力不写入普通 Grant；历史 Bootstrap Grant 不在本补丁删除。
- Session、账号状态、authorization version、convergence、Workspace Membership、资源门禁和 Audit 继续 Fail Closed。
- 不手改 `openapi.json`；只由 `uv run python scripts/export_openapi.py` 生成。
- 本地执行定向测试和静态门禁；PostgreSQL 全量 pytest 与镜像构建由 CI 完成。
- 未收到 `【同步进度】`，不修改 `docs/superpowers/progress/current.md`。

---

## File Map

- Modify: `control_plane/app/modules/authorization/domain/models.py` — 九项集合和精确 Super Admin Platform 判定。
- Modify: `control_plane/app/modules/authorization/domain/__init__.py` — 导出 domain 契约。
- Modify: `control_plane/app/modules/authorization/__init__.py` — 公开只读常量供测试和其他 Facade 消费。
- Modify: `control_plane/app/modules/authorization/application/decisions.py` — Principal 投影、请求授权和资源 guard 共用有限判定。
- Modify: `tests/authorization/test_reserved_capabilities.py` — 精确集合、普通 Grant 与未知/Workspace 能力回归。
- Create: `migrations/authorization/0005_authorization_v02_routes.py` — 六条 V0.2 route registry 数据。
- Modify: `tests/authorization/test_migration.py` — migration upgrade/conflict/downgrade 边界。
- Modify: `tests/authorization/test_api.py` — `/me` 与 `/navigation` 的 Super Admin 集成行为。
- Modify: `control_plane/app/__init__.py` — 版本 `0.2.1`。
- Modify: `tests/test_version.py` — 单源版本断言。
- Modify: `tests/test_e2e_access_governance.py` — release 版本断言。
- Modify (generated): `openapi.json` — 由应用导出。
- Modify: `docs/runbook-break-glass.md` — 目标环境首次 Bootstrap 员工号与凭据处理边界。

### Task 1: 建立有限 Super Admin Platform 能力单源

**Files:**

- Modify: `control_plane/app/modules/authorization/domain/models.py`
- Modify: `control_plane/app/modules/authorization/domain/__init__.py`
- Modify: `control_plane/app/modules/authorization/__init__.py`
- Modify: `control_plane/app/modules/authorization/application/decisions.py`
- Test: `tests/authorization/test_reserved_capabilities.py`

**Interfaces:**

- Produces: `V02_SUPER_ADMIN_PLATFORM_CAPABILITIES: frozenset[str]`。
- Produces: `is_v02_super_admin_platform_capability(capability: str, scope: Scope, *, is_super_admin: bool) -> bool`。
- Consumes: 现有 `Scope`、`ScopeType`、`AuthorizationPrincipal`、`RESERVED_PLATFORM_CAPABILITIES`。

- [ ] **Step 1: 把现有 reserved 测试升级为九项精确集合断言**

在 `tests/authorization/test_reserved_capabilities.py` 导入 `V02_SUPER_ADMIN_PLATFORM_CAPABILITIES`，把 `test_current_super_admin_fact_confers_both_reserved_capabilities_without_grants` 改名为 `test_current_super_admin_fact_confers_exact_v02_platform_capabilities_without_grants`。期望集合写成：

```python
EXPECTED_V02_SUPER_ADMIN_CAPABILITIES = {
    "platform.home.read",
    "platform.admin.access",
    "audit.read",
    "identity.account.manage",
    "platform.organization.manage",
    "platform.workspace.manage",
    "platform.authorization.manage",
    "platform.configuration.manage",
    "platform.super_admin.manage",
}

assert V02_SUPER_ADMIN_PLATFORM_CAPABILITIES == EXPECTED_V02_SUPER_ADMIN_CAPABILITIES
assert {
    (item.capability, item.scope.scope_type.value, item.scope.scope_id)
    for item in resolved.principal.capabilities
} == {(capability, "PLATFORM", None) for capability in EXPECTED_V02_SUPER_ADMIN_CAPABILITIES}
```

在同一测试中逐项调用 `authorize` 和 `principal_has_capability`，并增加两个拒绝断言：

```python
future = authorize(
    db,
    raw_token=token,
    capability="platform.future.manage",
    scope=Scope.platform(),
    dependencies=dependencies,
    decision_dependencies=decision_dependencies,
)
assert future.code is DecisionCode.DENIED

workspace_scoped = authorize(
    db,
    raw_token=token,
    capability="platform.workspace.manage",
    scope=Scope.workspace("workspace-1"),
    dependencies=dependencies,
    decision_dependencies=decision_dependencies,
)
assert workspace_scoped.code is DecisionCode.DENIED
```

- [ ] **Step 2: 运行测试观察 RED**

Run:

```powershell
uv run pytest tests/authorization/test_reserved_capabilities.py -v
```

Expected: FAIL，首先因为 `V02_SUPER_ADMIN_PLATFORM_CAPABILITIES` 尚未导出，或投影仍只有两项 reserved Capability。若无 PostgreSQL 而被 skip，启动仓库既有 PostgreSQL 18 compose 后只重跑该文件；不要把 skip 当作通过。

- [ ] **Step 3: 在 domain 定义集合与精确判定函数**

在 `models.py` 的现有 Capability 常量旁增加：

```python
V02_SUPER_ADMIN_PLATFORM_CAPABILITIES = frozenset(
    {
        "platform.home.read",
        "platform.admin.access",
        "audit.read",
        "identity.account.manage",
        "platform.organization.manage",
        "platform.workspace.manage",
        "platform.authorization.manage",
        PLATFORM_CONFIGURATION_MANAGE,
        PLATFORM_SUPER_ADMIN_MANAGE,
    }
)
```

在 `Scope` 定义后增加：

```python
def is_v02_super_admin_platform_capability(
    capability: str,
    scope: Scope,
    *,
    is_super_admin: bool,
) -> bool:
    return (
        is_super_admin
        and scope.scope_type is ScopeType.PLATFORM
        and capability in V02_SUPER_ADMIN_PLATFORM_CAPABILITIES
    )
```

从 domain `__init__.py` 和模块根 `__init__.py` 导出常量与函数。不得扩大 `RESERVED_PLATFORM_CAPABILITIES`。

- [ ] **Step 4: 让 Principal 投影去重并派生九项集合**

在 `_principal_capabilities` 中用稳定 key 去重普通 Grant 与自动能力：

```python
values: dict[tuple[str, ScopeType, str | None], ScopedCapability] = {}

def add_capability(item: ScopedCapability) -> None:
    values.setdefault(
        (item.capability, item.scope.scope_type, item.scope.scope_id),
        item,
    )
```

普通 Grant 通过既有 reserved 与 Membership 过滤后调用 `add_capability`。Super Admin 分支改为：

```python
if is_super_admin:
    for capability in sorted(V02_SUPER_ADMIN_PLATFORM_CAPABILITIES):
        add_capability(
            ScopedCapability(capability=capability, scope=Scope.platform())
        )
return tuple(values.values())
```

这样历史 Bootstrap Grant 与自动集合重叠时，`/me` 不会出现重复 Capability。

- [ ] **Step 5: 让两条授权入口复用同一精确判定**

在 `authorize` 完成 Session、version 与 convergence 校验后，先调用：

```python
if is_v02_super_admin_platform_capability(
    capability,
    scope,
    is_super_admin=bool(session.is_super_admin),
):
    try:
        capabilities = _principal_capabilities(
            repository,
            account_id=account_id,
            is_super_admin=True,
            decision_dependencies=decision_dependencies,
            dependencies=dependencies,
        )
    except Exception:
        return _unavailable(
            repository,
            dependencies=dependencies,
            actor=account_id,
            capability=capability,
            scope=scope,
            version=state.version,
            reason="effective capability projection unavailable",
        )
    return AuthorizationDecision(
        allowed=True,
        code=DecisionCode.ALLOW,
        principal=AuthorizationPrincipal(
            account_id=account_id,
            employee_id=str(session.employee_no),
            name=str(session.display_name),
            is_super_admin=True,
            authorization_version=state.version,
            capabilities=capabilities,
        ),
    )
```

该分支之后保留现有 reserved deny，再走普通 exact Grant。`principal_has_capability` 在 version/fence 校验后使用同一函数返回 `True`，之后保留 reserved deny 与普通 Grant/Membership 判定。不要只检查 `principal.capabilities` 数组，因为资源 guard 必须重新验证当前 version。

- [ ] **Step 6: 运行定向 GREEN 与静态检查**

Run:

```powershell
uv run pytest tests/authorization/test_reserved_capabilities.py tests/authorization/test_decisions.py -v
uv run ruff format --check control_plane/app/modules/authorization tests/authorization/test_reserved_capabilities.py
uv run ruff check control_plane/app/modules/authorization tests/authorization/test_reserved_capabilities.py
uv run mypy control_plane/app/modules/authorization tests/authorization/test_reserved_capabilities.py
```

Expected: 测试 PASS；Ruff/Mypy PASS。普通账号通过显式 Grant 的既有测试不得改变。

- [ ] **Step 7: 提交有限能力实现**

```powershell
git add control_plane/app/modules/authorization/domain/models.py control_plane/app/modules/authorization/domain/__init__.py control_plane/app/modules/authorization/__init__.py control_plane/app/modules/authorization/application/decisions.py tests/authorization/test_reserved_capabilities.py
git commit -m "feat(authorization): grant finite V0.2 super admin capabilities" -m "Super Admin 需要覆盖当前 V0.2 治理闭环；显式 Platform 集合复用于投影与服务端判定，避免通配能力和重复 Grant。"
```

### Task 2: 迁移 V0.2 route registry 并验证真实导航

**Files:**

- Create: `migrations/authorization/0005_authorization_v02_routes.py`
- Modify: `tests/authorization/test_migration.py`
- Modify: `tests/authorization/test_api.py`

**Interfaces:**

- Consumes: Task 1 的 `V02_SUPER_ADMIN_PLATFORM_CAPABILITIES` 与 Principal 投影。
- Produces: 六条 route registry；`/navigation` 对 Super Admin 返回八个已激活 routeKey。

- [ ] **Step 1: 写 migration 和 API 的失败测试**

在 `test_migration.py` 增加 `test_authorization_0005_installs_exact_v02_routes_and_preserves_extensions`：先 downgrade 到 `0004_authorization_pending_set`，插入一个 `custom.extension`，upgrade heads，查询 `route_key/capability/scope_type/sort/meta` 并断言六条新增值精确且扩展仍在。随后修改 `audit` 的 `meta.name`，downgrade 到 0004，断言其余五条精确记录删除、被人工修改的 `audit` 和未知扩展保留；finally 清理测试行并恢复 heads。

再增加 `test_authorization_0005_rejects_conflicting_managed_route`：在 0004 状态预插入 capability 错误的 `audit`，`command.upgrade(config, "heads")` 必须抛 `ProgrammingError`；清理后恢复 heads。

在 `test_api.py` 增加 `test_super_admin_me_and_navigation_are_exact_v02_projection`，沿现有 `_initialize_account` 创建 Full Session，把账号提升为 Super Admin 并创建 principal version，然后断言：

```python
expected_route_keys = [
    "home",
    "admin",
    "audit",
    "admin.workspaces",
    "admin.organization",
    "admin.users",
    "admin.grants",
    "admin.policies",
]
assert [item["routeKey"] for item in navigation.json()] == expected_route_keys
assert {item["capability"] for item in me.json()["capabilities"]} == set(
    V02_SUPER_ADMIN_PLATFORM_CAPABILITIES
)
assert not {
    "tasks",
    "workspaces",
    "admin.skills",
    "admin.models",
    "admin.roles",
    "admin.menus",
} & set(expected_route_keys)
```

- [ ] **Step 2: 运行测试观察 RED**

```powershell
uv run pytest tests/authorization/test_migration.py tests/authorization/test_api.py::test_super_admin_me_and_navigation_are_exact_v02_projection -v
```

Expected: FAIL，因为 0005 migration 不存在，真实目录仍只有 `home/admin`。

- [ ] **Step 3: 创建 `0005_authorization_v02_routes.py`**

文件头、revision 与受管值必须为：

```python
"""activate the completed V0.2 navigation catalog."""

from alembic import op

revision = "0005_authorization_v02_routes"
down_revision = "0004_authorization_pending_set"
branch_labels = None
depends_on = None

_VALUES = """
    ('audit', 'audit.read', 'PLATFORM', 7,
        jsonb_build_object('name', '审计看板', 'order', 7)),
    ('admin.workspaces', 'platform.workspace.manage', 'PLATFORM', 8,
        jsonb_build_object('name', '工作区管理', 'order', 8)),
    ('admin.organization', 'platform.organization.manage', 'PLATFORM', 9,
        jsonb_build_object('name', '组织管理', 'order', 9)),
    ('admin.users', 'identity.account.manage', 'PLATFORM', 13,
        jsonb_build_object('name', '用户管理', 'order', 13)),
    ('admin.grants', 'platform.authorization.manage', 'PLATFORM', 14,
        jsonb_build_object('name', 'Grant 管理', 'order', 14)),
    ('admin.policies', 'platform.configuration.manage', 'PLATFORM', 15,
        jsonb_build_object('name', 'Policy 发布', 'order', 15))
"""
```

`upgrade()` 复用 `_VALUES` 做冲突检查与缺失插入：

```python
def upgrade() -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE conflict_key TEXT;
        BEGIN
            SELECT actual.route_key INTO conflict_key
            FROM "authorization".route_registry AS actual
            JOIN (VALUES {_VALUES}) AS desired
                (route_key, capability, scope_type, sort, meta)
              ON desired.route_key = actual.route_key
            WHERE (actual.capability, actual.scope_type, actual.sort, actual.meta)
                IS DISTINCT FROM
                (desired.capability, desired.scope_type, desired.sort, desired.meta)
            LIMIT 1;
            IF conflict_key IS NOT NULL THEN
                RAISE EXCEPTION 'conflicting managed route: %', conflict_key;
            END IF;
        END
        $migration$
        """
    )
    op.execute(
        f"""
        INSERT INTO "authorization".route_registry
            (route_key, capability, scope_type, sort, meta)
        VALUES {_VALUES}
        ON CONFLICT (route_key) DO NOTHING
        """
    )
```

`downgrade()` 必须同时匹配全部字段；被环境人工改写的行保留，不删除 `home/admin` 或未知扩展：

```python
def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM "authorization".route_registry AS actual
        USING (VALUES {_VALUES}) AS desired
            (route_key, capability, scope_type, sort, meta)
        WHERE actual.route_key = desired.route_key
          AND (actual.capability, actual.scope_type, actual.sort, actual.meta)
              IS NOT DISTINCT FROM
              (desired.capability, desired.scope_type, desired.sort, desired.meta)
        """
    )
```

- [ ] **Step 4: 运行 migration/API GREEN**

```powershell
uv run pytest tests/authorization/test_migration.py tests/authorization/test_api.py::test_super_admin_me_and_navigation_are_exact_v02_projection tests/authorization/test_api.py::test_real_me_navigation_and_grant_lifecycle_are_protected_and_compatible -v
uv run ruff format --check migrations/authorization/0005_authorization_v02_routes.py tests/authorization/test_migration.py tests/authorization/test_api.py
uv run ruff check migrations/authorization/0005_authorization_v02_routes.py tests/authorization/test_migration.py tests/authorization/test_api.py
```

Expected: PASS；普通账号已有 Grant 时仍只获得对应 route；Super Admin 获得八个 routeKey。

- [ ] **Step 5: 提交导航 migration**

```powershell
git add migrations/authorization/0005_authorization_v02_routes.py tests/authorization/test_migration.py tests/authorization/test_api.py
git commit -m "feat(authorization): activate V0.2 navigation routes" -m "真实 route registry 只包含基础入口，导致删除前端 Mock 后治理页面消失；确定迁移补齐已验收路由并对冲突数据失败关闭。"
```

### Task 3: 提升 0.2.1 并导出唯一 OpenAPI Artifact

**Files:**

- Modify: `control_plane/app/__init__.py`
- Modify: `tests/test_version.py`
- Modify: `tests/test_e2e_access_governance.py`
- Modify (generated): `openapi.json`

**Interfaces:**

- Consumes: Task 1–2 的后端行为；现有 release workflow 的 tag/version 等值门。
- Produces: `api-v0.2.1` OpenAPI bytes，预期 SHA-256 `624712f97f8f8f3fe9d8e57422df7a62ae88cfa9c1059a316f0b188fb19a6b1a`。

- [ ] **Step 1: 先更新版本测试并观察 RED**

把 `tests/test_version.py` 与 `tests/test_e2e_access_governance.py` 的 release 断言改为 `0.2.1`，函数名同步改为 `test_release_version_is_0_2_1`。

Run:

```powershell
uv run pytest tests/test_version.py tests/test_e2e_access_governance.py::test_release_version_is_0_2_1 -v
```

Expected: FAIL，当前应用仍报告 `0.2.0`。

- [ ] **Step 2: 修改单源版本并重导出**

把 `control_plane/app/__init__.py` 改为：

```python
__version__ = "0.2.1"
```

然后运行：

```powershell
uv run python scripts/export_openapi.py
uv run python scripts/export_openapi.py --check
```

Expected: 输出 `version=0.2.1`，check PASS。

- [ ] **Step 3: 证明 schema 未发生意外变化并核对 digest**

Run:

```powershell
git diff -- openapi.json
(Get-FileHash openapi.json -Algorithm SHA256).Hash.ToLowerInvariant()
```

Expected: OpenAPI diff 只有 `info.version: 0.2.0 → 0.2.1`；LF 文件 digest 为 `624712f97f8f8f3fe9d8e57422df7a62ae88cfa9c1059a316f0b188fb19a6b1a`。若出现任何 path/schema/status/security 差异或 digest 不同，停止发布并解释契约变化，不能更新本计划中的前端锁值掩盖漂移。

- [ ] **Step 4: 运行版本 GREEN 并提交**

```powershell
uv run pytest tests/test_version.py tests/test_e2e_access_governance.py::test_release_version_is_0_2_1 -v
git add control_plane/app/__init__.py tests/test_version.py tests/test_e2e_access_governance.py openapi.json
git commit -m "chore(release): prepare api-v0.2.1" -m "导航与有限 Super Admin 行为需要独立可部署补丁；版本化 Artifact 让前端能够锁定同一后端提交与摘要。"
```

### Task 4: 固化首次 Super Admin Bootstrap 运维输入

**Files:**

- Modify: `docs/runbook-break-glass.md`
- Verify: `tests/tools/test_recovery.py`

**Interfaces:**

- Consumes: 既有 `control_plane.tools.bootstrap_admin` CLI。
- Produces: 目标环境首次 Bootstrap 使用员工号 `00000000` 的无秘密 runbook；不改变身份或授权代码。

- [ ] **Step 1: 修订 Bootstrap runbook**

把首次 Bootstrap 示例改为：

```powershell
uv run python -m control_plane.tools.bootstrap_admin --employee-no 00000000 --display-name 平台超级管理员
```

紧邻命令写明三条约束：

```text
仅在目标环境尚无任何 Super Admin 时运行；已有首个 Super Admin 时不得重建或覆盖账号。
临时密码只允许在受控交互终端一次展示，不重定向到文件、不复制进工单、聊天、日志或 Git。
执行者随后完成正式密码与 TOTP 初始化；应用授权始终依据 is_super_admin，不依据员工号。
```

不得在 runbook 增加固定密码。

- [ ] **Step 2: 运行 CLI 合同回归和文档检查**

```powershell
uv run pytest tests/tools/test_recovery.py -k "bootstrap and not concurrent" -v
git diff --check
```

Expected: bootstrap CLI 相关测试 PASS；runbook diff 只包含员工号示例与安全约束，不包含凭据。

- [ ] **Step 3: 提交 runbook**

```powershell
git add docs/runbook-break-glass.md
git commit -m "docs(runbook): set initial super admin employee number" -m "目标环境以 00000000 作为首次 Bootstrap 输入；runbook 同时固定一次性凭据不落盘和已有管理员不覆盖的边界。"
```

### Task 5: 完成后端门禁、合并与发布

**Files:**

- Verify only: `.github/workflows/ci.yml`
- Verify only: all Task 1–3 files

**Interfaces:**

- Consumes: Task 1–4 的四个实现/运维提交和目标架构基线提交。
- Produces: 远端 `api-v0.2.1` Release、OpenAPI Artifact 与 CI 证据，供前端计划 Task 1 使用。

- [ ] **Step 1: 跑本地修改范围门禁**

```powershell
uv run ruff format --check control_plane/app/modules/authorization migrations/authorization tests/authorization control_plane/app/__init__.py tests/test_version.py tests/test_e2e_access_governance.py
uv run ruff check control_plane/app/modules/authorization migrations/authorization tests/authorization control_plane/app/__init__.py tests/test_version.py tests/test_e2e_access_governance.py
uv run mypy control_plane/app/modules/authorization migrations/authorization/0005_authorization_v02_routes.py tests/authorization/test_reserved_capabilities.py tests/authorization/test_migration.py tests/authorization/test_api.py
uv run lint-imports
uv run python scripts/export_openapi.py --check
git diff --check
```

Expected: 全部 PASS。PostgreSQL 可用时再运行 Task 1–2 的三个定向测试文件，任何 skip 都不能作为集成通过证据。

- [ ] **Step 2: 推送功能分支并等待 CI 全量门禁**

```powershell
git push -u origin fix/v02-super-admin-navigation
```

Expected: PR/分支 CI 按 workflow 运行 locked install、Ruff format/check、Mypy、import-linter、Alembic heads、全量 pytest 和 OpenAPI check，全部成功后才合并。

- [ ] **Step 3: fast-forward/squash 合并后验证 main CI 与镜像**

确认远端 main 包含 Task 1–3 的提交，且 main `verify` 与 `publish-image` 均成功；记录 main SHA 与 `ghcr.io/unif-code/engineering-platform-backend@sha256:...`，不要把镜像 tag 当成 digest。

- [ ] **Step 4: 创建并推送 `api-v0.2.1` tag**

在本地 main 与远端 main SHA 一致后：

```powershell
git tag -a api-v0.2.1 -m "api-v0.2.1"
git push origin api-v0.2.1
```

Expected: tag CI 的 verify 与 release job 全绿；Release 同时包含 `openapi.json` 和 `openapi.json.sha256`，tag/version 等值检查通过。

- [ ] **Step 5: 验证远端 Artifact**

```powershell
Invoke-WebRequest https://github.com/unif-code/engineering-platform-backend/releases/download/api-v0.2.1/openapi.json -OutFile $env:TEMP/api-v0.2.1-openapi.json
(Get-FileHash $env:TEMP/api-v0.2.1-openapi.json -Algorithm SHA256).Hash.ToLowerInvariant()
```

Expected: digest 精确为 `624712f97f8f8f3fe9d8e57422df7a62ae88cfa9c1059a316f0b188fb19a6b1a`，`info.version` 为 `0.2.1`。若 GitHub Release 不可达或摘要不同，前端计划不得开始 Artifact 锁定。

- [ ] **Step 6: 仅在尚未 Bootstrap 的目标环境创建首个 Super Admin**

由环境所有者先确认当前环境不存在任何 Super Admin；确认后在受控交互终端运行 runbook 中的精确命令。CLI 若报告已经存在 Super Admin，立即停止，不执行 recovery、不删除账号、不修改数据库。临时密码只由执行者现场读取并立即完成正式密码与 TOTP 初始化；自动化记录只保留员工号、版本、request/correlation ID 与 Audit 结果。
