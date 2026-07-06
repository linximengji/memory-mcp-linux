#!/usr/bin/env python3
"""Install post-commit hooks into git repos under WORKSPACE_ROOT.

Usage:
    python scripts/install-hooks.py

Rerun after cloning new projects. Skips memory-mcp itself.
"""

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import WORKSPACE_ROOT as WORKSPACE

HOOK_SCRIPT = WORKSPACE / "memory-mcp" / "scripts" / "code_graph_hook.py"

# Skip these directories (no hooks needed)
SKIP = frozenset({"memory-mcp", "node_modules", ".git", "__pycache__"})

POST_COMMIT_TEMPLATE = """#!/bin/sh
# Auto-update memory-mcp code-graph after commit
python3 "{}" "{}"
""".format(str(HOOK_SCRIPT).replace("\\", "/"), "{}")


def install() -> None:
    if not HOOK_SCRIPT.exists():
        print(f"ERROR: hook script not found at {HOOK_SCRIPT}")
        sys.exit(1)

    installed = 0
    skipped = 0

    for entry in sorted(WORKSPACE.iterdir()):
        if not entry.is_dir() or entry.name in SKIP or entry.name.startswith("."):
            continue

        git_dir = entry / ".git"
        if not git_dir.exists():
            continue

        # Determine hooks directory (regular git or worktree)
        if git_dir.is_file():
            # Worktree — read the gitdir pointer
            try:
                content = git_dir.read_text(encoding="utf-8")
                if "gitdir:" in content:
                    actual = content.split("gitdir:")[1].strip()
                    hooks_dir = Path(actual) / "hooks"
                else:
                    skipped += 1
                    continue
            except Exception:
                skipped += 1
                continue
        else:
            hooks_dir = entry / ".git" / "hooks"

        if not hooks_dir.exists():
            hooks_dir.mkdir(parents=True, exist_ok=True)

        post_commit = hooks_dir / "post-commit"
        hook_content = POST_COMMIT_TEMPLATE.format(
            str(entry).replace("\\", "/")
        )

        post_commit.write_text(hook_content, encoding="utf-8")
        # Make executable (on Windows this is advisory but consistent)
        current = post_commit.stat().st_mode
        post_commit.chmod(current | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        print(f"  installed: {entry.name}")
        installed += 1

    print(f"\nDone. {installed} hooks installed, {skipped} skipped.")
    print(f"Hook script: {HOOK_SCRIPT}")
    print(f"Log file: {HOOK_SCRIPT.parent / 'hook_scan.log'}")


if __name__ == "__main__":
    install()
