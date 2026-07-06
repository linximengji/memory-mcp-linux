"""Memory MCP server — namespace/segment directory tree."""
import json, os, re, subprocess, time
from pathlib import Path
from mcp.server.fastmcp import FastMCP

from _storage import (
    memory_store, memory_update, memory_recall, memory_get, memory_list,
    memory_delete, memory_rebuild_index, memory_review, memory_cleanup,
    memory_upsert, memory_snapshot,
    format_recall_text,
    VALID_SEGMENTS, MEMORY_ROOT,
)
from scan import scan, trace_impact, query_symbols

mcp = FastMCP("memory-mcp-server")

_SCHEMA_NOTE = (
    "namespace is the module/project name, lowercase kebab-case (e.g. model-proxy, claudetalk, presenton). "
    "segment is one of: _index|overview|deps|design|sop. "
    "name should contain search keywords — it's the primary recall surface. "
    "BEFORE storing: call memory_recall to check for existing memories in the same namespace. "
    "If any existing memory already covers this topic, use memory_update to append. "
    "Prefer merging into existing namespace segments over creating new namespaces. "
    "Use [[wiki-link]] syntax for cross-namespace references."
)


@mcp.tool(name="memory_store", description=_SCHEMA_NOTE)
def memory_store_wrapper(
    name: str,
    content: str,
    namespace: str = "",
    segment: str = "_index",
) -> str:
    """Store a new memory. BEFORE calling: use memory_recall to check for existing memories in the same namespace."""
    ns = namespace if namespace else None
    return memory_store(name, content, ns, segment)


@mcp.tool(name="memory_update")
def memory_update_wrapper(
    name: str,
    content: str | None = None,
    namespace: str | None = None,
) -> str:
    """Update an existing memory. Only provided fields are changed. Use namespace to disambiguate name collisions."""
    return memory_update(name, content, namespace=namespace)


@mcp.tool(name="memory_recall")
def memory_recall_wrapper(
    query: str = "",
    namespace: str | None = None,
    top_k: int = 5,
) -> str:
    """Search memories by query. Pass namespace to narrow search to one module (e.g. 'claudetalk'). Returns top_k results with scores and segments."""
    return format_recall_text(memory_recall(query, namespace, top_k))


@mcp.tool(name="memory_get")
def memory_get_wrapper(name: str, namespace: str | None = None) -> str:
    """Read a specific memory. Use 'namespace/segment' format (e.g. 'claudetalk/architecture') or pass namespace separately. Use 'namespace' alone to get its _index.md."""
    return memory_get(name, namespace)


@mcp.tool(name="memory_list")
def memory_list_wrapper(namespace: str | None = None) -> str:
    """List all memory namespaces and their segments. Pass namespace to see one module's segments."""
    return memory_list(namespace)


@mcp.tool(name="memory_delete")
def memory_delete_wrapper(name: str, namespace: str | None = None) -> str:
    """Delete a memory by name. Pass namespace to disambiguate."""
    return memory_delete(name, namespace)


@mcp.tool(name="memory_rebuild_index")
def memory_rebuild_index_wrapper() -> str:
    """Rebuild MEMORY.md index from all memory files."""
    return memory_rebuild_index()


@mcp.tool(name="memory_review")
def memory_review_wrapper(action: str, name: str = "", namespace: str | None = None) -> str:
    """Apply, confirm, or dismiss suggestions from the review report. action: 'list', 'confirm', 'dismiss', 'dismiss-all'."""
    return memory_review(action, name, namespace)


@mcp.tool(name="memory_upsert")
def memory_upsert_wrapper(
    name: str,
    content: str,
    namespace: str = "",
    segment: str = "_index",
) -> str:
    """Insert or update a memory. If similar content exists, updates it; otherwise creates new."""
    ns = namespace if namespace else None
    return memory_upsert(name, content, ns, segment)


@mcp.tool(name="memory_snapshot")
def memory_snapshot_wrapper() -> str:
    """Return runtime memory coverage snapshot (counts by namespace/segment)."""
    return json.dumps(memory_snapshot(), ensure_ascii=False, indent=2)


@mcp.tool(name="memory_cleanup")
def memory_cleanup_wrapper(action: str = "list", names: list[str] | None = None) -> str:
    """List or delete low-quality memories. action='list' shows candidates, action='delete' removes them."""
    return memory_cleanup(action, names)




@mcp.tool(name="memory_code_scan")
def memory_code_scan_wrapper(projects_filter: list[str] | None = None) -> str:
    """[Trigger: 首次对话 / 怀疑代码图过期 / 刚改了文件结构] 扫描工作区项目,检测跨项目运行时依赖(HTTP/subprocess/fs/import)。增量扫描优先,只扫 git diff 有改动的文件。不传 projects_filter 则扫全量。返回 JSON 摘要(项目数、变更数、边数)。"""
    return scan(projects_filter)


@mcp.tool(name="memory_trace_impact")
def memory_trace_impact_wrapper(target: str, depth: int = 3) -> str:
    """[Trigger: 准备改函数签名/API接口/文件路径/配置字段前, 想了解改动波及范围] BFS 影响链追踪。传入目标文件路径(绝对或项目相对),返回所有下游依赖方(项目+文件+距离)。内部自动检查代码图新鲜度,过时则增量扫描。距离1=高风险,2=中,3+=低。"""
    return trace_impact(target, depth)


@mcp.tool(name="memory_code_query")
def memory_code_query_wrapper(query: str, type: str = "symbol") -> str:
    """[Trigger: 搜函数/类/变量定义、查谁在调用、找文件的导出符号列表] 查询代码图符号索引。type=symbol→搜定义位置(自动FTS5); type=consumer→查谁调用了该符号; type=exporter→列出文件的所有导出符号。内部自动检查代码图新鲜度。"""
    return query_symbols(query, type)


if __name__ == "__main__":
    mcp.run(transport="stdio")
