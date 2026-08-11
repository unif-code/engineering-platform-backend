# V0.2 访问治理闭环 · 后端实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现本地身份认证（员工号+密码+强制 TOTP）、PG 承载的可撤销 Session、服务端 Fail Closed 授权链、组织/Workspace/Grant 治理、PLATFORM_POLICY 基础生命周期与全量审计，收口发布 `api-v0.2.0`。

**Architecture:** 模块化单体新增 `organization/workspace/authorization/configuration` 四模块并扩展 `identity/audit`，各五层 + 独立 schema/迁移/角色；Session 与授权投影由 PostgreSQL 承载（Port 抽象）；Secret 材料按 DEV-003 经 `SecretManagerPort` 读文件。设计细节见 `docs/superpowers/specs/2026-08-10-backend-access-governance-v02-design.md`。

**Tech Stack:** Python 3.12 / uv / FastAPI / Pydantic 2 / SQLAlchemy 2 / Alembic / psycopg3 / argon2-cffi / pyotp / cryptography(AES-GCM)

## Global Constraints

- 里程碑门禁：**Task 1–2 立即可做（V0.1 骨架修缮）；Task 3 起须等 V0.1 `ACCEPTED`**。
- 每任务收尾质量门全绿：`uv run ruff format --check . && uv run ruff check . && uv run mypy . && uv run lint-imports && uv run pytest`；改路由后 `uv run python scripts/export_openapi.py` 重导出并提交 `openapi.json`。
- API 层 DTO 一律继承 `shared/api/camel.py` 的 `CamelModel`；domain 层普通 `BaseModel`；错误一律 Problem Details（`shared/api/problem.py`）。
- 变更命令自本版本起强制 `Idempotency-Key` 头（缺失→422）；并发写走 ETag/`If-Match`。
- Audit 与领域写入同一 PostgreSQL 事务；Audit 摘要绝不含密码/临时密码/TOTP Code/TOTP Secret/Cookie/Token。
- 凭据与 Secret 不进 Git/镜像/环境变量/日志；pepper 与 TOTP 密钥经 `SecretManagerPort` 读文件（DEV-003，本地开发路径由 `.env` 指定）。
- 新增依赖仅限：`argon2-cffi`、`pyotp`、`cryptography`（均入 `pyproject` 主依赖并 `uv lock`）。
- Conventional Commits 单主题；依赖/迁移/CI 变更在提交信息标注。
- 模块间只用公开 Facade（包根 `__init__`）；新模块必须同步登记 import-linter 契约（Task 1 的守护测试会强制）。

---

### Task 1:【硬前置】import-linter 契约守护测试

**Files:**
- Create: `tests/test_contract_guard.py`
- Modify: `pyproject.toml`（如守护测试暴露缺口则补契约）

**Interfaces:**
- Produces: 测试 `test_layer_contract_covers_all_modules` / `test_facade_contracts_are_symmetric`——后续任何新增模块未登记契约即红。

- [ ] **Step 1: 写失败测试**

```python
"""契约守护：import-linter 配置必须覆盖 modules/ 下全部模块。"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES_DIR = ROOT / "control_plane" / "app" / "modules"


def actual_modules() -> set[str]:
    return {p.name for p in MODULES_DIR.iterdir() if p.is_dir() and (p / "__init__.py").exists()}


def contracts() -> list[dict]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return data["tool"]["importlinter"]["contracts"]


def test_layer_contract_covers_all_modules() -> None:
    layers = next(c for c in contracts() if c["type"] == "layers")
    declared = {c.rsplit(".", 1)[-1] for c in layers["containers"]}
    assert declared == actual_modules(), (
        f"import-linter layers 契约缺少模块：{actual_modules() - declared}"
    )


def test_facade_contracts_are_symmetric() -> None:
    forbidden = [c for c in contracts() if "内部" in c.get("name", "")]
    mods = actual_modules()
    for m in sorted(mods):
        others = mods - {m}
        for other in sorted(others):
            covered = any(
                f"control_plane.app.modules.{m}" in c["source_modules"]
                and any(
                    f".{other}." in fm or fm.endswith(f".{other}") for fm in c["forbidden_modules"]
                )
                for c in forbidden
            )
            assert covered, f"缺少 {m} → {other} 深层导入的 forbidden 契约"
```

- [ ] **Step 2: 跑测试确认当前通过（identity/audit 已登记）**

Run: `uv run pytest tests/test_contract_guard.py -v` → 预期 PASS（守护基线成立；若 FAIL 先补 pyproject 契约再过）。

- [ ] **Step 3: 临时验证守护有效**：`mkdir -p control_plane/app/modules/_probe && touch control_plane/app/modules/_probe/__init__.py`，重跑测试预期 FAIL，随后删除 `_probe` 目录恢复 PASS。

- [ ] **Step 4: 质量门 + 提交**

```bash
git add tests/test_contract_guard.py pyproject.toml
git commit -m "test(arch): guard import-linter contracts against unregistered modules"
```

---

### Task 2:【硬前置】错误契约簇（Problem Details 进 OpenAPI + request id）

**Files:**
- Create: `control_plane/app/shared/api/request_id.py`、`tests/test_request_id.py`、`tests/test_error_contract.py`
- Modify: `control_plane/app/shared/api/problem.py`（补 `requestId` 扩展与共享 response 声明）、`control_plane/app/bootstrap/app.py`（挂中间件、readyz 200 schema、全局 responses）、`openapi.json`（重导出）

**Interfaces:**
- Produces: `ProblemDto`（OpenAPI component，字段 `type/title/status/detail/requestId`）；`request_id_middleware`（响应头 `X-Request-ID`，contextvar `current_request_id()`）；`PROBLEM_RESPONSES: dict[int, dict]` 供路由 `responses=` 引用（401/403/404/409/422/500）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_request_id.py
from fastapi.testclient import TestClient
from control_plane.app.bootstrap.app import create_app


def test_response_carries_request_id() -> None:
    client = TestClient(create_app())
    r = client.get("/healthz")
    assert r.headers["x-request-id"]


def test_inbound_request_id_is_propagated() -> None:
    client = TestClient(create_app())
    r = client.get("/healthz", headers={"X-Request-ID": "req-abc"})
    assert r.headers["x-request-id"] == "req-abc"


def test_problem_body_contains_request_id() -> None:
    client = TestClient(create_app())
    r = client.get("/nope", headers={"X-Request-ID": "req-x"})
    assert r.status_code == 404
    assert r.json()["requestId"] == "req-x"
```

```python
# tests/test_error_contract.py — openapi 声明检查
from control_plane.app.bootstrap.app import create_app


def test_openapi_declares_problem_responses() -> None:
    schema = create_app().openapi()
    assert "Problem" in schema["components"]["schemas"]
    ready = schema["paths"]["/readyz"]["get"]["responses"]
    assert "200" in ready and "503" in ready
    me = schema["paths"]["/api/v1/me"]["get"]["responses"]
    assert "401" in me
```

- [ ] **Step 2: 跑测试确认失败**（无中间件/声明）。

- [ ] **Step 3: 实现**：`request_id.py` 用 `contextvars.ContextVar`，中间件读取/生成（`uuid4().hex[:16]`）、写响应头；`problem.py` 的 `problem_response` 自动附 `requestId`，新增 `Problem` Pydantic 模型与 `PROBLEM_RESPONSES` 常量；`app.py` 挂中间件、readyz 声明 `response_model`（200 `{"status": "ready"}` schema + 503 Problem）、me/navigation 补 401/403 声明（此时行为不变，仅契约声明）。

- [ ] **Step 4: 测试通过 + 重导出 openapi + 前端联检**

Run: `uv run pytest -v && uv run python scripts/export_openapi.py`；在前端仓（同级目录）跑 `pnpm openapi:check` 验证判定为**非 breaking**（新增响应声明）。

- [ ] **Step 5: 提交**：`git commit -m "feat(api): declare problem responses and request id correlation"`（含 openapi.json）。

---

> **⛔ V0.2 门禁**：以下任务在 V0.1 `ACCEPTED` 前不得开工。

### Task 3: shared/security 基元 + SecretManagerPort（DEV-003）

**Files:**
- Create: `control_plane/app/shared/security/__init__.py`、`secrets.py`、`password.py`、`totp.py`、`sealed.py`、`csrf.py`；`tests/shared/test_security.py`
- Modify: `pyproject.toml`（+argon2-cffi、pyotp、cryptography）、`control_plane/app/shared/db/settings.py` 同款风格新增 `SecuritySettings(secret_material_path: str = "./.localdev-secrets")`

**Interfaces:**
- Produces:
  - `SecretMaterial.load(path) -> SecretMaterial`（属性 `password_pepper: bytes`、`totp_sealing_key: bytes`；文件缺失抛 `SecretMaterialUnavailable`——调用方 Fail Closed）
  - `hash_password(plain: str, *, pepper: bytes) -> str` / `verify_password(plain, hashed, *, pepper) -> bool`（Argon2id）
  - `validate_password_floor(plain: str, *, context: list[str]) -> list[str]`（返回违规项；15~64、大小写+特殊字符、内置弱口令表、上下文包含检查）
  - `totp_provisioning_uri(secret: str, account: str) -> str`、`verify_totp(secret: str, code: str, *, last_used_step: int | None) -> int | None`（返回本次 step，同 step 重放返回 None；±1 窗口）
  - `seal(data: bytes, key: bytes) -> bytes` / `unseal(token: bytes, key: bytes) -> bytes`（AES-GCM，随机 nonce 前置）
  - `assert_same_origin(request) -> None`（校验 `Origin`/`Sec-Fetch-Site`，跨源抛 403 Problem）

- [ ] **Step 1: 失败测试**（节选核心断言；文件含全部函数各至少一正一反用例）

```python
def test_password_roundtrip_and_pepper_dependency() -> None:
    h = hash_password("Str0ng!Passw0rd#2026", pepper=b"p1")
    assert verify_password("Str0ng!Passw0rd#2026", h, pepper=b"p1")
    assert not verify_password("Str0ng!Passw0rd#2026", h, pepper=b"p2")


def test_floor_rejects_short_weak_and_context() -> None:
    assert validate_password_floor("Sh0rt!", context=[])
    assert validate_password_floor("Password!2026aaaa", context=[])  # 弱口令表
    assert validate_password_floor("Xx!00000001Xx!zzz", context=["00000001"])


def test_totp_replay_rejected() -> None:
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    step = verify_totp(secret, code, last_used_step=None)
    assert step is not None
    assert verify_totp(secret, code, last_used_step=step) is None


def test_seal_unseal_and_missing_material_fail_closed() -> None:
    key = os.urandom(32)
    assert unseal(seal(b"s", key), key) == b"s"
    with pytest.raises(SecretMaterialUnavailable):
        SecretMaterial.load("/nonexistent/dir")
```

- [ ] **Step 2: RED** → **Step 3: 实现（依赖入 pyproject + `uv lock`）** → **Step 4: GREEN + 质量门**
- [ ] **Step 5: 提交** `feat(security): add password/totp/sealing primitives and secret material port`（标注新增依赖）。本地开发说明写入 AGENTS.md 构建命令节（生成 `./.localdev-secrets/{pepper,totp_key}` 的一行命令，目录已在 `.gitignore`）。

---

### Task 4: identity schema v2 迁移与角色

**Files:**
- Create: `migrations/identity/`（`0001_identity_base.py`：`identity` schema、`account`、`temp_credential`、`session`、`login_backoff` 表、`identity_rw` 角色最小 DML）、`tests/identity/test_migration.py`
- Modify: `alembic.ini`（多目录 version_locations 增加 identity 分支，沿 audit 模式）

**Interfaces:**
- Produces 表结构（后续任务的 SQLAlchemy 模型与此一致）：
  - `identity.account(id uuid pk, employee_no char(8) unique, display_name text, profession text null, status text check in ('PENDING_INIT','ENABLED','DISABLED','RESTRICTED'), password_hash text null, password_set_at timestamptz null, totp_sealed bytea null, totp_confirmed_at timestamptz null, totp_last_step bigint null, is_super_admin bool default false, version bigint, created_at/updated_at)`
  - `identity.temp_credential(id uuid pk, account_id fk, secret_hash text, expires_at timestamptz, consumed_at timestamptz null, issued_by uuid, created_at)`
  - `identity.session(id uuid pk, account_id fk, token_hash text unique, kind text check in ('BOOTSTRAP','FULL'), created_at, last_seen_at, expires_hint timestamptz, revoked_at timestamptz null, revoke_reason text null)`
  - `identity.login_backoff(employee_no char(8) pk, failure_count int, last_failure_at, locked_until timestamptz null)`
  - 角色 `identity_rw`：`USAGE` on schema + `SELECT/INSERT/UPDATE` on 上述表（**不授 DELETE**；session/temp_credential 撤销与消费都是 UPDATE）

- [ ] **Step 1: 失败测试**（integration 标记）：迁移后断言表存在、`identity_rw` 对 `account` 无 DELETE 权限（`has_table_privilege`）、employee_no 唯一约束生效。
- [ ] **Step 2: RED**（`uv run alembic upgrade head` 前测试跳过/失败）→ **Step 3: 写迁移** → **Step 4: `uv run alembic upgrade head && uv run pytest -m integration -v` GREEN**
- [ ] **Step 5: 提交** `feat(identity): add identity schema baseline migration`（标注迁移）。

---

### Task 5: identity domain/application——账号、临时密码、密码、TOTP、Session、退避

**Files:**
- Create: `control_plane/app/modules/identity/domain/`（`account.py`、`session.py`、`errors.py`）、`application/`（`accounts.py`、`auth.py`、`sessions.py`）、`ports/repository.py`、`adapters/sqlalchemy.py`；`tests/identity/test_accounts.py`、`test_auth_flow.py`、`test_sessions.py`
- Modify: `control_plane/app/modules/identity/__init__.py`（公开 Facade：`create_account`、`issue_temp_password`、`consume_temp_password`、`complete_password_setup`、`enroll_totp`、`confirm_totp`、`login_password_step`、`login_totp_step`、`logout`、`revoke_sessions_for`、`validate_session`、`set_account_status`）

**Interfaces:**
- Consumes: Task 3 全部基元；Task 4 表；audit Facade `record(...)`（V0.1 既有）。
- Produces（签名节选，全部走应用层服务函数 + UoW session 入参）:
  - `create_account(db, *, employee_no: str, display_name: str, actor: Principal, reason: str) -> tuple[AccountDto, str]`（返回一次性临时密码明文，仅此一次）
  - `login_password_step(db, *, employee_no, password) -> LoginChallenge`（含 `challengeToken`；触发退避与 Audit；账号 PENDING_INIT 时走临时密码分支返回 Bootstrap 挑战）
  - `login_totp_step(db, *, challenge_token, code) -> IssuedSession`（校验 TOTP 防重放、Session 上限逐出最旧、写 Audit）
  - `validate_session(db, *, raw_token) -> Principal | None`（哈希查表 + 空闲期校验 + 续期 last_seen；空闲策略值经 Task 11 的 Effective Policy Port，落地前用常量默认 60min）
  - 全部撤销类操作 = UPDATE revoked_at + 授权版本提升回调 `on_auth_change(account_id)`（Task 9 注入，此时为 no-op hook）

- [ ] **Step 1: 失败测试**（核心行为，节选）

```python
def test_temp_password_atomic_consume(db) -> None:
    _, temp = create_account(db, employee_no="00000001", display_name="A", actor=SYSTEM, reason="t")
    assert consume_temp_password(db, employee_no="00000001", temp_password=temp) is not None
    assert consume_temp_password(db, employee_no="00000001", temp_password=temp) is None  # 二次失败


def test_session_cap_evicts_oldest(db) -> None: ...  # 第 4 个登录使最旧 revoked
def test_idle_expiry(db, freezer) -> None: ...  # 61min 无活动 → validate 返回 None
def test_backoff_after_five_failures(db) -> None: ...  # 第 6 次返回带 retryAfter 的错误
def test_disable_revokes_sessions(db) -> None: ...
def test_audit_written_in_same_txn(db) -> None: ...  # 制造失败回滚 → 无 audit 行
```

- [ ] **Step 2: RED** → **Step 3: 实现（domain 纯逻辑 + application 组事务 + adapter SQL）** → **Step 4: GREEN + 质量门**
- [ ] **Step 5: 提交** `feat(identity): implement account, credential, totp and session lifecycle`。

---

### Task 6: 认证 API（login/totp/logout/bootstrap）与 Cookie

**Files:**
- Create: `control_plane/app/modules/identity/api/auth_routes.py`、`api/auth_dto.py`；`tests/identity/test_auth_api.py`
- Modify: `bootstrap/app.py`（挂路由）、`shared/api/`（无改动预期）、`openapi.json`（重导出）

**Interfaces:**
- Produces 路由（operationId）：`auth_login`、`auth_totp`、`auth_logout`、`auth_bootstrap_password`、`auth_bootstrap_totp_enroll`、`auth_bootstrap_totp_confirm`。Cookie 名 `ep_session`，`Secure+HttpOnly+SameSite=Lax`，值为随机 256bit token（DB 存哈希）。写请求依赖 `assert_same_origin` + `Idempotency-Key` 校验 dependency `require_idempotency_key`（放 `shared/api/idempotency.py`，本版本仅强制存在性与格式）。

- [ ] **Step 1: 失败测试**：登录两步成功置 Cookie；错误路径 401 Problem 带 `requestId`；退避 429 带 `Retry-After`；bootstrap 全流程（临时密码→设密码→enroll 返回 `otpauth://` URI→confirm 后 FULL 登录可用）；logout 后原 Cookie 失效。
- [ ] **Step 2: RED** → **Step 3: 实现（DTO CamelModel；错误全 Problem）** → **Step 4: GREEN + `export_openapi.py` + 质量门**
- [ ] **Step 5: 提交** `feat(identity): expose authentication and bootstrap endpoints`。

---

### Task 7: organization 模块

**Files:**
- Create: `migrations/organization/0001_org_base.py`（`organization.org_edge(account_id pk/fk, superior_id fk null, kind text check in ('MANAGER','LEADER','MEMBER'))` + `organization_rw` 角色）、模块五层（domain `structure.py` 校验、application `commands.py`/`queries.py`、api `routes.py`+`dto.py`、ports/adapters）、`tests/organization/`
- Modify: `pyproject.toml`（登记契约——Task 1 守护会强制）、`alembic.ini`、`bootstrap/app.py`

**Interfaces:**
- Consumes: identity Facade（账号状态校验）。
- Produces Facade：`get_tree(db) -> OrgTreeDto`、`set_superior(db, *, account_id, superior_id, actor, reason) -> None`（校验无环/层级/状态，成功后调用 `on_membership_change([affected...])` 回调——Task 8 注入投影重算，Task 9 注入版本提升）；查询 `direct_reports(db, leader_id) -> list[AccountRef]` 供 workspace 投影消费。
- 路由：`org_tree`（GET `/api/v1/admin/organization/tree`）、`org_set_superior`（PUT `/api/v1/admin/accounts/{accountId}/superior`）。

- [ ] **Step 1: 失败测试**：Leader 必须挂经理、员工必须挂 Leader、拒绝环/Leader 管 Leader、变更写 Audit、树查询形状。
- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN + openapi 重导出 + 质量门**
- [ ] **Step 5: 提交** `feat(organization): add org structure module`（标注迁移+契约登记）。

---

### Task 8: workspace 模块与 FormalMembers 投影

**Files:**
- Create: `migrations/workspace/0001_workspace_base.py`（`workspace.workspace(id, name, owner_id, archived_at null, version)`、`workspace.leader(workspace_id, account_id, invited_by, pk(workspace_id,account_id))`、`workspace.members_projection(workspace_id, account_id, source text, computed_at, pk(workspace_id,account_id))` + `workspace_rw`）、模块五层、`tests/workspace/`
- Modify: 契约登记、`alembic.ini`、`bootstrap/app.py`

**Interfaces:**
- Consumes: organization Facade `direct_reports`；identity 状态。
- Produces Facade：`create_workspace(db,*,name,owner_id,actor)`、`invite_leader/remove_leader/transfer_owner`（Owner 门禁 + Capability 由 API 层 Task 9 依赖施加）、`recompute_members(db, workspace_id) -> int`、`is_formal_member(db,*,workspace_id,account_id) -> bool`（授权链消费）；`on_membership_change` 的实现（受影响 Workspace 重算）。
- 路由：`workspace_list/create/invite_leader/remove_leader/transfer_owner/members`。

- [ ] **Step 1: 失败测试**：投影 = Owner∪受邀 Leader∪其直属（去重）；未受邀 Leader 的员工不在内；Owner 转让后旧 Owner 若非 Leader 移出；**重建等价性**（全量 recompute == 事件驱动结果）；Owner 不可直接移除（先转让）。
- [ ] **Step 2: RED** → **Step 3: 实现（投影同步重算，无消息链）** → **Step 4: GREEN + 导出 + 质量门** → **Step 5: 提交** `feat(workspace): add workspace governance and members projection`。

---

### Task 9: authorization 模块——Grant、判定链、授权版本、me/navigation

**Files:**
- Create: `migrations/authorization/0001_authz_base.py`（`authorization.grant(id, principal_id, capability, scope_type, scope_id null, source, valid_from/valid_to null, status, version)`、`authorization.principal_version(account_id pk, version bigint)`、`authorization.route_registry(route_key pk, capability, scope_type, sort, meta jsonb)` 种子数据 + `authorization_rw`）、模块五层、`shared/api/authz.py`（`require_capability` dependency）、`tests/authorization/`
- Modify: identity 的 me/navigation stub 路由**迁移至** `authorization/api/routes.py`（operationId 保持 `identity_me`/`identity_navigation` 不变，路径不变——非 breaking）；`bootstrap/app.py`；契约登记

**Interfaces:**
- Consumes: `validate_session`（identity）、`is_formal_member`（workspace）。
- Produces:
  - `require_capability(capability: str, scope: ScopeResolver = PLATFORM) -> Depends`（链：Cookie→Session→账号状态→Grant(capability,scope)→Membership(适用时)→Deny 时 403 Problem + Audit；任何投影读取失败→503 Fail Closed）
  - Facade：`grant(db,*,principal_id,capability,scope,actor,reason)`、`revoke(db,*,grant_id,actor,reason)`、`bump_version(db, account_id)`（`on_auth_change` 的实现，注入 identity/org/workspace 回调）、`current_principal(db, raw_token) -> Principal`（含有效 Capability 集）
  - 路由：`grants_list/create/revoke`；`identity_me`（真实 Principal+组织摘要+Workspace+Capability）、`identity_navigation`（route_registry ∩ 有效 Capability，含 routeKey/sort/meta）
- **me/navigation DTO 字段保持 V0.1 形状的超集**（新增字段可选），保证非 breaking。

- [ ] **Step 1: 失败测试**：无 Grant→403 且有 Audit；revoke 后**下一请求**即 403（版本提升）；跨 Workspace scope 拒绝；投影表被 drop 模拟读失败→503 不放行；navigation 只含有 Capability 的 routeKey；me 返回有效 Capability 集合。
- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN + 导出 + 前端 `pnpm openapi:check` 非 breaking 确认 + 质量门** → **Step 5: 提交** `feat(authorization): add grants, decision chain and real me/navigation`。

---

### Task 10: Super Admin 生命周期 + break-glass CLI

**Files:**
- Create: `control_plane/tools/__init__.py`、`control_plane/tools/bootstrap_admin.py`、`control_plane/tools/recovery.py`；`control_plane/app/modules/identity/application/super_admin.py`；api 路由 `super_admin_routes.py`；`tests/identity/test_super_admin.py`、`tests/tools/test_recovery.py`；`docs/runbook-break-glass.md`（交 ③ 的 runbook 草案：Job 模板要点 + 双证据要求）
- Modify: `bootstrap/app.py`、`openapi.json`

**Interfaces:**
- Produces:
  - CLI `python -m control_plane.tools.bootstrap_admin --employee-no 00000001 --display-name 张三`：环境仅一次（存在任何 `is_super_admin` 即拒绝）；产出临时密码到 stdout（一次展示），写 Audit（actor=SYSTEM_BOOTSTRAP）。
  - API：`super_admin_list/add/remove`——add/remove 要求当前 Super Admin + 全新 TOTP code 参数 + reason；remove 拒绝移除最后一个；变化即 `bump_version` + 撤销目标既有 Session。
  - CLI `python -m control_plane.tools.recovery --employee-no ...`：仅当该账号为 Super Admin 且当前不可用（DISABLED 或凭据丢失场景由运维判定）时重签临时密码 + 撤销其全部 Session + 双写证据（audit 表 + stderr 结构化日志）；退出码非 0 即未执行。
- 保留能力常量：`PLATFORM_CONFIGURATION_MANAGE = "platform.configuration.manage"`、`PLATFORM_SUPER_ADMIN_MANAGE = "platform.super_admin.manage"`——判定链对这两个 capability 仅认 `is_super_admin` 事实，不认普通 Grant。

- [ ] **Step 1: 失败测试**：bootstrap 二次执行拒绝；add 需 TOTP 且被加者必须已完成初始化；remove 最后一个拒绝；普通 Grant 授予保留能力不生效；recovery 后旧 Session 全失效且新临时密码可走 bootstrap 流。
- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN + 导出 + 质量门** → **Step 5: 提交** `feat(identity): super admin lifecycle and break-glass recovery cli`。

---

### Task 11: configuration 模块①——Schema 注册、Draft、Validation、种子版本

**Files:**
- Create: `migrations/configuration/0001_config_base.py`（`configuration.policy_key(key pk, namespace, value_type, unit null, default_value jsonb, min/max/enum jsonb null, effect_semantics)`、`configuration.draft(id, namespace, scope, content jsonb, base_version bigint, owner_id, revision int, status text check in('DRAFT','ARCHIVED'), stale bool, last_meaningful_activity_at, archived_at null)`、`configuration.version(namespace, scope, version bigint, snapshot jsonb, changeset jsonb, published_by, reason, published_at, pk(namespace,scope,version))`、`configuration.active_pointer(namespace, scope, version, pk(namespace,scope))` + `configuration_rw`；**种子数据**：identity namespace 七个 Key（临时密码 24h/密码周期 NEVER/Session 上限 3/空闲 60min/退避参数/TOTP 上限 5/Draft 归档 30d，默认值按 appendix）+ 以默认值发布 version 1（actor=SYSTEM_SEED，Audit 标注 bootstrap 来源）
- Create: 模块五层（registry/draft 命令查询）+ `tests/configuration/test_drafts.py`
- Modify: 契约登记、`alembic.ini`、`bootstrap/app.py`

**Interfaces:**
- Produces Facade：`catalog(db) -> list[PolicyKeyDto]`、`active_snapshot(db, namespace) -> PolicySnapshot`（含 version）、`create_draft/update_draft(ETag=revision)/validate_draft(db, draft_id) -> list[ValidationIssue]`；`EffectivePolicyPort`：`get_identity_policy(db) -> IdentityPolicy`（类型化取值，identity 模块改为消费它——替换 Task 5 的常量默认）。
- 路由：`policies_catalog/draft_create/draft_update/draft_validate`。

- [ ] **Step 1: 失败测试**：种子后 `active_snapshot` 返回 version 1 全默认值；Draft ETag 落后 PATCH→409；validate 拦截越界（空闲 10min < 15 下限）；identity 空闲失效改读 Effective（发布 15min 后 16min 无活动 session 失效——先用直接写 version 2 的测试辅助）。
- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN + 导出 + 质量门** → **Step 5: 提交** `feat(configuration): policy schema, drafts and effective policy port`。

---

### Task 12: configuration 模块②——Preview、Publish、Rollback、归档任务

**Files:**
- Create: `application/publish.py`、`application/preview.py`、`application/archive.py`；api 路由补 `draft_preview/draft_publish/policy_rollback/policy_versions`；`tests/configuration/test_publish.py`
- Modify: `openapi.json`

**Interfaces:**
- Consumes: Task 10 保留能力判定 + TOTP 校验（identity Facade `verify_admin_totp(db, actor, code)`——Task 10 已提供）。
- Produces:
  - `preview(db, draft_id) -> PreviewDto`（逐 Key 前后值、生效语义、影响说明文案）
  - `publish(db, *, draft_id, actor, reason, totp_code) -> PublishedVersion`：单事务内重校验（授权、Base==当前 Active、约束、TOTP）→ 写不可变 version + changeset → 原子切 active_pointer → Audit；Base 落后→409 `SOURCE_STALE` Problem；其他同 namespace Draft 标 `stale=true`。
  - `rollback(db, *, namespace, scope, to_version, actor) -> DraftDto`（从历史 snapshot 开新 Draft，不动指针）
  - `archive_stale_drafts(db, *, now) -> int`（按归档 Policy 幂等，供 CLI/定时调用：`python -m control_plane.tools.archive_drafts`）
- **Snapshot 不可变**：version 表无 UPDATE 授权（`configuration_rw` 对 version 仅 SELECT/INSERT）。

- [ ] **Step 1: 失败测试**：publish 后 active 切换且旧 snapshot 原样可查；并发两 publish 仅一成功另一 409；错误 TOTP 拒绝且写安全 Audit；rollback 产出的 Draft 发布后版本号更高；30 天无活动 Draft 被归档、有活动的跳过；`configuration_rw` 无法 UPDATE version（权限测试）。
- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN + 导出 + 质量门** → **Step 5: 提交** `feat(configuration): publish, rollback and draft archival`。

---

### Task 13: 管理面 API——账号治理全量

**Files:**
- Create: `control_plane/app/modules/identity/api/admin_routes.py` + DTO；`tests/identity/test_admin_api.py`
- Modify: `bootstrap/app.py`、`openapi.json`

**Interfaces:**
- Produces 路由（全部挂 `require_capability`，capability 常量：`identity.account.manage` 等，Platform scope）：`accounts_list/create/reset_password/enable/disable/totp_reset`。reset/create 响应含一次性临时密码；totp_reset 撤销 Session + 要求重新 Enrollment + 安全 Audit；全部写路由要求 `Idempotency-Key` + Origin 校验 + reason 字段。

- [ ] **Step 1: 失败测试**：无 capability 403；创建重复员工号 409；reset 后旧密码/旧 Session 失效而 TOTP 保留；totp_reset 后登录要求重新 enroll；响应不含任何哈希/密文字段。
- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN + 导出 + 质量门** → **Step 5: 提交** `feat(identity): admin account governance endpoints`。

---

### Task 14: 审计查询 API

**Files:**
- Create: `control_plane/app/modules/audit/api/routes.py` + DTO（audit 模块现无 api 层，按五层补齐）；`tests/audit/test_query_api.py`
- Modify: `bootstrap/app.py`、`openapi.json`、契约（audit api 层已在 layers 契约内）

**Interfaces:**
- Produces：`audit_events_list`（GET `/api/v1/admin/audit-events`，`require_capability("audit.read")`；过滤 actor/targetType/targetId/from/to；cursor 分页 `{items, nextCursor}`，cursor=`(occurred_at,id)` 编码；item 含 requestId）。audit 表补 `request_id` 列（`migrations/audit/0002_request_id.py`，nullable，记录时从 `current_request_id()` 取）。

- [ ] **Step 1: 失败测试**：无 capability 403；过滤与分页正确（3 页遍历无重复无遗漏）；新写入的 audit 行带 requestId 且可按其检索。
- [ ] **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN + 导出 + 质量门** → **Step 5: 提交** `feat(audit): expose audit query api with request id correlation`（标注迁移）。

---

### Task 15: 端到端验收、构件发布 api-v0.2.0

**Files:**
- Create: `tests/test_e2e_access_governance.py`
- Modify: `control_plane/app/__init__.py`（`__version__ = "0.2.0"`）、`openapi.json`

**Interfaces:**
- Consumes: 全部前序 Facade 与路由。

- [ ] **Step 1: 写端到端测试**（integration）：CLI bootstrap Super Admin → 登录（bootstrap 流全程）→ 建普通账号 → 该账号初始化+登录 → 无 Grant 访问管理 API 403 → Super Admin 授 Grant → 成功 → revoke → 下一请求 403 → 全程 audit 行数与关键动作一一对应、requestId 贯通。
- [ ] **Step 2: 跑通全套** `uv run pytest -v` + 质量门。
- [ ] **Step 3: 版本与构件**：`__version__="0.2.0"`；`uv run python scripts/export_openapi.py`；前端仓 `pnpm openapi:check` 确认非 breaking（预期新增路径+新增声明；若判 breaking 则按规则处理，不得放宽检查）。
- [ ] **Step 4: 提交并打 tag**

```bash
git add -A && git commit -m "feat(release): close V0.2 access governance and bump api to 0.2.0"
git tag api-v0.2.0 && git push origin main --tags
```

- [ ] **Step 5: 确认 CI release job 绿、Release 附件含 openapi.json+sha256**；把新 sha256 与 `file:` 通道说明交给前端会话重新锁定；`docs/runbook-break-glass.md` 与 DEV-003 Secret 生成步骤回执给 gitops 会话。

---

## Self-Review 完成项

- spec 覆盖检查：认证链(T5/6)、组织(T7)、Workspace(T8)、授权链+me/navigation(T9)、Super Admin+break-glass(T10)、Policy 基础生命周期(T11/12)、管理面(T13)、审计查询(T14)、验收与发布(T15)、硬前置(T1/2)——spec 全节有归属。
- 命名一致性：Facade 函数名、operationId、角色名、表名跨任务已对齐；me/navigation operationId 保持 V0.1 不变。
- 无占位符；每任务含可执行测试片段与明确 RED/GREEN 步骤。
