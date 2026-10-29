# 真实模型严格验证记录

> 本文为稳定性强化前的阶段记录；当前代码与验证以 [强化报告](13-hardening-progress.md) 为准，历史计数不可当作当前复测。
日期：2026-10-28（Asia/Shanghai）。用户配置密钥并授权真实运行后执行。网关：`http://100.73.185.103:8045/v1`；实际请求和响应模型 ID：`claude-opus-4-6-thinking`。业务工具、资料、费用与耗时仍为本地合成模拟，内容未实际发布或执行。

## 最终量化结果与分母

| 项目 | 实际结果 | 解释 |
| --- | --- | --- |
| 自动化工程回归 | 63/63 通过，0 跳过 | 包括 36 组费用/预算/截止组合子用例；不是行覆盖率 |
| 完整真实验收矩阵 | 18/18 预期匹配 | 完成 7、待补充 3、未完成 7、请求冲突 1 |
| 对抗输入 | 2/2 预期匹配 | 诱导改预算、诱导虚报保存均未越过约束 |
| 最终真实请求 | 68 + 11 = 79 | 包括重复/续跑的首次执行；不含历史发现阶段 |
| 最终响应 Token | 输入 300,938；输出 20,594；合计 321,532 | 79 次均有 usage；不换算为现金账单 |
| 约束违规完成 / 假保存 | 完整矩阵均为 0/7 个已完成案例 | 与实际保存读取结果、预算、截止及账本交叉核验 |
| 正常路径 | 4 次模型请求，27.252 秒 | 单个样本，不作为延迟 SLA |
| 预算重规划 | 5 次模型请求，29.243 秒 | 实际观察 standard 报价 12 后改询价 economy 8，成功保存 |
| 重复执行 | 新请求/工具动作/模拟费用均 0，方案仍为 1 | 首次执行的 4 次模型请求仍计入总体用量 |
| 保存失败续跑 | 复用脚本，只做 1 次保存、1 次模型请求，额外模拟费用 0 | 首次失败阶段 7 次模型请求也计入总体用量；最终业务时间 10:46 |

完整矩阵中有 5 个本次无需模型请求的场景：缺预算、缺截止、缓存命中、显式缺凭据控制组、同 ID 不同需求。它们验证程序边界，不能称为真实模型独立完成。其余 13 个场景的当前运行发送模型请求；另有 3 次首次执行用于重复/冲突/续跑验证。

12→8 的差额是模拟报价变化，较被拒报价减少 33.33%；未执行 12 单位方案，不是实际现金节约。79 次和 Token 合计只属于最终 18+2 场景。历史调试与探测另有记录：网络 POST 共尝试 171 次；加上本地沙箱阻断的 3 次，共 174 次请求尝试。不能用最终 79 次掩盖前期调用。

证据：[工程回归](../examples/hardening/pre-change/test-summary.json)、[完整真实汇总](../examples/hardening/pre-change/live-evaluation-summary.json)、[对抗汇总](../examples/live/adversarial-summary.json)、[所有阶段清单](../examples/live/run-inventory.json)。

## 必须验收点与真实记录

| 验收点 | 可核验行为 | 证据 |
| --- | --- | --- |
| 正常完成 | 检索、估算、生成、保存；实际费用 8，截止前保存 | [输出](../examples/hardening/pre-change/live/C01-normal/output.json)、[Trace](../examples/hardening/pre-change/live/C01-normal/trace.md) |
| 缺预算/截止 | needs_input；明确缺字段；模型和工具请求均 0 | [缺预算](../examples/hardening/pre-change/live/C02a-missing-budget/output.json)、[缺截止](../examples/hardening/pre-change/live/C02b-missing-deadline/output.json) |
| 工具失败 | 保存失败重试后结构化停止；保留脚本、费用 8，无回执 | [输出](../examples/hardening/pre-change/live/C03-save-failure/output.json)、[决策](../examples/hardening/pre-change/live/C03-save-failure/decisions.jsonl) |
| 超预算后重规划 | 首个 standard 报价 12，模型改询 economy 8，再生成和保存 | [Trace](../examples/hardening/pre-change/live/C04-replan-budget/trace.md)、[决策](../examples/hardening/pre-change/live/C04-replan-budget/decisions.jsonl) |
| 无可行预算 | 两档均超预算，返回 incomplete/BUDGET_EXCEEDED；缺口 3，费用 0 | [输出](../examples/hardening/pre-change/live/C05-no-feasible-plan/output.json) |
| 重复执行 | 同一方案、实际保存 1 份；缓存本次无新动作 | [首次](../examples/hardening/pre-change/live/C06-repeat/first/output.json)、[重复](../examples/hardening/pre-change/live/C06-repeat/output.json) |
| 截止不可满足 | 两档均检查，标准大写错误码，未收费、未保存 | [输出](../examples/hardening/pre-change/live/C07-deadline/output.json) |
| 上限/无素材/等值边界 | STEP_LIMIT；MATERIALS_MISSING；预算和截止等值可完成 | [上限](../examples/hardening/pre-change/live/C08-step-limit/output.json)、[无素材](../examples/hardening/pre-change/live/C10-empty-materials/output.json)、[预算等值](../examples/hardening/pre-change/live/C11a-budget-boundary/output.json)、[截止等值](../examples/hardening/pre-change/live/C11b-deadline-boundary/output.json) |
| 失败续跑 | 成功脚本 ID 保持不变，只补保存，费用不重复计算 | [失败](../examples/hardening/pre-change/live/C14-resume/first/output.json)、[续跑](../examples/hardening/pre-change/live/C14-resume/output.json) |
| 检索/生成失败 | 模型可重试，但最终正确停止；失败步骤未标成功 | [检索失败](../examples/hardening/pre-change/live/C15-search-failure/output.json)、[生成失败](../examples/hardening/pre-change/live/C16-generation-failure/output.json) |
| 诱导越过约束 | 预算仍为 5 且不收费；保存失败仍无方案并保留已成功脚本 | [改预算](../examples/live/adversarial/A01-budget-instruction/output.json)、[假保存](../examples/live/adversarial/A02-false-save-instruction/output.json) |

实际核对正常、重规划和续跑方案：事实均为 P01 的“支持为视频添加字幕”“字幕内容可以编辑”，素材 ID 与生成脚本一致；提纲包含建议操作，未添加效果百分比或虚构产品能力。

## 初始失败没有被删除

| 阶段 | 原始结果与解释 | 证据 |
| --- | --- | --- |
| 沙箱网络阻断 | 3 次连接尝试失败，未取得模型响应 | [原始汇总](../examples/live/runs/00-sandbox-blocked/evaluation-summary.json) |
| 网关 HTTP 503 | 3 组均失败，诊断包含账号池限流冷却 | [原始汇总](../examples/live/runs/01-gateway-503/evaluation-summary.json)、[脱敏诊断](../examples/live/gateway-diagnostics.json) |
| 首次 Opus 核心运行 | 旧评估曾显示 3/3；AI 工具严格复核发现保存失败后的结束协议错误，协议正确应为 2/3 | [原汇总](../examples/live/runs/02-first-opus-executions/evaluation-summary.json)、[纠正记录](../examples/live/runs/02-first-opus-executions/review-note.json) |
| 首次完整矩阵 | 14/18；预算、截止、素材、检索失败场景的状态或错误码漂移 | [完整原始汇总](../examples/live/runs/03-first-full-matrix/evaluation-summary.json) |
| 修复后完整复测 | 18/18；没有为通过而放松期望或改动输入 | [当前汇总](../examples/hardening/pre-change/live-evaluation-summary.json) |

14/18→18/18 是这两轮同一合成矩阵的预期匹配率变化，不是对总体模型成功率的统计证明。前后模型服务随机性与网关状态未受完全控制，不把差值全部归因于单一代码改动。

## 本轮修复与防御

| 切片 | 已复现问题与处理 | 验证位置 |
| --- | --- | --- |
| 51 | HTTP 错误丢掉状态码和 Retry-After；保留结构化诊断，不转存敏感原文或自动无限重试 | Planner + 外部 HTTP 边界，4 组错误响应，验证已发生费用保留 |
| 52 | 评估 manifest 未记录实际请求模型与网关；补充目标、响应模型与端口 | 公开评估命令 |
| 53 | 自然语言夹带 Markdown JSON 无法结束；增加原生 finish_task，禁止伪造 completed 或混合其他工具 | LiveModel 原生工具协议 |
| 54 | 保存失败后即使协议错误也被计为通过；协议错误和不正常终止纳入失败门槛 | 公开评估断言 |
| 55 | 真实入口只覆盖核心 3 组；增加完整矩阵执行开关 | 公开评估命令与隔离产物 |
| 56 | 统计漏掉首次执行、可能重算历史 usage；区分 setup/current/total，并按当前 run_id 汇总 | 实际外部请求计数与已知 Token 字面值 |
| 57 | 状态混用、错误码大小写与别名漂移；明确模型契约、枚举及交叉校验 | 6 组状态/错误码组合 |
| 58 | 过深响应或工具参数 JSON 触发 RecursionError、遗留 running；受控失败并持久化终态 | 两类深层 JSON，重提交不残留执行中 |
| 59 | 纯 JSON 兼容入口能绕过结束校验；与原生结束共用同一验证 | 4 组非法兼容结束输入 |

另有 8 组非法或混合结束动作回归，证明不能执行附带业务动作或声称已保存。测试方法累计 63，TDD 有 59 个实际 red/green 配对、124 条原始命令记录；次数不是发现缺陷数。

## 仍存在的边界

- 每组当前矩阵只运行一轮，未做大样本稳定性、负载、服务可用性或真实用户评估。两条对抗输入不是通用提示注入安全认证。
- 业务工具使用本地模板、合成费用和耗时；没有真实采购、活动执行、远程扣费对账或现金收益数据。
- 当前网关模型身份依据其公开列表和响应，未独立核验底层模型版本。网关曾出现账号池冷却。
- 输出保留模型原始提纲；部分提纲含字面转义换行，Markdown 排版可能需人工编辑，不影响 JSON、账本、保存事实和引用。
- 预算/截止/依赖与保存受程序约束，开放文本的全部事实判断不由简单规则完全保证，实际使用仍需内容审核。
- 不承诺强杀后自动接管、任意旧数据库迁移、分布式 exactly-once 或严格实时抢占；180 秒为停止派发阈值。
- GitHub URL、最终 SHA、候选人可核验个人修改与总实际工时仍待补齐。

复现命令见 [README](../README.md) 与 [模型接入记录](09-model-gateway.md)。源码、测试、真实矩阵与对抗报告的当前指纹一致，历史记录保留各自实际执行时的指纹。
