# engineering-platform-backend

内部研发平台的 Control Plane 后端：模块化单体，FastAPI + SQLAlchemy 2 + PostgreSQL 18，Python 3.12（uv 管理）。

## 仓库拓扑与架构事实源

平台由四个同级仓库组成，建议克隆到同一父目录（跨仓联调与文档引用都按同级相对路径约定，如前端 OpenAPI 锁定走 `file:../engineering-platform-backend/openapi.json`）：

```
<workspace>/
├── engineering-platform-docs/     # 平台架构文档 + 基线治理（唯一事实源）
├── engineering-platform/          # 前端（Umi Max）
├── engineering-platform-backend/  # 本仓：Control Plane 后端
└── engineering-platform-gitops/   # 集群清单、部署与运维 runbook
```

平台架构的唯一事实源是 `engineering-platform-docs/architecture/`（00–12 共 13 篇 + `appendix-parameters.md`，基线号与文档 SHA-256 由 `baseline-manifest.json` 治理）。**不要把架构文档复制进本仓**——复制件脱离基线治理必然漂移，按上述拓扑就近查阅。与本仓关系最密的几篇：

- `06-platform-application-integration.md`：应用结构与集成契约（本仓分层以此为准）
- `07-data-messaging-storage.md`：数据、消息与存储（audit 追加式、StorageBinding 等）
- `08-security-audit-governance.md`：安全与审计治理
- `appendix-parameters.md`：全部参数与错误码的唯一事实源
- `deviations.md`：DEV-001/DEV-002 例外（仅 DEV 环境，V0.5 前必须关闭）

## 快速开始

```bash
uv sync                        # 安装依赖（CI 用 --locked）
docker compose up -d           # 本地一次性 PostgreSQL 18（仅开发/测试；部署走 k8s，见下）
uv run alembic upgrade head    # 执行迁移
uv run uvicorn control_plane.app.bootstrap.app:create_app --factory --reload
uv run pytest                  # 无 DB 时集成测试自动 skip（勿据此判定通过）
```

质量门与 CI 同链：`ruff format --check` / `ruff check` / `mypy` / `lint-imports` / `pytest` / `python scripts/export_openapi.py --check`，全部以 `uv run` 执行。

## 两条发布链

| 触发 | 产物 | 消费方 |
| --- | --- | --- |
| push 到 main | 容器镜像 `ghcr.io/unif-code/engineering-platform-backend:sha-<短哈希>`（digest 见 CI job summary） | gitops 仓按 **digest** 引用，部署进 k8s |
| 打 `api-vX.Y.Z` tag | `openapi.json` + SHA-256 附到 GitHub Release | 前端仓生成类型化 client（breaking 变更必须升 major） |

## 运行契约（部署侧）

- 端口 `8000`；liveness `/healthz`，readiness `/readyz`（DB 不可达返回 503 `application/problem+json`）。
- 环境变量：`DATABASE_URL`（运行时，`audit_rw` 受限角色）、`MIGRATION_DATABASE_URL`（迁移 Job，owner 角色），格式 `postgresql+psycopg://user:pass@host:5432/platform`。
- 迁移 Job 使用同一镜像执行 `alembic upgrade head`（镜像已含 `migrations/` 与 `alembic.ini`）。
- 容器以非 root（uid/gid 999）运行；镜像 Private，集群侧需 `read:packages` 的 imagePullSecret。

## 约定速查

分层结构、模块 Facade 边界、API/数据约定与提交规范见 [AGENTS.md](AGENTS.md)。凭据一律不入 Git，`.env` 仅本地。
