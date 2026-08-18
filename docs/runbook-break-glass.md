# Super Admin bootstrap 与 break-glass 恢复 runbook

本文只描述带外运维流程。平台 Web 和普通平台 API 不提供恢复入口；禁止用直接 SQL、临时普通
Grant 或修改 authorization projection 替代本流程。

## 安全边界

- bootstrap 只用于全新环境，并且仅在数据库中不存在任何 `is_super_admin=true` 账号时执行一次。
- recovery 只用于没有任何 Super Admin 能正常认证的事故。目标必须仍是 Super Admin，且为
  `DISABLED`；如果数据库状态仍为可用，只有完成凭据丢失判定并保留审批证据后才可使用
  `--credentials-lost`。该标志是对环境中所有表面可用 Super Admin 均无法认证的全局运维
  证明：每个这类账号都必须分别留存独立证据，不能只证明目标账号凭据丢失。
- recovery 的 scope 固定为 `SUPER_ADMIN_AUTHENTICATION`；reason 必须引用已批准事故，expiry
  必须带时区且不超过当前临时凭据策略允许的有效期。
- 临时密码是一次性、限时 bootstrap 材料。使用后必须重新设置正式密码并初始化 TOTP；它不能
  用作普通登录密码。
- 每次执行都必须保留两份相互独立的证据：数据库 Audit，以及 stderr 的 credential-safe JSON。
  密码只允许在 stdout 出现一次，不得进入 Audit、stderr、Job manifest、Git、工单、聊天或日志。

## 执行前审批与双人证据

执行人和复核人应分别确认并记录：

1. 事件已获批准，reason、目标员工号、固定 scope 和 expiry 一致；
2. 所有已知 Super Admin 均无法走正常认证；使用 `--credentials-lost` 时，逐一列出数据库中
   每个表面 `ENABLED` 的 Super Admin，并为每个账号保留独立的凭据丢失证据；
3. 目标账号确为 Super Admin，且 `DISABLED` 或已有独立的凭据丢失证据；
4. 使用锁定到镜像 digest 的已审批 Job/CLI，数据库连接角色和 Secret 文件来源正确；
5. 已准备不经过容器 stdout 日志的单次凭据交付通道；
6. 执行后由不同人员核对数据库 Audit 与 stderr 事件，并确认旧 Session 已失效。

不得把真实密码、TOTP Secret、pepper、sealing key 或数据库凭据写入本文件或派生的 Job YAML。

## 一次性 bootstrap

在受控终端执行：

```powershell
python -m control_plane.tools.bootstrap_admin --employee-no 00000000 --display-name 平台超级管理员
```

- 仅在目标环境尚无任何 Super Admin 时运行；已有首个 Super Admin 时不得重建或覆盖账号。
- 临时密码只允许在受控交互终端一次展示，不重定向到文件、不复制进工单、聊天、日志或 Git。
- 执行者随后完成正式密码与 TOTP 初始化；应用授权始终依据 `is_super_admin`，不依据员工号。

成功时退出码为 0，stdout 仅有一行临时密码。stderr 使用 credential-safe JSONL：在数据库提交与
stdout 凭据交付前，先可靠写入并 flush 带稳定 `commandId` 的 `ATTEMPT`；提交后再写 `SUCCESS`。
`ATTEMPT` 写入或 flush 不可用时命令 fail closed、回滚且不得输出凭据。事实变更与 actor 为 `SYSTEM_BOOTSTRAP` 的 Audit 在同一
Identity owner 事务提交；提交后通过持久 authorization convergence 提升版本并撤销不再可信的
授权缓存。数据库中已有任意 Super Admin 时退出码为 3，且不会创建账号、临时凭据或 Audit。

## break-glass recovery

使用带时区的短期 expiry；以下尖括号均为运行时参数占位符，不是凭据：

```text
python -m control_plane.tools.recovery --employee-no <employee-no> --reason <approved-incident-id> --scope SUPER_ADMIN_AUTHENTICATION --expires-at <RFC3339-expiry>
```

只有凭据丢失证据成立、但账号状态仍不是 `DISABLED` 时，才在同一命令末尾增加：

```text
--credentials-lost
```

若数据库中有多个表面可用的 Super Admin，只有双人复核确认并留存每个账号都无法认证的证据
后才能使用该标志；它不是绕过“仍有管理员可认证”前提的便利开关。

成功会原子地撤销目标全部 Session、清除旧密码/TOTP 初始化状态、签发一次性临时密码并写入
actor 为 `SYSTEM_RECOVERY` 的 Audit。任何执行前提、数据库事务或 stdout 安全交付失败都会
回滚；密码写入 stdout 后会在数据库 commit 前显式 flush，缓冲区交付失败不会留下恢复事实。
在 stdout 之前，CLI 还必须可靠写入并 flush credential-safe `ATTEMPT`；stderr sink 此时不可用同样
回滚全部事实、不得输出密码并返回 4。提交后的 `SUCCESS`/`OUTCOME_UNKNOWN` 属于终态诊断；若其
写入失败，不能把已提交或可能已提交的操作报告为非 0，执行人以已持久化的 `ATTEMPT.commandId`
查询数据库 Audit，并用完全相同参数重放解析幂等结果。
非 0 退出码表示没有执行恢复。业务前提拒绝时，回滚恢复事务后会用独立短事务追加一条与 stderr
同 `commandId` 的 `SYSTEM_RECOVERY` `DENIED` Audit；它只含固定 reason code、目标员工号与
canonical scope，不含命令中的原始 reason 或异常文本。若该拒绝 Audit 自身失败，stderr 仅报告
`DENIAL_EVIDENCE_FAILED`，exit 4，恢复事实仍未执行。若提交确认丢失，CLI 以稳定 command ID 查询持久幂等
claim：确认已提交才输出 `SUCCESS`；仍无法判定时输出 credential-safe `OUTCOME_UNKNOWN` 并以
0 退出，执行人必须用完全相同参数重放以取回同一份已密封凭据，禁止改参数再次签发。提交后的
authorization 投影暂时不可用时，持久 fence
保持 fail-closed，并由 convergence 重试收敛，不能通过重复发放凭据来“修复”。

退出码：

| 退出码 | 含义 | 数据库结果 |
| --- | --- | --- |
| 0 | 命令已提交并交付凭据，或提交结果尚待同参数重放确认 | 已原子提交，或由稳定 command ID 安全解析；绝不把可能已提交的命令报为非 0 |
| 2 | argparse 结构错误或缺少必填参数 | 未执行 |
| 3 | 参数值/业务前提不成立 | 恢复未执行；已追加 correlated `DENIED` Audit |
| 4 | 已确认回滚的事务、precommit stderr 证据、stdout 安全输出或 denial Audit 失败 | 已回滚，恢复未执行；stderr 不泄露失败细节 |

## GitOps 一次性 Job 模板要点

GitOps 仓维护受保护的模板，不在本仓复制生产 YAML。模板至少满足：

- 镜像按不可变 digest 固定，使用专用、最小权限 ServiceAccount；审批合并后只生成一次性 Job；
- `restartPolicy: Never`、`backoffLimit: 0`、有限 `activeDeadlineSeconds` 和短期
  `ttlSecondsAfterFinished`，禁止并发/自动重试导致二次凭据签发；
- 只允许访问 PostgreSQL 所需的 NetworkPolicy；运行时分别使用 Identity、Audit 与
  Authorization 的受限角色，迁移账号不作为常规 CLI 运行账号；
- pepper、TOTP sealing key、idempotency key 和数据库连接信息只从已批准 Secret/CSI
  只读挂载或环境引用注入；manifest 与参数中不出现其真实值；
- 禁止 shell trace、命令回显、APM body capture 和 stdout 日志采集。CLI 的 stdout 必须接入
  经批准的一次性凭据交付通道，不能成为 Kubernetes 主容器日志；stderr 可进入受限审计日志；
- Job 参数必须显式包含 employee number、reason、固定 scope 与 expiry；不得把密码或 TOTP
  放入参数、annotation、label、ConfigMap 或 Job status；
- 模板必须让执行人能确认退出码，并在失败时阻止自动重启。禁止通过修改 Job 命令绕过 CLI
  的前提检查或直接调用 SQL。

## 执行后验证

1. 已进入受保护执行的 stderr 通常应有两条同 `commandId` 的结构化 JSONL 事件：提交前已 flush 的
   `ATTEMPT` 与提交后的 `SUCCESS` 或 `OUTCOME_UNKNOWN`；若终态写入失败，至少必须保留 `ATTEMPT`，
   并按上文用数据库 Audit 与同参数重放解析。业务前提在执行前被拒绝时仍为单条 `DENIED`。事件只能
   包含事件名、结果、员工号、scope、expiry、`reasonCode`、`commandId` 等 credential-safe 字段，
   不能含临时密码、原始 reason 或异常文本。
2. Audit 中核对 `SYSTEM_BOOTSTRAP` 或 `SYSTEM_RECOVERY`、目标账号、结果、scope 和 expiry。
   denial 只核对固定 reason code，不应出现命令原始 reason；Audit correlation ID 必须与 stderr
   `commandId` 一致。两份证据分别归档。
3. 确认目标旧 Session 均已撤销，authorization convergence work 已完成且 principal version
   已提升；若仍 pending，保持受保护操作 fail-closed 并运行既有持久 reconcile，不再次恢复。
4. 通过受控通道把临时密码只交付给目标人员；完成正式密码与 TOTP 初始化后，确认临时凭据已
   消耗且不能重放。
5. 删除一次性 Job 和临时输出载体，按组织保留策略保存无凭据的审批、Audit 与 stderr 证据。
