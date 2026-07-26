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

MARKER = "# >>> memory-mcp code-graph hook >>>"
MARKER_END = "# <<< memory-mcp code-graph hook <<<"


def hook_cmd(project_path: str) -> str:
    return f'python3 "{str(HOOK_SCRIPT).replace(chr(92), "/")}" "{project_path}"'


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
        cmd = hook_cmd(str(entry).replace("\\", "/"))

        if post_commit.exists():
            existing = post_commit.read_text(encoding="utf-8")
            if MARKER in existing:
                print(f"  up-to-date: {entry.name}")
                continue
            # Preserve foreign hook content; back it up before appending
            backup = post_commit.with_suffix(".bak")
            backup.write_text(existing, encoding="utf-8")
            if not existing.endswith("\n"):
                existing += "\n"
            post_commit.write_text(existing + f"{MARKER}\n{cmd}\n{MARKER_END}\n", encoding="utf-8")
            print(f"  appended: {entry.name} (backed up to {backup.name})")
        else:
            post_commit.write_text(f"#!/bin/sh\n{MARKER}\n{cmd}\n{MARKER_END}\n", encoding="utf-8")
            print(f"  installed: {entry.name}")
        # Make executable (on Windows this is advisory but consistent)
        current = post_commit.stat().st_mode
        post_commit.chmod(current | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        installed += 1

    print(f"\nDone. {installed} hooks installed, {skipped} skipped.")
    print(f"Hook script: {HOOK_SCRIPT}")
    print(f"Log file: {HOOK_SCRIPT.parent / 'hook_scan.log'}")


if __name__ == "__main__":
    install()
