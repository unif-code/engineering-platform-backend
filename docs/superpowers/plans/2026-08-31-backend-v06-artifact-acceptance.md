# V0.6 Evidence, Acceptance & Formal Delivery · 后端实施计划

> 执行规则：严格 Red → Green → Refactor。每个 Task 先运行最小失败测试，确认失败由缺失行为造成；再写最小
> 生产代码。不得读取/复制/合并 V0.5 工作树，不更新 `docs/superpowers/progress/current.md`。

**Base:** `9b71b82602bb4231f6ccc2622c0f49a91287a366`

**Architecture:** Requirement 拥有 Snapshot/Selection/Acceptance/Review/状态；Source Control 拥有外部验证、
Evidence、Formal MR/Effect/Reconciliation。只经 package-root Facade、Port 与 Outbox/Inbox 协作。

## Task 1: 设计与 Contract Guard

**Produces**

- 本设计与计划。
- import-linter 对新增跨模块 seam 的约束。
- V0.6 默认应用 activation 与生产依赖装配测试。

**TDD**

1. 添加结构/公开 Facade/默认激活路径测试并运行 RED。
2. 增加最小 package/contract 定义，运行 focused tests GREEN。

## Task 2: Requirement Snapshot / Selection / Delivery Gate schema 与 domain

**Files**

- `migrations/requirement/0006_requirement_evidence_acceptance.py`
- `control_plane/app/modules/requirement/domain/{evidence,acceptance}.py`
- `control_plane/app/modules/requirement/ports/{evidence,acceptance}.py`
- 对应 migration/domain tests。

**TDD slices**

1. canonical acceptance criteria / delivery snapshot hash。
2. exact WorkItem set coverage 与 duplicate/extra/missing 拒绝。
3. Selection CAS 与 Requirement Version 前后绑定。
4. Acceptance/Review Decision snapshot、失效和完成状态转换。
5. Migration constraints、runtime grants、append-only 与安全 downgrade。

## Task 3: Source Control 外部验证与 IntegrationBaselineEvidence

**Files**

- `migrations/source_control/0007_source_control_evidence.py`
- `migrations/source_control/0008_source_control_formal_delivery.py`
- `control_plane/app/modules/source_control/domain/evidence.py`
- `control_plane/app/modules/source_control/application/evidence.py`
- `control_plane/app/modules/source_control/ports/evidence*.py`
- `control_plane/app/modules/source_control/adapters/*evidence*.py`
- `tests/source_control/test_{evidence_domain,evidence_repository,evidence_e2e}.py`

**TDD slices**

1. 外部 URL 规范化、query/fragment 拒绝持久化、精确 target Commit、Artifact ref 规范化。
2. append-only/idempotent external validation record。
3. Requirement snapshot envelope Inbox 去重与 snapshot conflict。
4. 从已证明 Integration MR Observation 生成 exact-set immutable Evidence/hash。
5. Evidence currentness reader 与变化后新 Evidence。

## Task 4: Requirement Selection 与 Acceptance application/API

**Files**

- `control_plane/app/modules/requirement/application/{evidence,acceptance}.py`
- `control_plane/app/modules/requirement/adapters/{evidence,acceptance}.py`
- `control_plane/app/modules/requirement/api/{dto,routes}.py`
- package-root Facade 与 bootstrap dependencies。
- 显式 V0.6 router capability guards；按 2026-09-04 activation 补充方案，默认 navigation 已启用 V0.6，`0009_auth_v06_routes` 追加能力注册，不自动创建 Grant。

**TDD slices**

1. 请求 Evidence 的 Snapshot + Outbox 原子事务与 replay/conflict。
2. Source Control Evidence Reader 只经包根 Facade。
3. Selection exact match、stale conflict、Requirement Version bump、Audit/Outbox。
4. Acceptance assignment + eligibility snapshot + decision + replay/conflict。
5. 详情投影只返回 current 值并保留历史查询能力。
6. HTTP capability、camelCase、If-Match/ETag、Problem Details。

## Task 5: Formal MR / Review / Merge

**Files**

- Source Control effect/binding/ports/application/reconciliation 扩展。
- Requirement formal delivery commands、Gate callbacks 与 API。
- worker composition 与相应 unit/integration tests。

**TDD slices**

1. Formal Binding 与 Integration Binding 可并存，target 固定 `main`。
2. Acceptance evidence 不当前时拒绝请求 Formal MR。
3. CREATE_FORMAL_MR effect：同 subject replay、exact head、read-after-write unknown。
4. Review assignment snapshot → Requirement Formal Review Gate callback。
5. Review decision绑定 head；head change 使 Review/Acceptance 失效。
6. MERGE_FORMAL_MR effect：批准证据、exact head、squash/delete source、reconciliation。
7. callback 只在全部 required WorkItem merged 且 Acceptance current 时完成 Requirement。

## Task 6: 默认 Activation、PG/HTTP E2E 与 OpenAPI

1. 用默认 router 与唯一公共 worker 完成真实 PostgreSQL + HTTP 人工闭环 E2E。
2. V0.5 基线已集成；`create_app()` 默认暴露 V0.6 路径并装配真实 owner Policy、qualification 与 callbacks。
3. 删除独立 V0.6 worker 入口；统一 relay/process/reconcile 的最小总 limit 为 4/5/3，不保留 shim。
4. 导出并校验 `openapi.json`。

## Task 7: Verification 与 review

2026-09-04 执行修订：本地实现阶段只跑最小新增/改动检查；以下批量回归在版本集成后统一执行，PR CI 保持启用。

依次执行：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
uv run alembic upgrade heads
uv run pytest -v
uv run python scripts/export_openapi.py --check
```

随后按固定 base/head 启动独立 reviewer；修复所有真实 findings 后重新运行受影响测试与完整门禁。

## Task 8: Local commit / review checkpoint

- 确认工作树只包含 V0.6 文件且未改 progress。
- 以 Conventional Commit 提交当前分支。
- 当前执行授权以最新用户“仅本地”及继续 V0.6 本地整合指令为准：执行子任务仅做范围内实现、最小验证、本地提交，由主任务负责独立 review、将 V0.6 整合到本地 main，以及安全清理已合并的 V0.6 分支/工作树。禁止推送、创建 PR、触发远端 CI、打 tag 或部署；此前远端交付/main push CI 步骤为历史计划，须另获明确授权。执行子任务不操作其他工作树、不合并或清理。本地批量回归留待版本整合后，现有必需 CI 配置不关闭或绕过，focused 结果不代表发布验收。
