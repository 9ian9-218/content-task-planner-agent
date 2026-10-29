# 单机运行与恢复手册

适用范围：同一 Linux 主机、同一状态数据库、当前部署版本和本地合成业务工具。共享 HTTP 服务、跨数据库幂等、远端工具对账、掉电恢复尚不属于已验证承诺。

## 运行时与存储

发布验证固定 Python 3.14.10 / SQLite 3.53.1；归档地址、文件大小和 SHA-256 见 [runtime.lock.json](../runtime.lock.json)。安装器先验摘要再解包，拒绝覆盖非匹配目录；也可使用已下载归档离线安装：

```bash
export PLANNER_RUNTIME_DIR="$(mktemp -d /tmp/content-planner-runtime.XXXXXX)"
python3 tools/bootstrap_runtime.py --destination "$PLANNER_RUNTIME_DIR/python"
# 已有归档时，增加 --archive .local/runtime/python.tar.gz
export PLANNER_PY="$PLANNER_RUNTIME_DIR/python/bin/python3"
PYTHONPATH=src "$PLANNER_PY" -m content_planner doctor --production
```

本轮在原生 Linux 临时目录成功安装和验证；仓库挂载目录的默认安装尝试返回 RUNTIME_SETUP_FAILED，原因尚未确定，不能把它称为该路径已安装成功。上面的命令显式使用已验证的原生文件系统；临时目录被清理后需重新安装，正式部署应选择持久的原生 Linux 目录。详见 [安装记录](../examples/hardening/runtime-installation.json)。

doctor 与 run 的 `--production` 只检查运行时前提，不能替代生产放行审查。当前 Python 3.10 系统环境不满足该前提。支持分支依据 [Python 生命周期](https://devguide.python.org/versions/)；SQLite 检查采用 [WAL 修复公告](https://sqlite.org/wal.html)中的版本下限，默认仍使用回滚日志，不自动开启 WAL。版本门槛不意味着旧库已经损坏。

将状态放在可信的本机 Linux 文件系统、仅运行账号可访问的目录。WSL 的 Windows 挂载盘不据 Unix mode 自动获得隔离保证；本轮多进程和强杀矩阵使用原生临时目录。不要把库放在共享网络盘。示例可在本机创建私有目录后指定 `--db`；不要在两个库上同时运行同一实际任务。程序不会自动清理状态或备份，需由操作者按保留策略管理。

## 启动、查询与取消

```bash
PYTHONPATH=src "$PLANNER_PY" -m content_planner run --input examples/cases/C01-normal/input.json --mode mock --as-of 2026-11-01T10:00:00+08:00 --db .local/work.sqlite3 --output-dir .local/result --production
PYTHONPATH=src "$PLANNER_PY" -m content_planner status --db .local/work.sqlite3 --request-id evaluation-001
PYTHONPATH=src "$PLANNER_PY" -m content_planner cancel --db .local/work.sqlite3 --request-id evaluation-001
```

`status` 只读，不迁移或创建库，不访问模型，也不读取密钥。查询时任务快照与租约来自同一个 SQLite 读取快照，避免把刚完成任务误判为崩溃。

取消是持久化请求。它与新动作登记在事务中竞争：取消先提交则后续动作无法登记；动作已经登记时可以完成该次调用，已有成功费用仍保留。保存已经提交时返回 `too_late` 和原回执。取消不是撤销远端请求或退款，也不强制中止正在执行的事务。外部结果未知时仍需对账。

默认每个库最多 2 个活跃任务；容量拒绝返回 `CAPACITY_EXCEEDED`，不创建待执行任务。不同库之间不共享额度。同请求活跃时返回 `REQUEST_IN_PROGRESS`；同 ID 不同需求返回 `REQUEST_ID_CONFLICT`。

## 恢复决策

| 查询或执行结果 | 处理 |
| --- | --- |
| running / REQUEST_IN_PROGRESS | 等待当前拥有者；不要启动另一个库绕过限制 |
| recoverable / RECOVERY_REQUIRED | 确认已停止原执行器后，用原输入、原库和 `--resume` 显式续跑 |
| reconcile_required / OPERATION_OUTCOME_UNKNOWN | 停止重新派发；保留待确认 action_id/run_id，核对网关或远端回执 |
| legacy_recovery_required | 保留备份，由操作者核对旧 running 状态；不自动推断旧操作没有发生 |
| LEASE_LOST | 原执行器已失去写权限；以当前状态和新拥有者回执为准 |
| CANCELLED | 保留已确认结果；续跑不会清除取消请求 |
| TASK_BUDGET_EXHAUSTED | 原任务累计限额已耗尽；更大的 CLI 参数不能提高已保存限额 |
| RETRY_EXHAUSTED | 同一轮同参数瞬态失败已达 3 次，保留成功结果；修复原因后可显式续跑，仍受任务总限额限制 |
| PERSISTENCE_FAILED / SAVE_FAILED | 不视为已保存；保留 stdout，修复存储后查询实际状态再决定续跑 |

```bash
PYTHONPATH=src "$PLANNER_PY" -m content_planner run --input examples/cases/C01-normal/input.json --mode mock --resume --as-of 2026-11-01T10:00:00+08:00 --db .local/work.sqlite3 --output-dir .local/resumed
```

已落盘的模型决定可直接重放，Trace 同时保留决定所属 run_id 和本次工具执行 run_id。本地查表、模板和草稿保存没有外部副作用，未提交结果可以重做，模拟费用与 SQLite 事务共同确认。这个保证不能直接移植给实际采购、远端生成或发布工具。

网关超时/连接中断属于结果未知，保留在途意图，直接续跑会被阻断。当前网关没有本项目可用的请求结果查询契约，因此**尚无自动或人工确认命令**；不要手工删改 SQLite 标记来解除阻断。取得实际回执与对账契约后再实现受审计的处理入口。HTTP 401/403不自动重试；429/503保留Retry-After并停止，批次同时停止后续请求并列出未运行场景。没有自动后台等待或未知结果重发。

## 备份、迁移与回滚

使用 [SQLite backup 接口](https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.backup)，不直接复制正在写入的数据库文件。备份是只读源库，恢复必须写入新文件，拒绝覆盖已有目标和未知新版本：

```bash
PYTHONPATH=src "$PLANNER_PY" -m content_planner backup --db .local/work.sqlite3 --output .local/backups/work-before-upgrade.sqlite3
PYTHONPATH=src "$PLANNER_PY" -m content_planner restore --input .local/backups/work-before-upgrade.sqlite3 --db .local/restored.sqlite3
PYTHONPATH=src "$PLANNER_PY" -m content_planner status --db .local/restored.sqlite3 --request-id evaluation-001
```

回执包含摘要、大小和 schema 版本；保存回执以便检查传输完整性。升级前先备份并停止旧二进制，新的可写连接事务化迁移到 schema 3；未知更高版本拒绝打开。回滚使用旧版本代码和升级前备份恢复到新库，不能让旧二进制写新库。恢复出来的库与原库不得同时处理同一任务；复制不是跨库幂等方案。

`SIGSTOP` 或整机冻结可能连 SQLite 写锁一起保留。租约过期不能夺走操作系统锁，此时新写连接在约 5 秒忙等待后报持久化失败；只读查询仍可用于判断已确认状态。先确认旧进程身份，再由操作者恢复或终止该进程，待锁释放后继续。验证代次隔离时会明确在写事务外暂停；不会把无法接管冻结写锁伪装成已恢复。

当前恢复测试只证明小型合成库可恢复已保存方案；尚无生产规模 RTO 测量、自动每日备份或备份过期告警。RPO≤24h、恢复≤10min 仍是目标。租约、fsync 和进程强杀测试不等于机器掉电测试。

## 产物与验证

导出每次创建 `.runs/<export_id>/`，`current.json` 是原子提交索引，manifest 中有文件 SHA-256。核验时先读索引，再读取该目录并检查摘要；顶层 output/trace/plan 文件是方便旧工具阅读的镜像，不具有整批原子性。索引提交前中断，上一批完整证据仍保留；没有索引则该目录没有已提交的本轮导出。历史批次不会自动删除。

```bash
"$PLANNER_PY" tools/verify.py
"$PLANNER_PY" tools/evaluate.py --mode mock --samples 20
"$PLANNER_PY" tools/check_stability.py --rounds 20 --clients 16
"$PLANNER_PY" tools/check_recovery.py --rounds 10
"$PLANNER_PY" tools/evaluate.py --mode live --max-model-requests 24
```

最后一条使用本机 `.env.local` 或环境变量，仅运行三组场景。批次请求上限跨场景模型实例共享，消耗到零即停止；它是单次评估进程内的计数，不是跨进程、跨重启或货币预算。Token 用量是已响应 usage，不把未知账单按 0 计算。

CI 定义见 [quality.yml](../.github/workflows/quality.yml)，只运行离线质量门槛，显式清空模型密钥。checkout 固定到已查询的 [官方 v7.0.1 提交](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1)。当前未发布仓库，因此工作流尚未在 GitHub runner 上实际执行。


## 已验证短期并发配置

单机、多客户端且数据库位于可信的原生Linux文件系统时，可在通过doctor补丁门槛后显式使用`--journal-mode wal`。未指定时保留已有模式；新库默认回滚日志。WAL配置不能用来绕过任务并发上限，容量在写事务内仍再次校验。拒绝请求采用只读容量预检查，以减少与已接纳任务竞争。

CLI初始化/任务领取遇忙锁会返回STATE_BUSY与retryable，不代表方案已保存；读取不到旧状态时不推断旧费用已经回退。用户或上层调用者可在有界时间内重试领取，不能无限重启整个任务。短期负载器对初次领取和续跑分别等待容量，并只允许5秒尾部续跑排空；所有拒绝、未接纳和明确失败都在报告中计数。

当前真实模型命令应使用本机wb/hy3配置，例如 `python3 tools/evaluate.py --mode live --all-cases --max-model-requests 100 --model-timeout 60`。对抗命令也须明确额度：`python3 tools/evaluate_adversarial.py --max-model-requests 24 --model-timeout 60`。3轮重规划使用 `python3 tools/evaluate_replanning.py --rounds 3 --max-model-requests 20 --model-timeout 60`。
