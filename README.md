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
uv run alembic upgrade heads   # 执行全部独立模块迁移分支
uv run uvicorn control_plane.app.bootstrap.app:create_app --factory --reload
uv run pytest                  # 无 DB 时集成测试自动 skip（勿据此判定通过）
```

本地 Compose 资源使用固定名称，避免因仓库目录或 worktree 名不同而在 Docker Desktop
中生成难以辨认的资源：

| 资源 | 固定名称 |
| --- | --- |
| Compose 项目 | `engineering-platform-local` |
| PostgreSQL 容器 | `engineering-platform-local-postgres` |
| PostgreSQL 数据卷 | `engineering-platform-local-postgres-data` |
| 默认网络 | `engineering-platform-local-network` |

`docker compose down` 只停止并移除容器与网络，保留数据库卷；仅在明确要清空本地数据时
才使用 `docker compose down --volumes`。

### 本地首次 Super Admin

完成数据库迁移，并按 [AGENTS.md](AGENTS.md) 生成本地密钥材料后，在真实交互终端执行：

```powershell
.\scripts\open-local-super-admin.ps1
```

脚本会打开独立 PowerShell 窗口。`ATTEMPT` JSON 之后黑字亮黄底的下一行才是一次性临时密码；
`commandId` 只是审计编号。成功后窗口最多保留 3 分钟，按 Enter 可提前关闭。
交互模式拒绝 stdout 重定向，避免凭据误入文件或日志。正式带外交付仍使用不带
`--interactive` 的底层命令，其 stdout 契约保持为唯一一行原始临时密码。

质量门与 CI 同链：`ruff format --check` / `ruff check` / `mypy` / `lint-imports` / `pytest` / `python scripts/export_openapi.py --check`，全部以 `uv run` 执行。

## 两条发布链

### Model Gateway 连接检查 worker

Control Plane 仅受理和查询检查；独立 worker 使用 `MODEL_GATEWAY_WORKER_DATABASE_URL`
及 `model_gateway_worker_rw` 权限角色执行。候选状态仍为 DRAFT/ARCHIVED，检查不会激活部署。

两个入口读取同一非敏感清单：`MODEL_GATEWAY_CONNECTIONS_PATH` 指向受控 JSON，
`MODEL_GATEWAY_ENVIRONMENT` 必须与清单的 `environment` 一致。清单结构为
`{ "environment": "...", "connections": [...] }`；每项包含 `reference`、`version`、
`materialVersion`、`providerKind`、`region`、`workspaceId`、`allowedModelIds`、`secretRef`。
字段约束以 [ConnectionDefinition](control_plane/app/modules/model_gateway/domain/connections.py)
为准。目标只由 Workspace 与批准区域派生，不接收浏览器 URL 或任意 Header。
协议依据[百炼 compatible-mode Chat](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)。

只有 worker 配置 `MODEL_GATEWAY_SECRET_REFERENCE_ROOT` 并挂载 Provider 材料。
`secret-ref:...` 对应文件是 UTF-8 JSON，字段为 `version` 和 `value`；`version` 必须等于
清单的 `materialVersion`，`value` 是密钥。轮换必须同时发布新的非敏感版本及匹配材料，
不能原地换值却复用版本；材料不进入候选表、Identity 的密钥目录、公开 DTO 或审计正文。
Control Plane 不挂载这组文件。

```bash
uv run python -m control_plane.tools.model_gateway_worker --limit 20
```

每个检查最多进入一次发送边界；`attempted` 表示到达该边界，不证明 Provider 已执行。
固定探针使用 64 个最大 completion tokens、20 秒总 HTTP/清理时限、64 KiB 响应上限，
每连接最多一个 RUNNING。Provider 超时或 worker 丢失结果收敛 UNKNOWN，不自动重发；
新检查需管理员明确发起，先前请求可能已经执行并计费。
检查 POST 必须显式提交 `checkKind`：`BASIC_TEXT`、`STREAM_TEXT`、`STREAM_STOP`、`THINKING` 或 `SEARCH_SOURCES`。
流式请求带 `include_usage`，按 SSE 事件处理；单事件上限 16 KiB，最多 256 个 data 事件。
`STREAM_TEXT` 需要非空文本、正常 finish 和 `[DONE]`；`STREAM_STOP` 在首个合法非空文本
增量后关闭本地响应及客户端。后者只证明本地停止接收，Provider 取消与停止计费均未确认。
有限观测不包含正文；历史记录未记录的观测保持 null。新增迁移只补 BASIC_TEXT 种类，
不改写旧冻结输入、幂等回执或执行状态；旧终态不重发，旧排队请求仍受当前资格与输入检查。
API 统一要求显式种类，旧空对象请求不再受理；旧 key 配合新 body 不会变成新意图。

THINKING 只核对本次专用 reasoning_content 信号和完整答案，不评价答案正确性或推理质量。
使用固定算术提示词、开启思考与流式输出，thinking_budget 为 32；沿用 64 个总输出 token、
20 秒总时限及现有响应/事件限制。完整响应缺少非空白专用信号返回 THINKING_SIGNAL_MISSING，
仅表示本次未观察到；不会自动扩大预算或重发。只保存 reasoning 的观察标志、增量数和字节数，
不保存推理或答案正文；这些计数不是 token、费用或能力认证。

SEARCH_SOURCES 仅在北京/新加坡、且模型属于连接清单 `responsesSearchModelIds` 时执行。
该字段默认空并须为 `allowedModelIds` 子集；单改搜索准入只影响搜索输入指纹，旧四类不变。
它使用同批准主机的 `/compatible-mode/v1/responses`，固定公开输入、store=false、stream=false、
仅 web_search、tool_choice=required、reasoning.effort=none、max_output_tokens=64，显式关闭 session cache。
无 previous_response_id/conversation、其他工具或 max_tool_calls；不支持参数时失败，不 fallback。

搜索证据只来自 completed 的 web_search_call.action.sources 和完整答案，不从答案提取 URL。
本地最多接受 8 个搜索调用、每调用 8 个查询（每条最多 512 字符）、每调用 16 条来源且总计 32 条，
URL 最长 2048 字符；重复脱敏 URL 按调用去重，超限直接拒绝。引用去掉 query/fragment，仅校验
HTTPS、主机/地址类别与凭据形态，不作来源 DNS/页面获取；不证明页面内容、精确版本或逐句 citation。
queries 只保存各调用的规范化去重数量与摘要，原文不保存；未返回的查询或 Provider 搜索次数为 null。
一次 HTTP 可包含多轮 Provider 搜索；本地上限、关闭和 store=false 均不证明远端次数/费用硬上限、
停止搜索/计费或全数据零留存。Responses 未返回合格来源时记录 SEARCH_SOURCE_SIGNAL_MISSING。

检查 ETag 是含当前性的 opaque 表示标识，不能用于候选 If-Match。
清单及探针变化会实时改变查询当前性，检查自身 revision 只表示持久检查状态的版本。

实际使用仍需部署侧确定区域/Workspace、批准模型、专用材料及费用预算。
一次基础响应通过不证明完整能力、价格、配额或数据处理等级。

### Source Control worker

API 默认提供 V0.6 Artifact、Acceptance 与 Formal Delivery 路由。worker 与 Connector 共用生产运行时，
配置仍从 `SourceControlDevSettings` 读取；不得在命令行或日志中打印凭据。

```bash
uv run python -m control_plane.tools.source_control_worker relay --limit 50
uv run python -m control_plane.tools.source_control_worker process --limit 50
uv run python -m control_plane.tools.source_control_worker reconcile --limit 50
```

每次命令的 `limit` 是所有 lane 的总预算，按 lane 均分，余数按固定 lane 顺序分配；空闲预算不挤占其他 lane。
relay：binding/integration/evidence/formal（最小 4）；process：binding/integration/webhook/evidence/formal
（最小 5）；reconcile：branch/integration/formal（最小 3）。失败与释放数、稳定错误码和 Effect IDs 单独报告，
不能把命令退出 0 或空批次当成交付成功。普通 Gate 转派需显式 WORKSPACE `requirement.delivery_gate.assign` Grant，
Assignment 与 SuperAdmin 身份都不会自动授予该能力。

### 发布触发

| 触发 | 产物 | 消费方 |
| --- | --- | --- |
| push 到 main | 容器镜像 `ghcr.io/unif-code/engineering-platform-backend:sha-<短哈希>`（digest 见 CI job summary） | gitops 仓按 **digest** 引用，部署进 k8s |
| 打 `api-vX.Y.Z` tag | `openapi.json` + SHA-256 附到 GitHub Release | 前端仓生成类型化 client（breaking 变更必须升 major） |

`openapi.json` 是公开浏览器 API 契约；`sandbox-openapi.json` 是独立私有 workload
Controller 契约。两者均入库并由 CI 确定性校验，私有契约不挂载到浏览器 Control Plane，
也不改变上述 tag 发布附件。私有契约的入库与校验不代表部署、物理隔离或发布验收通过。

## 运行契约（部署侧）

- 端口 `8000`；liveness `/healthz`，readiness `/readyz`（DB 不可达返回 503 `application/problem+json`）。
- 环境变量：`DATABASE_URL`（审计运行时，`audit_rw` 受限角色）、`IDENTITY_DATABASE_URL`（身份运行时，`identity_rw` 受限角色）、`MIGRATION_DATABASE_URL`（迁移 Job，owner 角色），格式 `postgresql+psycopg://user:pass@host:5432/platform`。
- 迁移 Job 使用同一镜像执行 `alembic upgrade heads`（镜像已含 `migrations/` 与 `alembic.ini`）。
- 模型候选目录使用 `MODEL_GATEWAY_DATABASE_URL`，运行账号须继承 `model_gateway_rw`。
  迁移只创建该 NOLOGIN 权限角色；登录账号与凭据由部署侧配置。目录管理无需 Provider 凭据，
  `connectionRef` 仅保存引用，不解析连接或发起模型调用。
- 容器以非 root（uid/gid 999）运行；镜像 Private，集群侧需 `read:packages` 的 imagePullSecret。

## 约定速查

分层结构、模块 Facade 边界、API/数据约定与提交规范见 [AGENTS.md](AGENTS.md)。凭据一律不入 Git，`.env` 仅本地。
