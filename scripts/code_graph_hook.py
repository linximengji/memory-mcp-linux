#!/usr/bin/env python3
"""Called by git post-commit hook. Triggers single-project code-graph update.

Usage:
    code_graph_hook.py <project_root>

The hook runs silently — output goes to stderr so it doesn't interfere
with git's return channel. On error, it writes to a log file instead of
interrupting the user's commit flow.
"""

import json
import os
import sys
import time
from pathlib import Path

# Resolve paths relative to this script's location
SCRIPT_DIR = Path(__file__).resolve().parent
MCP_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(MCP_ROOT))

LOG_FILE = MCP_ROOT / "scripts" / "hook_scan.log"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(str(LOG_FILE), "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def main() -> None:
    if len(sys.argv) < 2:
        log("ERROR: missing project_root argument")
        sys.exit(1)

    project_root = Path(sys.argv[1])
    if not project_root.is_dir():
        log(f"ERROR: invalid project root: {project_root}")
        sys.exit(1)

    project_name = project_root.name

    try:
        from scan import scan
        result = json.loads(scan(projects_filter=[project_name]))
        edges = result.get("edge_count", 0)
        symbols = result.get("symbol_count", 0)
        changed = result.get("changed_projects", 0)
        elapsed = result.get("elapsed_ms", 0)
        log(f"OK {project_name}: {edges} edges, {symbols} symbols, "
            f"{changed} changed, {elapsed}ms")
    except Exception as e:
        log(f"ERROR {project_name}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
