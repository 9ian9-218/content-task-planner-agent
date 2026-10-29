# 第2题：内容任务规划 Agent

帮助运营人员在预算和截止时间内形成方案。输入主题、预算、截止时间；输出任务计划、步骤状态、工具结果、模拟费用、已保存方案或未完成原因。仅保存方案，不执行活动或发布。

## 评审顺序

1. [题面逐项验收](docs/16-acceptance-and-submission.md)：每条要求对应输入、实际输出与Trace。
2. [实际评估](docs/03-evaluation.md)与[质量评分](docs/20-current-quality-assessment.md)：当前结果、历史结果和未通过项分开记录。
3. [AI使用](docs/04-ai-usage.md)与[个人参与](docs/17-candidate-participation.md)：如实区分AI生成、自动执行、人类决策和待完成的独立复现。

## 最短离线运行

在项目根目录，使用Python 3.14；Mock无需任何配置，也不会访问模型。固定解释器安装方法见[README](README.md)。

```bash
PYTHONPATH=src python3 -m content_planner run --input examples/cases/C01-normal/input.json --mode mock --as-of 2026-11-01T10:00:00+08:00 --db .local/review.sqlite3 --output-dir .local/review
PYTHONPATH=src python3 -m content_planner run --input examples/cases/C04-replan-budget/input.json --scenario data/scenarios/over-budget.json --mode mock --as-of 2026-11-01T10:00:00+08:00 --db .local/replan-review.sqlite3 --output-dir .local/replan-review
python3 tools/verify.py
```

重复第一条应复用同一份方案，新增模拟费用和模型/工具动作均为0。退出码0=完成，2=输入/配置问题或待补充，3=未完成，4=导出失败。完成仅表示有保存回执。

## 核心实例

| 场景 | 查看内容 |
| --- | --- |
| 正常 | [实际输出](examples/mock/C01-normal/output.json)、[Trace](examples/mock/C01-normal/trace.md) |
| 信息不足 | [缺预算](examples/mock/C02a-missing-budget/output.json)、[缺截止时间](examples/mock/C02b-missing-deadline/output.json) |
| 工具失败 | [保存失败输出](examples/mock/C03-save-failure/output.json)、[Trace](examples/mock/C03-save-failure/trace.md) |
| 预算重规划 | [12→8的实际Trace](examples/live/C04-replan-budget/trace.md)，模型及版本以对应manifest为准 |
| 重复与续跑 | [重复](examples/mock/C06-repeat/output.json)、[续跑](examples/mock/C14-resume/output.json) |

## 模型与边界

当前用户授权接入`http://127.0.0.1:4000/v1`的`wb/hy3`。填写被忽略的`.env.local`或使用环境变量，不提交真实密钥。离线Mock只证明流程，真实模型记录单独标记；历史Opus结果不混记为当前模型结果。真实批次设请求上限，模型不可用时明确未完成，不伪造决策或保存成功。

资料、费用和业务耗时都是合成值；12→8不是实际现金节约。5分钟本地压力检查不证明长期SLA、真实远端副作用对账或运营收益。详见[评估报告](docs/03-evaluation.md)与[操作手册](docs/14-operations.md)。

## 提交信息

- 题号：2。
- GitHub URL：[https://github.com/9ian9-218/content-task-planner-agent](https://github.com/9ian9-218/content-task-planner-agent)，已核实为公开仓库；元数据见[submission.json](submission.json)。
- 最终commit SHA：在实际提交仓库运行`git rev-parse HEAD`后填入考核平台，不使用源码摘要代替。
- 个人实际工时：待据实登记；一天半/12小时为计划，不冒充实际投入。
- 已知未完成与验证受限项：以最新[交付检查](examples/delivery-check.json)为准。
