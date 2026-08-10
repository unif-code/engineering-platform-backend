# Repository Guidelines

## 项目结构与模块组织

本仓是内部研发平台的 Python Control Plane（模块化单体），结构以
`engineering-platform-docs` 仓 `architecture/06-platform-application-integration.md` 为准
（架构文档已独立成仓，与本仓同级克隆）：
`control_plane/app/bootstrap/` 负责装配，`control_plane/app/shared/{api,db}/`
是无业务基建（`observability`：结构化日志、request-id，随 V0.2 落地），
领域模块在 `control_plane/app/modules/<module>/`，模块内固定五层
`api/ application/ domain/ ports/ adapters/`。模块间只允许使用对方包根的公开 Facade；
边界由 import-linter 契约强制（`uv run lint-imports`），禁止绕过。迁移在 `migrations/`
按模块分目录；测试在 `tests/`（集成测试标记 `integration`）。

## 构建、测试与开发命令

- `uv sync`：安装依赖（CI 使用 `--locked`）。
- `docker compose up -d`：启动本地 PostgreSQL 18。
- `uv run alembic upgrade head`：执行迁移。
- `uv run uvicorn control_plane.app.bootstrap.app:create_app --factory --reload`：本地起服务。
- `uv run pytest`：全部测试；无 DB 时集成测试自动 skip。
- `uv run ruff format . && uv run ruff check . && uv run mypy . && uv run lint-imports`：质量门。
- `uv run python scripts/export_openapi.py`：更新 `openapi.json`（`--check` 校验一致性，改路由后必须重导出并提交）。

## API 与数据约定

JSON 一律 camelCase（DTO 继承 `shared/api/camel.py` 的 `CamelModel`）；ID 一律 string；
前缀 `/api/v1`；错误统一 `application/problem+json`（RFC 9457），无 `{code,data,message}`
信封；分页 cursor 型 `{items, nextCursor}`、写并发 `If-Match`/ETag、变更命令
`Idempotency-Key` 自 V0.2 首个真实接口起强制。`info.version` 单源于
`control_plane/app/__init__.py`。审计表追加式由 `audit_rw` 权限保证，绝不授予
UPDATE/DELETE；Alembic 用 owner 账号执行 DDL，应用运行时只用受限角色。

## OpenAPI Artifact 发布

`openapi.json` 是入库的唯一导出，CI 校验与代码一致。正式发布打 `api-vX.Y.Z` tag，
CI 将构件与 SHA-256 附到 GitHub Release；breaking 变更必须升 major（前端仓
`openapi:check` 以 git 基线强制）。

## 容器镜像发布

main push 触发 CI `publish-image`：构建 `linux/amd64` 镜像推
`ghcr.io/unif-code/engineering-platform-backend:sha-<short-sha>`，digest 写入 job
summary，gitops 仓按 digest 引用部署。镜像同时承载 API 与迁移 Job（含
`migrations/` 与 `alembic.ini`，Job 执行 `alembic upgrade head`），非 root 运行。
本地验证：`docker build -t backend-dev . && docker run --rm -p 8000:8000 backend-dev`。

## 提交与 Pull Request 规范

线性历史、Conventional Commits（如 `feat(identity): ...`），每次提交单一主题。
依赖、迁移或 CI 变更必须在提交信息中明确标注。禁止提交凭据；`.env` 仅本地。
