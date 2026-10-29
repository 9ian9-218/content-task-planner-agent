# 同类功能调研：内容任务规划 Agent

调研日期：2026-10-28（Asia/Shanghai）。状态：官方资料调研，未安装或实测以下产品；本文件不代表项目已经实现或测试通过。

## 1. 范围与证据口径

围绕题目 2 的核心问题：输入主题、预算、截止时间，检索素材、生成脚本、估算成本，并在超预算、工具失败或部分成功后调整行动，最后可靠地保存方案。

选取两类参照：Flowise、Dify、n8n 代表可自托管的 Agent/自动化平台；Jasper、Copy.ai 代表商业内容与营销工作流产品。两类产品的目标和交付方式不同，不做统一性能排名。所有“优势/局限”均指**对本次三小时考核项目的适配性判断**。

另核验了题面[交付示例 README](https://github.com/lwz1357/agent-campus-submission-example/blob/main/README.md)：它明确说明采用离线确定性 Mock、不调用大模型。本项目只参考其交付组织方式，不把它作为真实模型决策效果的依据，也不沿用其中的示例题号。[S18](https://github.com/lwz1357/agent-campus-submission-example/blob/main/README.md)

证据分为三层：

- **文档事实**：官方技术文档、官方许可证中可以直接核验的描述。
- **产品方声明**：营销页面对产品行为的描述，尚未通过本次实测确认。
- **项目推论**：根据题目要求得出的设计取舍；不是对竞品能力的否定。

公开文档未提及某个能力，只能写“本次未核验”，不能推断该产品不支持。平台的 Token 消耗、订阅额度、工作流计费与题目中的**内容活动预算**是不同概念；日志存在也不等于业务状态正确或重复保存已经幂等。

## 2. 五个产品的相关功能与取舍

| 产品 | 代码与交付形态 | 已核验的相关功能 | 对本项目的优势（推论） | 适配成本与局限（推论） |
| --- | --- | --- | --- | --- |
| Flowise | 普通代码 Apache-2.0；企业目录及特定文件采用商业许可，不能把整仓库一概称为 Apache 开源。[S01](https://github.com/FlowiseAI/Flowise/blob/main/LICENSE.md) | Agentflow V2 的 Agent 动态选工具，支持共享状态、人工输入与检查点；提供逐步 Trace。[S02](https://docs.flowiseai.com/using-flowise/agentflowv2) [S03](https://docs.flowiseai.com/using-flowise/analytics) | 便于可视化解释“观察结果后再次行动”，状态和恢复思路可借鉴。 | 安装、导出与评审复现多一个平台环节；仍需自行实现业务费用、时间和防重复规则。 |
| Dify | 源码可见，基于修改版 Apache-2.0，附加多租户和前端标识限制；不等同于未经修改的 Apache 许可证。[S04](https://github.com/langgenius/dify/blob/main/LICENSE) | Classic Agent 节点支持 Function Calling/ReAct、最大迭代数、工具输出和日志；部分节点支持错误处理。[S05](https://docs.dify.ai/en/cloud/use-dify/nodes/agent) [S06](https://docs.dify.ai/en/cloud/use-dify/build/predefined-error-handling-logic) | 能较快搭建带工具的决策循环，可借鉴明确的工具描述和迭代限制。 | 需要交付自定义业务逻辑与平台配置，不能只交画布或截图；具体错误处理能力应按节点核验。 |
| n8n | 社区代码采用 Sustainable Use License，企业部分另有许可；本文归入源码可见。[S07](https://github.com/n8n-io/n8n/blob/master/LICENSE.md) | Tools Agent 选择工具；有迭代上限、中间步骤、错误工作流和历史执行调试。[S08](https://docs.n8n.io/integrations/builtin/cluster-nodes/root-nodes/n8n-nodes-langchain.agent/tools-agent/) [S09](https://docs.n8n.io/build/flow-logic/handle-errors-gracefully.md) [S10](https://docs.n8n.io/build/understand-workflows/understand-executions/debug-executions.md) | 适合把多个外部业务工具连接起来，错误执行可追查。 | 本题允许本地工具，丰富连接器的收益有限；错误工作流或重新运行本身不能证明模型重规划，也不能自动保证幂等。 |
| Jasper | 商业 SaaS，作为闭源产品对照；本次未核验到整套产品的开源许可证，不对局部 SDK 的许可作推断。 | Canvas 文档有项目上下文、素材管理和协作；Agents 页面声明多步执行、记忆与迭代；开发文档提供结构化任务调用。[S11](https://help.jasper.ai/hc/en-us/articles/55320360614299-Jasper-Canvas) [S12](https://www.jasper.ai/agents) [S13](https://developers.jasper.ai/docs/using-agents) | 贴近内容团队的“简报→素材→内容产出”心智，能借鉴统一上下文和可编辑交付。 | 产品界面与平台内部决策不能代替候选人的实现证据；本次未核验到活动预算、截止时间及幂等保存的完整技术保证。 |
| Copy.ai | 商业 SaaS，作为闭源产品对照；本次未核验到整套产品的开源许可证。 | Workflows 页面描述可复用 Actions、事件触发与流程；Copy Agents 页面声明可在约束内做局部决策；官方指南展示检索、提纲、草稿、修改链路。[S14](https://www.copy.ai/platform/what-are-workflows) [S15](https://www.copy.ai/platform/copy-agents) [S16](https://www.copy.ai/guides/supercharge-your-content-creation-with-copy-ai-workflows) | 说明内容生产适合拆为有输入、输出和依赖的步骤，Agent 可以嵌入流程。 | 不能简单认定它只有固定串行调用；但产品方的“约束内决策”声明不能替代本题所需的费用核算与真实执行记录。 |

## 3. 进一步核验及容易误读的地方

### Flowise：把“可选择工具”和“固定执行工具”分开

官方明确区分 Agent Node 的动态选择与 Tool Node 的指定执行；Loop 有循环上限，人工输入使用检查点。[Agentflow V2](https://docs.flowiseai.com/using-flowise/agentflowv2) 本项目应保留这种职责区分：允许程序校验动作，但替代方案应由看到工具结果的模型选择。

Flowise 的评估功能文档标注 Cloud/Enterprise 可用。[Evaluations](https://docs.flowiseai.com/using-flowise/evaluations) 因此不能因为源码中存在某能力，就假定免费自托管版本具备同等功能；本项目可以直接输出 JSONL Trace 和本地测试报告。

### Dify：错误分支的存在不等于任何节点都能恢复

错误处理文档列出 LLM、HTTP、Code、Tool 节点，提供停止、默认值、失败分支等模式。[Handle Errors](https://docs.dify.ai/en/cloud/use-dify/build/predefined-error-handling-logic) 本次不把这些描述扩大到所有 Agent 节点。对本项目，失败应形成带错误码的工具结果，供下一次模型决策使用；备用结果不能伪装成原步骤成功。

### n8n：执行重跑与业务续跑有区别

官方提供从历史执行载入数据后调试和重新运行；可用范围与数据保存设置有关。[Debug executions](https://docs.n8n.io/build/understand-workflows/understand-executions/debug-executions.md) 本项目若做续跑，应验证已成功步骤不被重复执行、已发生费用不被清零、失败依赖得到正确处理，而不是仅增加一个“重新运行”按钮。

### Jasper 与 Copy.ai：工作流骨架可以存在，动态决策仍要举证

两家的公开资料都包含工作流与 Agent 的组合，不能把“有预设流程”当作“没有模型决策”的证据。[Jasper Agents](https://www.jasper.ai/agents) [Copy Agents](https://www.copy.ai/platform/copy-agents) 本题的有效证据应是：工具返回不同结果后，实际模型选择了不同的下一步，并能核对调用参数与最终状态。

## 4. 对本项目的产品定位

**定位建议：一个在明确预算和截止时间内形成内容方案，并能解释执行状态、可靠保存结果的轻量 Agent。** 差异化来自可核验性，不宣称替代商业营销平台。

借鉴：

1. 从 Jasper/Copy.ai 借鉴统一任务简报与可编辑的结构化产出：目标、素材来源、脚本草稿、步骤与费用放在同一方案中。
2. 从 Flowise/Dify 借鉴模型与工具的清晰分工、有限迭代、每步可观察结果。
3. 从 n8n 借鉴保留执行记录与失败诊断；以本地持久化支持重复执行，显式区分“返回已有方案”“重新尝试失败步骤”和“发起新请求”。

本期不照搬：可视化搭建器、多 Agent、团队权限、外部营销连接器、在线发布、向量库、完整任务调度平台。它们不是本题验收所必需，三小时内会挤占测试和真实模型验证时间。这是范围取舍，不是竞品缺陷。

公开资料尚未证明五个产品能直接满足本题全部验收点；这不是“产品不支持”的结论，而是说明**候选人仍须交付自己的业务规则与证据**。

## 5. 技术方案建议（供 PRD 与计划采用）

以下均为本项目推论：

- 采用 Python CLI、单 Agent 与本地函数工具。模型适配与离线 Mock 共用动作格式；只接入一个实际可用的模型供应商。
- 模型负责查询、选择方案、调整方案、请求补充或停止；工具负责素材/规则事实、确定性核算与保存结果；最终状态由程序依据实际证据生成。
- 硬约束不能只写在提示词中。执行有成本的动作前核验预算与剩余时间，保存前再次核验方案和结果版本。
- 活动预算、步骤实际消耗、未来执行估算、模型 API 用量分别记录，明确费用表中失败调用是否收费。未经定义的费用不能默认为零。
- 用业务请求标识及输入指纹识别重复；相同请求返回已有结果，标识相同但内容不同明确报冲突。事务或唯一约束承担防重复保存。
- 以真实模型至少两条不同路径证明动态决策；用可控工具失败和离线测试证明状态与核算。Mock 日志单独标注，不能作为真实决策证明。
- 本地 Trace 记录动作、参数、返回摘要、后续动作与简短依据，不要求或保存模型内部思维过程。

原生循环与成熟框架都能实现这些要求。考虑三小时、CLI 交付和本地工具，本期优先原生有限循环；当跨进程恢复、复杂分支或人工暂停成为实际需求时，再评估专用编排框架。

## 6. 来源索引与复核说明

所有来源访问日期均为 2026-10-28。GitHub 默认分支和在线文档会变化；以下结论是该日期可读内容的快照。许可证条目用于准确描述调研对象的代码形态，不构成后续商业授权的完整判断。

| 编号 | 第一方来源 | 本次用途 |
| --- | --- | --- |
| S01 | [Flowise LICENSE.md](https://github.com/FlowiseAI/Flowise/blob/main/LICENSE.md) | 普通代码与企业代码许可范围；读取文件内容 SHA：`68314426eaf58acf21727cdada2d8047797058e7` |
| S02 | [Flowise Agentflow V2](https://docs.flowiseai.com/using-flowise/agentflowv2) | 动态 Agent、固定 Tool、状态、检查点、人工输入、循环上限 |
| S03 | [Flowise Analytic](https://docs.flowiseai.com/using-flowise/analytics) | 逐步 Trace 与外部分析集成 |
| S04 | [Dify LICENSE](https://github.com/langgenius/dify/blob/main/LICENSE) | 修改版 Apache 条款；读取文件内容 SHA：`329ee302875f292fdd11e12e830562d0be9ade1c` |
| S05 | [Dify Agent](https://docs.dify.ai/en/cloud/use-dify/nodes/agent) | 本次依据 Classic Agent 部分，未把 beta New Agent 等同于该节点 |
| S06 | [Dify Handle Errors](https://docs.dify.ai/en/cloud/use-dify/build/predefined-error-handling-logic) | 错误处理模式和适用节点 |
| S07 | [n8n LICENSE.md](https://github.com/n8n-io/n8n/blob/master/LICENSE.md) | SUL 与企业代码例外；读取文件内容 SHA：`f85f59baa906530c26cee26e0c9ddd6bd5f86dbd` |
| S08 | [n8n Tools Agent](https://docs.n8n.io/integrations/builtin/cluster-nodes/root-nodes/n8n-nodes-langchain.agent/tools-agent/) | 工具选择、迭代限制、中间步骤与 Trace 元数据 |
| S09 | [n8n Handle errors gracefully](https://docs.n8n.io/build/flow-logic/handle-errors-gracefully.md) | 错误工作流和错误执行数据 |
| S10 | [n8n Debug executions](https://docs.n8n.io/build/understand-workflows/understand-executions/debug-executions.md) | 历史数据调试/重跑及可用范围 |
| S11 | [Jasper Canvas 帮助文档](https://help.jasper.ai/hc/en-us/articles/55320360614299-Jasper-Canvas) | 项目上下文、资产与协作 |
| S12 | [Jasper Agents 产品页](https://www.jasper.ai/agents) | 多步执行、记忆、迭代与人工控制的产品方声明 |
| S13 | [Jasper Using Agents](https://developers.jasper.ai/docs/using-agents) | 任务发现、结构化输入配置与执行 |
| S14 | [Copy.ai What are Workflows](https://www.copy.ai/platform/what-are-workflows) | Actions、事件触发和可复用流程的产品方声明 |
| S15 | [Copy Agents](https://www.copy.ai/platform/copy-agents) | 约束内局部决策的产品方声明 |
| S16 | [Copy.ai 内容工作流指南](https://www.copy.ai/guides/supercharge-your-content-creation-with-copy-ai-workflows) | 内容检索、提纲、草稿和修改的公开示例；页面原文日期为 2024-03-12，作为流程示例而非最新功能清单 |
| S17 | [Flowise Evaluations](https://docs.flowiseai.com/using-flowise/evaluations) | Cloud/Enterprise 可用范围 |
| S18 | [题面交付示例 README](https://github.com/lwz1357/agent-campus-submission-example/blob/main/README.md) | 目录与离线示例范围；读取文件内容 SHA：`bfc8defad51c2ee0250151644b62a6e442bf923b` |

来源限制：部分搜索结果是论坛或第三方内容，未用作结论；Copy.ai 的旧 `docs.copy.ai` 地址未能读取，改用当前官方产品页与第一方指南；Dify 的旧路径有重定向，引用采用当前官方文档路径。本次不采集价格、市场份额或未经验证的效率提升数据。
