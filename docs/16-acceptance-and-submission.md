# 题目2逐项验收与提交说明

本页按题目原文组织。运行方式选择CLI；界面、部署、多Agent、生产Runtime均不属于本题必交。仓库名称没有硬性要求，建议名为`content-task-planner-agent`。提交时必须提供题号、GitHub URL和最终commit SHA；私有仓库须让指定评审账号可读。

## 业务验收矩阵

| 编号 | 题面要求 | 结果核对点 | 当前代码离线实测文件 |
| --- | --- | --- | --- |
| AC-01 | 正常完成 | 任务计划、实际步骤和工具结果齐备；模拟费用8≤预算10；已保存方案与回执一致 | [输入](../examples/mock/C01-normal/input.json)、[输出](../examples/mock/C01-normal/output.json)、[Trace](../examples/mock/C01-normal/trace.md) |
| AC-02 | 缺预算 | needs_input，指出budget_units；模型/工具请求0，不代填预算 | [输出](../examples/mock/C02a-missing-budget/output.json)、[程序终态记录](../examples/mock/C02a-missing-budget/trace.jsonl) |
| AC-03 | 缺截止时间 | needs_input，指出deadline；不生成或保存 | [输出](../examples/mock/C02b-missing-deadline/output.json) |
| AC-04 | 工具失败 | 保存失败明确未完成，保留已生成脚本和模拟费用8；失败步骤不写为成功 | [输入](../examples/mock/C03-save-failure/input.json)、[输出](../examples/mock/C03-save-failure/output.json)、[Trace](../examples/mock/C03-save-failure/trace.md) |
| AC-05 | 预算超限后调整 | 先观察standard报价12，再询价economy报价8，重新估算后生成保存 | [输入](../examples/mock/C04-replan-budget/input.json)、[输出](../examples/mock/C04-replan-budget/output.json)、[Trace](../examples/mock/C04-replan-budget/trace.md) |
| AC-06 | 无可行预算方案 | 预算5时两档均不可行；incomplete/BUDGET_EXCEEDED，保留缺口，不修改硬预算 | [输出](../examples/mock/C05-no-feasible-plan/output.json) |
| AC-07 | 重复执行 | 同需求仍一份方案；新增模型/工具动作和模拟费用均0 | [首次](../examples/mock/C06-repeat/first/output.json)、[重复](../examples/mock/C06-repeat/output.json) |
| AC-08 | 失败续跑加分项 | 同需求续跑只补一次保存；脚本ID不变，已发生费用不清零也不重收 | [失败前](../examples/mock/C14-resume/first/output.json)、[续跑后](../examples/mock/C14-resume/output.json) |
| AC-09 | 有限执行 | 达到模型/工具上限返回明确未完成；续跑不提高任务累计额度 | [运行输出](../examples/mock/C08-step-limit/output.json)、[自动测试](../examples/test-results.txt) |
| AC-10 | 非成功场景完整性 | 检索失败、生成失败、资料不足、截止不足、请求ID冲突均有实际结果 | [离线18点汇总](../examples/evaluation-summary.json) |

以上覆盖至少三组输入、实际输出与运行记录：正常、信息不足、工具失败均有独立文件。同一案例可以覆盖预算、状态、依赖等多个验收点。

## 模型驱动决策与两条路径

`LiveModel`输出原生工具调用；程序执行工具，把实际结果作为下一条tool observation交回模型，再由模型选择下一步或finish_task。模型选择档位和停止动作；预算、截止、依赖、保存事实由程序强制校验。

- 正常路径：search_materials → estimate_cost → generate_script → save_plan。
- 报价变化路径：search_materials → estimate_cost(12，超预算) → estimate_cost(8，可行) → generate_script → save_plan。

实际模型的[正常Trace](../examples/live/C01-normal/trace.md)、[重规划Trace](../examples/live/C04-replan-budget/trace.md)和[汇总](../examples/live-evaluation-summary.json)包含参数、返回、后续动作、公开依据、模型及版本。以manifest和汇总中的模型、source_sha256与运行时间为准；正在补测或被网关阻断的场景不算通过。

历史已完成的两条真实模型路径另保留在[稳定性阶段归档](../examples/live/runs/06-cancellation-guard-final/evaluation-summary.json)中。它们属于旧源码/Opus模型，不冒充当前`wb/hy3`复测。离线Mock只证明流程和约束；不以Mock或受控HTTP替身证明真实模型决策。内部思维过程不保存。

## 材料清单

| 要求 | 仓库位置 |
| --- | --- |
| 环境、安装、运行、评审入口 | [README](../README.md)、[提交入口](../SUBMISSION.md) |
| 代码与模型/工具接入 | [src](../src/content_planner/) |
| 可执行测试 | [tests](../tests/)；[实际测试输出](../examples/test-results.txt) |
| 输入、实际输出、Trace与测试结果 | [examples](../examples/)、[评估报告](03-evaluation.md) |
| 合成资料、费用与耗时表 | [素材](../data/materials.json)、[工具表](../data/tool_catalog.json) |
| 用户、目标、范围、成功标准 | [01-requirements](01-requirements.md) |
| 流程、动态决策、技术取舍 | [02-solution](02-solution.md) |
| 预期、实际、失败和限制 | [03-evaluation](03-evaluation.md) |
| AI辅助、个人修改与验证 | [04-ai-usage](04-ai-usage.md)、[参与记录](17-candidate-participation.md) |
| 难点、学习与下一步 | [05-retrospective](05-retrospective.md) |
| 依赖与变量 | [requirements](../requirements.txt)、[.env.example](../.env.example)、[运行时锁](../runtime.lock.json) |
| 实际用时与未完成部分 | [执行日志](07-execution-log.md)、[个人实际记录](17-candidate-participation.md)、[提交元数据](../submission.json) |

本项目使用Python标准库原生循环，没有采用Agent编排框架。实际解释器/SQLite版本由各报告记录，无第三方运行时包。所用Skill帮助开发和测试，不是运行时依赖。

## 短期质量验证

详见[短期计划](15-short-validation-and-delivery.md)。5分钟混合负载采用8个真实进程、同库最多2个活跃任务；记录接纳/容量拒绝、混合正确结果、耗时、RSS、线程和FD，明确使用已验证SQLite补丁版本的本地WAL。并发与强杀矩阵另做验证，不能将这些结果称为24小时稳定性或长期SLA。

`tools/check_delivery.py`核对材料、结果一致性、版本和缺项；`tools/package_submission.py`按白名单打包并排除私有配置、数据库、缓存和符号链接。包内manifest记录文件摘要。最终GitHub URL/SHA和个人实际用时须为真实值，不能以计划或摘要替代。

## 提交步骤

1. 阅读[提交入口](../SUBMISSION.md)，在干净目录运行离线命令；复核模型记录和失败边界。
2. 在`submission.json`补入实际GitHub URL、可见性；私有仓库确认评审权限。
3. 如实登记个人参与、AI执行的验证范围和真实人工用时；题目允许AI辅助，不额外要求所有代码或测试必须手工完成。一天半/12小时是建议计划，未经实际确认不得当作已发生工时。
4. 提交全部材料到同一仓库；在最终提交后运行`git rev-parse HEAD`，将返回值填入考核平台。SHA不写回自身所在提交，避免自引用。
5. 按“第2题 / GitHub URL / 最终commit SHA”提交。目标仓库[https://github.com/9ian9-218/content-task-planner-agent](https://github.com/9ian9-218/content-task-planner-agent)已核实为公开、可推送。以远程main的最终HEAD填写SHA，未核实推送或CI结果时不写已通过。
