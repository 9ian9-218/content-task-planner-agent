# 实际执行 Trace

模式：mock；状态：needs_input；缓存：False

| 步骤 | 动作来源及已观察步骤 | 工具和参数 | 返回摘要 | 实际后续执行或终止 | 公开依据 |
| --- | --- | --- | --- | --- | --- |
| 1 | mock_policy / 已观察 None | search_materials {"query": "不存在的合成产品 Z99", "decision_summary": "先获取可引用的素材事实。"} | {"ok": true, "error_code": null, "data": {"materials": [], "profiles": {"standard": "完整的开场、事实说明、操作建议和结尾结构；最终费用须询价", "economy": "精简的事实说明和结尾结构，满足核心交付；最终费用须询价"}}} | 结束：MATERIALS_MISSING | 先获取可引用的素材事实。 |

终止原因：MATERIALS_MISSING。检索没有可用资料，请补充素材或调整主题。
