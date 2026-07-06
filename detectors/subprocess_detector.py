"""Detect subprocess spawn calls that target other projects' scripts."""

import re
from pathlib import Path

# Patterns for subprocess spawn that reference a script path
SPAWN_RE = re.compile(r"""
    (?:spawn|run|exec|Popen|popen)
    \s*\(?               # optional open paren
    \s*[\"']             # open quote
    (?:python|node|powershell|bash|cmd\s*/c)\s+
    (.*?)
    [\"']                # close quote
""", re.VERBOSE | re.IGNORECASE | re.DOTALL)

# Direct file path references that look like a script to spawn
PATH_ARG_RE = re.compile(r"""
    [\"']
    (?:python|node|powershell|bash)
    (?:\.exe)?
    [\"']\s*,\s*[\"']
    (.*?)
    [\"']
""", re.VERBOSE | re.IGNORECASE | re.DOTALL)


def detect(file: Path, content: str, projects: set[str],
           project_roots: dict[str, Path]) -> list[dict]:
    """Find subprocess spawn calls referencing other project scripts.

    Args:
        file: File being scanned.
        content: File text content.
        projects: All known project names.
        project_roots: Map project name → root path.

    Returns:
        List of edge dicts.
    """
    edges: list[dict] = []
    src_project = _infer_project(file)
    if not src_project:
        return edges

    # Collect all script path references from both patterns
    script_paths: list[str] = []
    for m in SPAWN_RE.finditer(content):
        script_paths.append(m.group(1).strip())
    for m in PATH_ARG_RE.finditer(content):
        script_paths.append(m.group(1).strip())

    # Convert Windows paths to forward-slash for matching
    from config import WORKSPACE_ROOT as ws_root
    for script_path in script_paths:
        spath = script_path.replace("\\", "/").strip("\"' ")
        if not spath:
            continue
        # Try to match against known project roots
        for proj_name, proj_root in project_roots.items():
            if proj_name == src_project:
                continue
            proj_str = str(proj_root).replace("\\", "/")
            if proj_str in spath:
                edges.append({
                    "src": {"project": src_project, "file": str(file)},
                    "dst": {"project": proj_name},
                    "kind": "subprocess-spawn",
                    "confidence": "extracted",
                    "detail": script_path.strip()[:200],
                })
                break

    return edges


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
