---
name: preload-deprecation
description: code_graph_preload 废弃，改用 tool 描述自触发 + 惰性刷新
metadata:
  type: architecture
---

## 废弃主动注入（code_graph_preload）

2026-07-02 删除了 `code_graph_preload.py` 及其 hooks 配置（`settings.json` 的 UserPromptSubmit hook）。

**废弃原因**：hook 只在 LLM 推理前看到用户文本，靠 keyword match 判断"是否代码需求"误触率高，注入的快照数据是静态过期切片，浪费 token。

**替代方案**：
- MCP tool 的 `description` 用 `[Trigger: ...]` 前缀写明触发条件，LLM 在推理上下文里自己判断是否调用
- `query_symbols` / `trace_impact` 内部加惰性刷新，调用时检查数据新鲜度，过期自动增量扫描
- 不再需要 hook 替 LLM 做"该不该用"的决策，tool 描述本身就是最佳的触发信号
