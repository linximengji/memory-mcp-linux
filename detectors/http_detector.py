"""Detect cross-project HTTP API calls by grepping URL patterns."""

import re
from pathlib import Path

# Known ports → project mapping for local services
PORT_PROJECT_MAP: dict[int, str] = {
    4000: "proxy",
    4001: "proxy",
    9877: "claudetalk",
    9292: "pact-broker",
    8765: "ops-dashboard",
}

# Known localhost URL patterns that indicate cross-project HTTP calls
LOCALHOST_RE = re.compile(r"""
    (?:localhost|127\.0\.0\.1|0\.0\.0\.0)
    :(\d{4})
""", re.VERBOSE | re.IGNORECASE)

# Patterns that reference a project by name in an HTTP URL,
# e.g. http://proxy:4000 or a config string "proxy:4000"
PROJECT_URL_RE = re.compile(r"""
    https?://
    ([a-z][-a-z0-9]+)   # project name
    (?::\d{4})?
    /v\d/                # API version path
""", re.VERBOSE | re.IGNORECASE)


def detect(file: Path, content: str, projects: set[str]) -> list[dict]:
    """Find HTTP calls to other projects in `content`.

    Args:
        file: File being scanned (for src metadata).
        content: File text content.
        projects: All known project names in the workspace.

    Returns:
        List of edge dicts.
    """
    edges: list[dict] = []
    src_project = _infer_project(file)
    if not src_project:
        return edges

    # Match localhost:port patterns
    for m in LOCALHOST_RE.finditer(content):
        port = int(m.group(1))
        dst_project = PORT_PROJECT_MAP.get(port)
        if dst_project and dst_project != src_project:
            edges.append(_edge(src_project, str(file), dst_project,
                               "http-calls", f"localhost:{port}"))
            continue
        # Port doesn't match known projects — try to find project name near the URL
        line_start = max(0, m.start() - 60)
        line_end = min(len(content), m.end() + 60)
        context = content[line_start:line_end]
        for proj in sorted(projects, key=len, reverse=True):
            if proj != src_project and proj in context and proj not in ("bin", "temp", "node_modules"):
                edges.append(_edge(src_project, str(file), proj,
                                   "http-calls", f"port {port}, near '{proj}'"))
                break

    # Match project-name in URL patterns
    for m in PROJECT_URL_RE.finditer(content):
        dst = m.group(1).lower()
        if dst in projects and dst != src_project:
            edges.append(_edge(src_project, str(file), dst,
                               "http-calls", m.group()))

    # Dedup by (src project, dst project, kind)
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


def _edge(src: str, src_file: str, dst: str, kind: str, detail: str) -> dict:
    return {
        "src": {"project": src, "file": src_file},
        "dst": {"project": dst},
        "kind": kind,
        "confidence": "extracted",
        "detail": detail,
    }


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
