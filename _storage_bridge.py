"""JSON-line bridge: stdin → _storage function → stdout. Long-running subprocess for TS MCP server."""
import sys, json, traceback

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from _storage import (
    memory_store, memory_update, memory_recall, memory_get, memory_list,
    memory_delete, memory_rebuild_index, memory_review, memory_cleanup,
    memory_upsert, memory_snapshot,
    format_recall_text,
)

from scan import scan, trace_impact, query_symbols
from _health import run_memory_health


FUNCTIONS = {
    "memory_store": memory_store,
    "memory_update": memory_update,
    "memory_recall": lambda query="", namespace=None, top_k=5: format_recall_text(memory_recall(query, namespace, top_k)),
    "memory_get": memory_get,
    "memory_list": memory_list,
    "memory_delete": memory_delete,
    "memory_rebuild_index": memory_rebuild_index,
    "memory_review": memory_review,
    "memory_cleanup": memory_cleanup,
    "memory_upsert": memory_upsert,
    "memory_snapshot": memory_snapshot,
    "memory_code_scan": scan,
    "memory_trace_impact": trace_impact,
    "memory_code_query": query_symbols,
    "health_check": run_memory_health,
}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _respond(False, error=f"invalid JSON: {e}")
            continue

        method = req.get("method", "")
        params = req.get("params", {})

        if method in FUNCTIONS:
            fn = lambda: FUNCTIONS[method](**params)
        else:
            _respond(False, error=f"unknown method: {method}")
            continue

        try:
            result = fn()
            _respond(True, result=result)
        except Exception as e:
            _respond(False, error=f"{type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stderr)


def _respond(ok: bool, result: str = "", error: str = ""):
    sys.stdout.write(json.dumps({"ok": ok, "result": result, "error": error}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
