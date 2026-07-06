"""Detect filesystem reads/writes that cross project boundaries."""

import re
from pathlib import Path

from config import WORKSPACE_ROOT

# Patterns that reference sibling project paths via relative traversal
REL_PATH_RE = re.compile(r"""
    (?:resolve|join|dirname|realpath|joinpath)
    \s*\(
    (.*?)
    \)
""", re.VERBOSE | re.IGNORECASE | re.DOTALL)

# Direct string references to workspace/<project> in code
_WS_NAME = WORKSPACE_ROOT.name
_ws_root_escaped = re.escape(str(WORKSPACE_ROOT).replace('\\', '/'))
ABSOLUTE_PATH_RE = re.compile(
    rf"""
    [\"']
    (?:{_ws_root_escaped})/
    ([^/\\\"'\s)]+)
    (?:/|\\)
""", re.VERBOSE | re.IGNORECASE)

# Also match <ws_name>/<project> patterns in paths (works for both Windows/Linux)
WS_NAME_RE = re.compile(
    rf"""
    [\"']
    (?:/|\\\\|[A-Za-z]:)?[/\\\\]
    {re.escape(_WS_NAME)}
    [/\\\\]
    ([^/\\\"'\s)]+)
    [/\\\\]
""", re.VERBOSE | re.IGNORECASE)

# Reference like '../ops-daemon/data/...' in path arguments
RELATIVE_NAV_RE = re.compile(r"""
    [\"']
    (?:\.\.[/\\]){1,3}
    ([^/\\\"'\s)]+)
""", re.VERBOSE)


def detect(file: Path, content: str, projects: set[str]) -> list[dict]:
    """Find filesystem references to other project directories.

    Args:
        file: File being scanned.
        content: File text content.
        projects: All known project names.

    Returns:
        List of edge dicts.
    """
    edges: list[dict] = []
    src_project = _infer_project(file)
    if not src_project:
        return edges

    # 1. Check explicit WORKSPACE_ROOT/<name> paths
    for m in ABSOLUTE_PATH_RE.finditer(content):
        dst = m.group(1)
        if dst in projects and dst != src_project:
            edges.append({
                "src": {"project": src_project, "file": str(file)},
                "dst": {"project": dst},
                "kind": "filesystem-reads",
                "confidence": "extracted",
                "detail": m.group().strip("\"'"),
            })

    # 2. Check workspace name/<name> patterns
    for m in WS_NAME_RE.finditer(content):
        dst = m.group(1)
        if dst in projects and dst != src_project:
            # Skip if already found by ABSOLUTE_PATH_RE
            if any(e["dst"]["project"] == dst for e in edges):
                continue
            edges.append({
                "src": {"project": src_project, "file": str(file)},
                "dst": {"project": dst},
                "kind": "filesystem-reads",
                "confidence": "extracted",
                "detail": m.group().strip("\"'"),
            })

    # 2. Check relative navigation that lands on another project
    for m in RELATIVE_NAV_RE.finditer(content):
        dst = m.group(1)
        if dst in projects and dst != src_project:
            edges.append({
                "src": {"project": src_project, "file": str(file)},
                "dst": {"project": dst},
                "kind": "filesystem-reads",
                "confidence": "extracted",
                "detail": m.group().strip("\"'"),
            })

    # 3. Check resolve/join calls for cross-project path construction
    for m in REL_PATH_RE.finditer(content):
        args = m.group(1)
        for proj in sorted(projects, key=len, reverse=True):
            if proj != src_project and proj in args:
                edges.append({
                    "src": {"project": src_project, "file": str(file)},
                    "dst": {"project": proj},
                    "kind": "filesystem-reads",
                    "confidence": "inferred",
                    "detail": f"resolve/join referencing '{proj}'",
                })
                break

    return _dedup(edges)


def _infer_project(file: Path) -> str | None:
    """Extract project name from file path under WORKSPACE_ROOT."""
    from config import WORKSPACE_ROOT
    try:
        parts = file.absolute().parts
        ws_parts = WORKSPACE_ROOT.absolute().parts
        if len(parts) > len(ws_parts) and parts[:len(ws_parts)] == ws_parts:
            return parts[len(ws_parts)]
    except Exception:
        pass
    return None


def _dedup(edges: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result = []
    for e in edges:
        key = (e["src"]["project"], e["src"]["file"],
               e["dst"]["project"], e["kind"])
        if key not in seen:
            seen.add(key)
            result.append(e)
    return result
