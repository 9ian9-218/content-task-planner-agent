# 内容任务规划 Agent · 题目2

在预算和截止时间内形成可编辑、可核验的内容活动方案。真实模型选择工具、观察返回并调整下一步；程序校验硬约束、依赖、费用与保存事实。业务工具使用合成资料和模拟费用，只保存方案，不执行活动或发布。

**评审从 [SUBMISSION.md](SUBMISSION.md) 开始。** 逐项对应题面的[验收矩阵](docs/16-acceptance-and-submission.md)、[最终交付报告](docs/19-delivery-report.md)、[当前评分](docs/20-current-quality-assessment.md)和[AI使用记录](docs/04-ai-usage.md)均在同一仓库。

## 当前实测

2026-10-29T01:41:38.888411+00:00，Python 3.14.10 / SQLite 3.53.1，WSL2/Linux。下表对应同一业务源码 `0bec91ee99c15d6542982ff68d8dcdeff126c9f32f5a80c89d6a6699a37e9a27`。

| 检查 | 实际结果 |
| --- | --- |
| 自动回归 | 103/103，0跳过；清洁ZIP解压副本再次103/103，代码与测试指纹一致 |
| 离线验收 | 18/18，覆盖正常、缺信息、失败、预算、重复、续跑与边界 |
| 当前真实模型完整矩阵 | wb/hy3：18/18；68次请求，129,357个已报告Token |
| 真实对抗检查 | 2/2；诱导修改预算与虚报保存均未越过约束 |
| 重复重规划 | 3/3轮均观察12后改询价8并保存；这是同一案例重复，不是独立样本 |
| 最终三批真实用量 | 93次请求，178,489 Token；三批请求上限合计144，不含历史调试 |
| 5分钟稳态混合负载 | 8客户端，每客户端每2秒新任务，约4个/秒；1200场景均符合预期，515份方案与预期一致 |
| 负载资源与拒绝 | FD/线程回到基线；单工作者RSS峰值25.18 MiB；容量拒绝1276次、忙锁3次均记录并有界处理 |
| 突发竞争与恢复 | 16进程×20轮，共320次提交；8强杀位置×10轮，80/80恢复检查 |

证据：[清洁复现](examples/short-validation/clean-room-check.json)、[测试](examples/test-summary.json)、[离线](examples/evaluation-summary.json)、[真实矩阵](examples/live-evaluation-summary.json)、[短测汇总](examples/short-validation/final-summary.json)。测试方法数不是代码覆盖率；18/18是预期行为匹配，包含正确失败与追问，不是18个任务都完成。故障与负载矩阵使用离线模型，不访问网关。

## 安装与运行

仅依赖Python标准库，无pip依赖。完整验证使用固定Python 3.14.10 / SQLite 3.53.1，见[runtime.lock.json](runtime.lock.json)。仓库位于Windows挂载盘时，建议显式安装到原生Linux路径：

```bash
export PLANNER_RUNTIME_DIR="$(mktemp -d /tmp/content-planner-runtime.XXXXXX)"
python3 tools/bootstrap_runtime.py --destination "$PLANNER_RUNTIME_DIR/python"
# 已下载归档时，可增加 --archive .local/runtime/python.tar.gz
export PLANNER_PY="$PLANNER_RUNTIME_DIR/python/bin/python3"
PYTHONPATH=src "$PLANNER_PY" -m content_planner doctor --production
PYTHONPATH=src "$PLANNER_PY" -m content_planner run --input examples/cases/C01-normal/input.json --mode mock --as-of 2026-11-01T10:00:00+08:00 --db .local/normal.sqlite3 --output-dir .local/normal
"$PLANNER_PY" tools/verify.py
"$PLANNER_PY" tools/evaluate.py --mode mock --samples 20
```

临时路径被清理后需重装；正式持久安装选择私有的原生Linux目录。项目挂载目录的默认运行时安装失败尚待定位，已验证原生路径可用。系统Python 3.10可做核心CLI兼容演示，完整WAL/平台测试使用上面的固定运行时；`--production`只检查运行时前提，不是生产认证。

## 题面必验路径

```bash
# 报价12超限后改选8
PYTHONPATH=src "$PLANNER_PY" -m content_planner run --input examples/cases/C04-replan-budget/input.json --scenario data/scenarios/over-budget.json --mode mock --as-of 2026-11-01T10:00:00+08:00 --db .local/replan.sqlite3 --output-dir .local/replan
# 保存失败，预期退出码3
PYTHONPATH=src "$PLANNER_PY" -m content_planner run --input examples/cases/C03-save-failure/input.json --scenario data/scenarios/save-failure.json --mode mock --as-of 2026-11-01T10:00:00+08:00 --db .local/resume.sqlite3 --output-dir .local/failed
# 修复模拟保存故障后续跑，不重复收取生成费用
PYTHONPATH=src "$PLANNER_PY" -m content_planner run --input examples/cases/C03-save-failure/input.json --mode mock --resume --as-of 2026-11-01T10:00:00+08:00 --db .local/resume.sqlite3 --output-dir .local/resumed
```

重复同一需求返回同一方案，新增模型/工具动作与模拟费用为0。同ID不同需求明确拒绝。CLI退出码：0完成，2输入/配置错误或待补充，3未完成，4导出失败。

## 模型与短期复测

用户当前授权`http://127.0.0.1:4000/v1`的`wb/hy3`。复制`.env.example`到被忽略的`.env.local`，填入自己的密钥；或使用MODEL_API_KEY / MODEL_BASE_URL / MODEL_NAME环境变量。不要提交真实密钥。适配器按显式配置运行，历史Opus结果与当前模型分开保存。

```bash
"$PLANNER_PY" tools/evaluate.py --mode live --all-cases --max-model-requests 100 --model-timeout 60
"$PLANNER_PY" tools/evaluate_adversarial.py --max-model-requests 24 --model-timeout 60
"$PLANNER_PY" tools/evaluate_replanning.py --rounds 3 --max-model-requests 20 --model-timeout 60
"$PLANNER_PY" tools/check_short_run.py --duration-seconds 300 --clients 8 --minimum-completed 300 --request-interval 2
"$PLANNER_PY" tools/check_stability.py --rounds 20 --clients 16
"$PLANNER_PY" tools/check_recovery.py --rounds 10
"$PLANNER_PY" tools/check_delivery.py
```

缺密钥或服务不可用会明确失败，不切换Mock。提供商错误会停止后续批次派发并列出未运行场景；请求额度不等于货币预算。示例模型调用结果来自真实执行，内部思维过程不保存。

## 工程范围与限制

同库默认最多2个活跃任务，租约60秒、心跳10秒，旧代次不能写回。已有库不重复申请迁移写锁；容量先只读拒绝、事务内再校验。任务累计限额跨续跑保留；取消与动作登记在事务内裁决；外部结果未知则阻断重发。已登记动作可能完成，取消不是退款。

新库默认回滚日志；只有显式指定`--journal-mode wal`且SQLite补丁门槛确认时才启用WAL。短测使用本机原生文件系统，不能推广到网络共享盘。导出以current.json指向的不可变批次为准，顶层文件是便利镜像。查询、备份、恢复与升级见[运行手册](docs/14-operations.md)。

无节流饱和测试曾出现1次明确保存失败、早期锁异常和人工短租约夹具问题，均保留在[失败归档](examples/short-validation/runs/)。本次按用户约束采用短测；不承诺长期SLA、真实远端扣费对账或实际运营收益。12→8为合成报价变化，不是现金节约。

## 材料与提交状态

[PRD](docs/01-requirements.md)、[方案](docs/02-solution.md)、[评估](docs/03-evaluation.md)、[AI使用](docs/04-ai-usage.md)、[复盘](docs/05-retrospective.md)、[逐条验收](docs/16-acceptance-and-submission.md)、[设计解释](docs/18-design-review.md)与依赖/示例齐备。TDD账本保留99个实际red/green配对和206条命令，包含独立的进程/负载检查，不能把所有red都称为业务缺陷。

本地技术验证已完成；GitHub URL、可见性/评审权限和最终远程commit SHA仍待用户提供的仓库确认。个人实际投入与独立复现尚未据实登记；[一天半/12小时参与计划](docs/17-candidate-participation.md)不冒充已经发生的工时。最终提交状态见[submission.json](submission.json)与[交付检查](examples/delivery-check.json)。
