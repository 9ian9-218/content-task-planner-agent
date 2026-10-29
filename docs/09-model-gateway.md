# 模型网关接入记录

日期：2026-10-28（Asia/Shanghai）。用户已完成本机配置，并授权真实运行、严格验证与修复。

| 配置项 | 实际值与证据 |
| --- | --- |
| 网关 | `http://100.73.185.103:8045/v1` |
| 最初授权写法 | `claude-opus-4.6` |
| 当前实际请求与响应 ID | `claude-opus-4-6-thinking`；网关模型列表公开的同系列 Opus 4.6 ID |
| 请求入口 | `POST /v1/chat/completions`，非流式、原生 function tools |
| 凭据 | 仅本机环境或被忽略的 `.env.local`，报告和 Trace 不含密钥 |
| 业务工具 | 四个本地合成工具；另有一个 `finish_task` 结束控制动作 |

## 连通性与模型 ID 的实际核验

初次请求被本地沙箱拒绝连接；通过授权网络权限后，网关可达。原配置的三组 Agent 调用均返回 HTTP 503。脱敏响应说明账号池限流冷却，并给出 Retry-After；不是成功模型响应，不能作为决策路径证据。

认证后的 `/models` 返回 17 个 ID，没有列出最初写法，列出 `claude-opus-4-6-thinking`。对该 ID 的单次工具探测成功，响应返回相同 ID 与实际工具调用。首次 503 同时涉及账号池状态，不能只凭模型列表断言全部失败均由别名导致。

随后使用已列出的同系列 ID 完成真实正常、预算重规划与保存失败场景。本机配置仅修改 MODEL_NAME，保留密钥及地址；`.env.example` 同步该值。运行前应显式设置示例中的三个变量；环境变量优先于文件。没有自动换为其他模型或回退 Mock。

证据：[脱敏网关诊断](../examples/live/gateway-diagnostics.json)、[列出模型的实际工具探测](../examples/live/listed-model-probe.json)、[最初 HTTP 503 运行](../examples/live/runs/01-gateway-503/evaluation-summary.json)。响应模型名只能证明网关报告的身份，不能独立核验底层不可变模型快照。

## 已复现并修复的协议问题

真实保存失败后，模型曾输出说明文字加 Markdown JSON 块，触发 MODEL_RESPONSE_INVALID。重放实际观察也复现了该格式，见[公开结束响应](../examples/live/terminal-response-probe.json)，其中未保存 reasoning_content 等内部思维字段。

现在以原生 `finish_task` 接收 status、reason_code、summary。仅缺少必要字段或资料时使用 needs_input；预算/截止不可满足或工具失败时使用 incomplete，错误码来自固定枚举。结束动作必须单独调用，不能宣称 completed 或与业务动作混用。它计入模型决策次数，只有保存事务成功才能完成。

保留的纯 JSON 结束兼容入口使用相同校验，不接受 Markdown、额外字段、未知错误码或矛盾状态。超深 JSON 返回明确失败，不让请求崩溃后残留 running。HTTP 错误保留状态码和数值 Retry-After，不转存可能带敏感内容的原始响应正文，也不自动无限重试。

相关证据：[接入边界测试](../tests/test_gateway.py)、[评估命令测试](../tests/test_live_evaluation.py)、[实际 TDD 账本](../examples/tdd/ledger.jsonl)。当前工程回归 63/63 通过、0 跳过。实际模型结果、Token 数量及失败归档见[评估报告](03-evaluation.md)。

## 复现真实验证

保留 `.env.local` 中正确的 MODEL_BASE_URL、MODEL_NAME 和 MODEL_API_KEY，在项目根目录执行：

```bash
python3 tools/evaluate.py --mode live --all-cases
python3 tools/evaluate_adversarial.py
```

第一条覆盖完整 18 场景，包括不需要模型的输入验证、缓存和显式缺凭据控制组；不能把 18 个场景都称为 18 次模型任务成功。默认不加 --all-cases 时只运行正常、重规划和保存失败三组。

第二条验证两条诱导越权输入，检查预算未被修改、超限不收费、保存失败不生成成功回执，且保留已有脚本与费用。每次以隔离数据库运行，Trace 和公开决策由实际执行生成。

缺密钥时真实验收入口退出码为 2，不请求模型、不回退 Mock。网关曾出现账号池冷却；若再次限流，应保留失败和等待提示，不能将失败样本删掉后声称稳定性达标。当前样本不证明长期稳定性、生产可用性或模型服务 SLA。


## 用户授权的本地网关切换

后续用户明确授权 `http://127.0.0.1:4000/v1` 的 `wb/hy3` 并提供本机访问凭据；凭据未写入文档、示例配置或提交包。读取模型列表返回200且列出该ID，原生工具循环的三组冒烟已实际通过。当前完整验收、对抗和重复重规划以最新报告为准；原Opus的503/Retry-After失败、受限探测与历史通过分别保留。

环境变量优先于`.env.local`；评审若换成自己的服务，应同时设置MODEL_BASE_URL、MODEL_NAME与MODEL_API_KEY，不把旧默认配置当作当前实测服务。接口不可用时输出明确失败，不切换成Mock伪造模型结果。每个真实评估命令要求显式请求上限；缺少usage不据此宣称账单为0。
