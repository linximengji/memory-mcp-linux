# 代码图 (code-graph) 架构

## 功能切面映射

| 切面 | 入口 | 文件 | 关键函数 | 备注 |
|------|------|------|----------|------|
| 依赖扫描 | MCP 工具/scan | `scan.py` | `scan()` | git rev 跟踪增量扫描 |
| 影响追踪 | MCP 工具/trigger | `scan.py` | `trace_impact()` | BFS, 全边表加载 |
| 符号查询 | MCP 工具/query | `scan.py` | `query_symbols()` | FTS5, fallback LIKE |
| 新鲜度保证 | 三入口通用 | `scan.py` | `_ensure_fresh()` | 返回 fresh/partial/unknown |
| 脏文件检测 | 三入口通用 | `scan.py` | `_detect_dirty_files()` | git diff HEAD + untracked |
| DB 层 | 存储 | `db.py` | 全部 | SQLite WAL-mode |
| 服务包装 | MCP 服务器 | `server.py` | wrapper 函数 | 参数转发 + 返回值传递 |

## 入口文件

- MCP Server: `server.py` — 注册 `memory_code_scan/trace_impact/query` 三个工具
- Bridge: `_storage_bridge.py` — JSON-line stdin/stdout，封装相同函数签名
- git hook: `scripts/code_graph_hook.py` — post-commit 触发增量扫描

## 跨文件协议

- `_ensure_fresh()` 返回 dict: `{freshness, last_scanned, dirty_projects, dirty_files, hint}`
- freshness 等级: `fresh`(有扫描且无脏文件) / `partial`(过期或有脏文件) / `unknown`(从未扫描或锁定中)
- 每条结果带 `certainty` 字段: `matched` / `overlay-pending`
- 结果顶层带 `hint` 自然语言建议

## 数据流

```
Agent/MCP → trace_impact()
  → _ensure_fresh()  // 检测脏文件 + 判定新鲜度，必要时触发扫描
  → BFS 遍历 edges 表 → 每条 impacted 带 certainty & hint
  → JSON 返回 {freshness, impacted, hint}

Agent/MCP → query_symbols()
  → _ensure_fresh()
  → FTS5/LIKE/consumer 查询 → 每个结果带 certainty
  → JSON 返回 {freshness, results, hint}
```

## 设计约束

- overlay 不合成边/符号表，只标注脏文件存在（FTS5 不支持虚拟 overlay）
- `_ensure_fresh()` 非阻塞锁，冷启动扫全量，后续增量
- git post-commit hook 是加速器，非可靠保证
- `trace_impact()` 全边表加载已在 Phase 2 计划中改为基于索引的 BFS
