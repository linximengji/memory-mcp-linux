"""Code-graph scanner: walk workspace, detect cross-project dependencies, build edges.

SQLite-backed storage for atomic per-project updates and fast queries.
"""

import concurrent.futures
import functools
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from config import WORKSPACE_ROOT

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
SCAN_WORKERS = 4
STALE_MINUTES = 30  # re-scan if last scan was this long ago
_scan_lock = threading.Lock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from detectors import http_detector, subprocess_detector, filesystem_detector, import_detector

SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "dist", ".git", ".claude",
    "temp", "venv", ".venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "bower_components", ".worktree",
})

SKIP_EXT = frozenset({
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib",
    ".exe", ".bin", ".whl", ".egg", ".egg-info",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".bz2",
    ".db", ".sqlite", ".sqlite3",
    ".lock", ".sum",
    ".bak", ".orig",
})

SOURCE_EXT = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def walk_projects() -> dict[str, dict]:
    """Discover projects and their source files.

    Returns:
        {project_name: {"root": str, "files": {rel_path: hash}}}
    """
    projects: dict[str, Any] = {}

    for entry in sorted(WORKSPACE_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or name.startswith("_") or name in SKIP_DIRS:
            continue
        source_files = _find_source_files(entry)
        if not source_files:
            continue
        projects[name] = {
            "root": str(entry),
            "files": source_files,
        }

    return projects


def _find_source_files(root: Path) -> dict[str, str]:
    """Find all source files in a project, return {rel_path: ""} (hashes filled lazily)."""
    result: dict[str, str] = {}
    try:
        for path in root.rglob("*"):
            parts = set(path.relative_to(root).parts)
            if SKIP_DIRS & parts:
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() in SKIP_EXT:
                continue
            if path.suffix.lower() in SOURCE_EXT:
                result[str(path.relative_to(root))] = ""
    except PermissionError:
        pass
    return result


def _fill_hashes(root: Path, files: dict[str, str]) -> dict[str, str]:
    """Compute sha256 for each file (hash-fallback change detection only)."""
    result: dict[str, str] = {}
    for rel in files:
        try:
            result[rel] = file_hash(root / rel)
        except Exception:
            pass
    return result


def _git_rev(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            rev = result.stdout.strip()
            if rev:
                return rev
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _git_files(project_root: Path, base: str | None) -> set[str]:
    """Changed source files: committed since *base* + uncommitted (diff HEAD) + untracked."""
    changed: set[str] = set()

    def _add(output: str) -> None:
        for line in output.strip().split("\n"):
            line = line.strip()
            if line and any(line.endswith(ext) for ext in SOURCE_EXT):
                changed.add(line)

    try:
        if base:
            r = subprocess.run(
                ["git", "-C", str(project_root), "diff", "--name-only", base, "HEAD"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                _add(r.stdout)
        r = subprocess.run(
            ["git", "-C", str(project_root), "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            _add(r.stdout)
        r = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            _add(r.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return changed


def _lang_for_ext(ext: str) -> str:
    return {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".jsx": "jsx",
        ".mjs": "javascript",
        ".cjs": "javascript",
    }.get(ext.lower(), "unknown")


def filter_source_files(
    projects: dict[str, dict],
    changed_projects: set[str] | None = None,
    git_files: dict[str, set[str]] | None = None,
) -> list[tuple[str, str, str, Path]]:
    """Collect (project_name, rel_path, file_path, lang) for files to scan.

    When git_files is given, only include explicitly changed files.
    """
    result: list[tuple[str, str, str, Path]] = []
    for pname, pinfo in projects.items():
        if changed_projects is not None and pname not in changed_projects:
            continue
        root = Path(pinfo["root"])
        specific = None
        if git_files and pname in git_files:
            specific = {f for f in pinfo["files"] if f in git_files[pname]}
        files_to_iter = specific if specific is not None else pinfo["files"]
        for rel_path in files_to_iter:
            fpath = root / rel_path
            lang = _lang_for_ext(fpath.suffix)
            result.append((pname, rel_path, str(fpath), lang))
    return result


def scan(projects_filter: list[str] | None = None, **kwargs) -> str:
    """Main scan entry point.

    Args:
        projects_filter: Optional list of project names to scan (subset).

    Returns:
        JSON string with summary stats.
    """
    db.init_db()
    db.migrate_from_json()
    start = time.time()

    all_projects = walk_projects()

    if projects_filter:
        valid = [p for p in projects_filter if p in all_projects]
        all_projects = {p: all_projects[p] for p in valid}

    project_roots: dict[str, Path] = {
        pname: Path(pinfo["root"]) for pname, pinfo in all_projects.items()
    }
    project_names: set[str] = set(all_projects)

    changed_projects: set[str] = set()
    git_files: dict[str, set[str]] = {}
    rev_to_save: dict[str, str] = {}
    prev_state = db.load_projects()
    for pname, pinfo in all_projects.items():
        root = project_roots[pname]
        curr_rev = _git_rev(root)
        if curr_rev is None:
            # Not a git repo: hash fallback (only branch that computes hashes)
            curr_hashes = _fill_hashes(root, pinfo["files"])
            pinfo["files"] = curr_hashes
            if prev_state.get(pname, {}).get("files", {}) != curr_hashes:
                changed_projects.add(pname)
            continue
        last_rev = db.get_scan_state(pname)
        if last_rev is None:
            # First scan for this project: scan all files
            changed_projects.add(pname)
            git_files[pname] = set(pinfo["files"])
            rev_to_save[pname] = curr_rev
            continue
        gfiles = _git_files(root, last_rev)
        if not gfiles:
            db.set_scan_state(pname, curr_rev)
            continue
        changed_projects.add(pname)
        git_files[pname] = gfiles
        rev_to_save[pname] = curr_rev

    if not changed_projects:
        elapsed = time.time() - start
        return json.dumps({
            "status": "ok",
            "project_count": len(all_projects),
            "file_count": sum(len(p["files"]) for p in all_projects.values()),
            "changed_projects": 0,
            "edge_count": db.get_edge_count(),
            "symbol_count": db.get_symbol_count(),
            "elapsed_ms": round(elapsed * 1000),
            "message": "no changes detected",
        })

    source_files = filter_source_files(all_projects, changed_projects, git_files)

    # Per-project accumulation
    project_edges: dict[str, list[dict]] = {p: [] for p in changed_projects}
    project_symbols: dict[str, list[dict]] = {p: [] for p in changed_projects}
    _lock = threading.Lock()

    def _scan_file(pname: str, fpath: str) -> None:
        """Scan a single file: read, detect detectors, extract symbols."""
        file_path = Path(fpath)
        # File size guard
        try:
            if file_path.stat().st_size > MAX_FILE_SIZE:
                return
        except OSError:
            return
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        edges = []
        edges.extend(http_detector.detect(file_path, content, project_names))
        edges.extend(subprocess_detector.detect(file_path, content, project_names, project_roots))
        edges.extend(filesystem_detector.detect(file_path, content, project_names))
        imported = import_detector.detect_imports(file_path, content)
        edges.extend(
            import_detector.resolve_imports_to_edges(
                imported, str(file_path), pname, project_names, project_roots
            )
        )
        symbols = import_detector.extract_symbols(file_path, content)

        with _lock:
            project_edges[pname].extend(edges)
            project_symbols[pname].extend(symbols)

    def _scan_file_safe(pname: str, fpath: str) -> None:
        try:
            _scan_file(pname, fpath)
        except Exception:
            pass  # individual file errors are non-fatal

    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        fut_to_key = {
            executor.submit(_scan_file_safe, pname, fpath): (pname, fpath)
            for pname, _rel_path, fpath, _lang in source_files
        }
        for fut in concurrent.futures.as_completed(fut_to_key, timeout=300):
            try:
                fut.result()
            except Exception:
                pass  # per-file timeout or error is non-fatal

    # Atomic per-project storage (only update changed projects)
    for pname in changed_projects:
        db.replace_project_edges(pname, project_edges.get(pname, []))
        db.replace_project_symbols(pname, project_symbols.get(pname, []))

    # Persist file hashes for changed projects only, without losing others
    db.upsert_projects(all_projects, changed_projects)
    for pname, rev in rev_to_save.items():
        db.set_scan_state(pname, rev)

    elapsed = time.time() - start
    summary: dict[str, Any] = {
        "status": "ok",
        "project_count": len(all_projects),
        "file_count": sum(len(p["files"]) for p in all_projects.values()),
        "changed_projects": len(changed_projects),
        "edge_count": db.get_edge_count(),
        "symbol_count": db.get_symbol_count(),
        "elapsed_ms": round(elapsed * 1000),
        "changed_list": sorted(changed_projects),
    }
    if projects_filter:
        missing = [p for p in projects_filter if p not in all_projects]
        if missing:
            summary["missing_projects"] = missing
    return json.dumps(summary)


def _is_stale() -> bool:
    """Check if code-graph data is empty or older than STALE_MINUTES."""
    if db.get_edge_count() == 0 and db.get_symbol_count() == 0:
        return True
    last = db.get_last_scanned()
    if not last:
        return True
    try:
        parsed = time.strptime(last, "%Y-%m-%dT%H:%M:%S")
        elapsed = time.time() - time.mktime(parsed)
        return elapsed > STALE_MINUTES * 60
    except (ValueError, OSError):
        return True


def _detect_dirty_files() -> dict[str, list[str]]:
    """Find uncommitted source files per project.

    Returns {project_name: [rel_path, ...]} — only projects that have dirty files.
    """
    dirty: dict[str, list[str]] = {}
    for pname in walk_projects():
        root = Path(db.load_projects().get(pname, {}) .get("root", "")) if pname else Path("")
        if not root.exists():
            continue
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                continue
            files = [f for f in r.stdout.strip().split("\n") if f]
            files = [f for f in files if any(f.endswith(ext) for ext in SOURCE_EXT)]
            # also check untracked
            r2 = subprocess.run(
                ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
                capture_output=True, text=True, timeout=30,
            )
            if r2.returncode == 0:
                files += [f for f in r2.stdout.strip().split("\n") if f
                          and any(f.endswith(ext) for ext in SOURCE_EXT)]
            if files:
                dirty[pname] = sorted(set(files))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return dirty


def _freshness_grade(last_scanned: str | None, dirty_count: int) -> str:
    """Return freshness rating: 'fresh' | 'partial' | 'unknown'.

    - fresh: scanned within STALE_MINUTES, no dirty files
    - partial: scanned but stale or has dirty files
    - unknown: never scanned
    """
    if not last_scanned:
        return "unknown"
    try:
        parsed = time.strptime(last_scanned, "%Y-%m-%dT%H:%M:%S")
        elapsed = time.time() - time.mktime(parsed)
        if elapsed > STALE_MINUTES * 60:
            return "partial"
    except (ValueError, OSError):
        return "unknown"
    if dirty_count > 0:
        return "partial"
    return "fresh"


def _ensure_fresh() -> dict:
    """Lazy re-scan if stale, return freshness info.

    Returns dict with freshness grade, last_scanned, and dirty file count.
    """
    dirty = _detect_dirty_files()
    dirty_count = sum(len(fs) for fs in dirty.values())
    last_scanned = db.get_last_scanned()
    grade = _freshness_grade(last_scanned, dirty_count)

    if grade != "unknown":
        return {
            "freshness": grade,
            "last_scanned": last_scanned,
            "dirty_projects": len(dirty),
            "dirty_files": dirty_count,
            "hint": "",
        }

    # unknown: trigger scan
    if not _scan_lock.acquire(blocking=False):
        return {
            "freshness": "unknown",
            "last_scanned": None,
            "dirty_projects": len(dirty),
            "dirty_files": dirty_count,
            "hint": "code graph is being refreshed by another caller; retry in a moment",
        }
    try:
        if _is_stale():
            scan()
    finally:
        _scan_lock.release()

    last_scanned = db.get_last_scanned()
    dirty = _detect_dirty_files()
    dirty_count = sum(len(fs) for fs in dirty.values())
    grade = _freshness_grade(last_scanned, dirty_count)
    return {
        "freshness": grade,
        "last_scanned": last_scanned,
        "dirty_projects": len(dirty),
        "dirty_files": dirty_count,
        "hint": "",
    }


def trace_impact(target_file: str, max_depth: int = 3, **kwargs) -> str:
    """BFS impact analysis: find all nodes affected by a change to target_file.

    Traverses at project, file, and symbol levels using SQLite-backed edge queries.

    Args:
        target_file: File path to trace (project-relative or absolute).
        max_depth: BFS search depth.
        min_risk: Optional minimum risk level filter ("low", "medium", "high").

    Returns:
        JSON string with impact report including freshness metadata.
    """
    freshness = _ensure_fresh()
    min_risk = kwargs.get("min_risk")
    edges = db.get_all_edges()
    if not edges:
        return json.dumps({
            "freshness": freshness,
            "seed": target_file,
            "impacted": [],
            "summary": "no edges loaded",
        })

    known_projects = {e.get("src_project", "") for e in edges if e.get("src_project")}
    known_projects |= {e.get("dst_project", "") for e in edges if e.get("dst_project")}

    target_project = _infer_project_from_path(target_file, known_projects)

    seeds: list[tuple[str, int, list[str]]] = []
    if target_project:
        seeds.append((f"proj:{target_project}", 0, []))
    seeds.append((f"file:{target_file}", 0, []))

    exported_symbols = db.get_symbols_in_file(target_file)
    for sym in exported_symbols:
        seeds.append((f"sym:{sym['name']}", 0, []))

    visited: set[str] = set()
    impacted_seen: set[str] = set()
    queue: deque[tuple[str, int, list[str]]] = deque(seeds)
    impacted: list[dict] = []

    has_dirty = freshness.get("dirty_files", 0) > 0
    certainty_base = "overlay-pending" if has_dirty else "matched"

    while queue:
        current, depth, path = queue.popleft()
        if current in visited or depth >= max_depth:
            continue
        visited.add(current)

        for e in edges:
            dst_p = e.get("dst_project", "")
            dst_f = e.get("dst_file", "")
            dst_s = e.get("dst_symbol", "")
            src_p = e.get("src_project", "")
            src_f = e.get("src_file", "")
            kind = e.get("kind", "")

            if current.startswith("proj:") and dst_p == current[5:]:
                key = f"file:{src_f}" if src_f else f"proj:{src_p}"
                if key not in visited and key not in impacted_seen:
                    impacted_seen.add(key)
                    new_path = path + [kind]
                    impacted.append({
                        "node": {"project": src_p, "file": src_f},
                        "distance": depth + 1,
                        "risk": _risk_score(depth + 1, src_f),
                        "certainty": certainty_base,
                        "path": new_path,
                        "confidence": e.get("confidence", "extracted"),
                    })
                    queue.append((key, depth + 1, new_path))

            elif current.startswith("file:") and dst_f and (
                db._normalize_path(current[5:]) == db._normalize_path(dst_f)
            ):
                key = f"file:{src_f}" if src_f else f"proj:{src_p}"
                if key not in visited and key not in impacted_seen:
                    impacted_seen.add(key)
                    new_path = path + [kind]
                    impacted.append({
                        "node": {"project": src_p, "file": src_f},
                        "distance": depth + 1,
                        "risk": _risk_score(depth + 1, src_f),
                        "certainty": certainty_base,
                        "path": new_path,
                        "confidence": e.get("confidence", "extracted"),
                    })
                    queue.append((key, depth + 1, new_path))

            elif current.startswith("sym:") and dst_s == current[4:]:
                key = f"file:{src_f}" if src_f else f"proj:{src_p}"
                if key not in visited and key not in impacted_seen:
                    impacted_seen.add(key)
                    impacted.append({
                        "node": {"project": src_p, "file": src_f, "symbol": dst_s},
                        "distance": depth + 1,
                        "risk": _risk_score(depth + 1, src_f),
                        "certainty": certainty_base,
                        "path": path + [kind],
                        "confidence": e.get("confidence", "extracted"),
                    })
                    queue.append((key, depth + 1, path + [kind]))

    # Apply min_risk filter if specified
    risk_order = {"low": 0, "medium": 1, "high": 2}
    if min_risk and min_risk in risk_order:
        threshold = risk_order[min_risk]
        impacted = [i for i in impacted if risk_order.get(i.get("risk", "low"), 0) >= threshold]

    hint = ""
    if freshness.get("freshness") == "partial":
        dirty_projects = freshness.get("dirty_projects", 0)
        if dirty_projects > 0:
            hint = (f"Analysis based on committed code. {dirty_projects} project(s) have "
                    f"uncommitted changes not reflected in edges/symbols. "
                    f"Consider committing and re-running for full results.")

    return json.dumps({
        "freshness": freshness,
        "seed": target_file,
        "target_project": target_project or "unknown",
        "exported_symbols": [{"name": s["name"], "kind": s["kind"]} for s in exported_symbols],
        "impacted": impacted,
        "summary": f"{len(impacted)} affected nodes across "
                   f"{len({i['node']['project'] for i in impacted if i['node']['project']})} projects",
        "hint": hint,
    }, indent=2, ensure_ascii=False)


def _risk_score(distance: int, file_path: str) -> str:
    if distance == 1:
        return "high"
    elif distance == 2:
        return "medium"
    return "low"


def _infer_project_from_path(path: str, known_projects: set[str] | None = None) -> str | None:
    raw = path.replace("\\", "/")
    ws_str = str(WORKSPACE_ROOT).replace("\\", "/") + "/"
    idx = raw.find(ws_str)
    if idx >= 0:
        rest = raw[idx + len(ws_str):]
        proj = rest.split("/")[0]
        return proj if proj else None
    if known_projects:
        first = raw.split("/")[0]
        if first in known_projects:
            return first
    return None


def query_symbols(query: str, query_type: str = "symbol", **kwargs) -> str:
    """Search code-graph symbols using FTS5 (fallback to LIKE).

    Args:
        query: Search term.
        query_type: "symbol" | "consumer" | "exporter"
        fuzzy: Enable FTS5 fuzzy search (default true).

    Returns:
        JSON string with query results including freshness metadata.
    """
    freshness = _ensure_fresh()
    results: list[dict] = []
    fuzzy = kwargs.get("fuzzy", True)

    has_dirty = freshness.get("dirty_files", 0) > 0
    certainty_base = "overlay-pending" if has_dirty else "matched"

    if query_type == "symbol":
        if fuzzy:
            try:
                results = db.search_symbols_fts(query)
            except Exception:
                results = db.search_symbols(query)
        else:
            results = db.search_symbols(query)
        for r in results:
            r["certainty"] = certainty_base

    elif query_type == "exporter":
        matched = db.search_symbols(query)
        files = {s["file_path"] for s in matched if s.get("file_path")}
        if files:
            all_syms = db.get_all_symbols()
            results = [s for s in all_syms if s.get("file_path") in files]
        for r in results:
            r["certainty"] = certainty_base

    elif query_type == "consumer":
        matched = db.search_symbols(query)
        files = {s["file_path"] for s in matched if s.get("file_path")}
        edges = db.get_all_edges()
        results = [
            {
                "consumer": {"project": e["src_project"], "file": e["src_file"]},
                "provider": {"project": e["dst_project"], "file": e["dst_file"]},
                "kind": e["kind"],
                "detail": e.get("detail", ""),
                "certainty": certainty_base,
            }
            for e in edges if e.get("dst_file") in files
        ]

    hint = ""
    if freshness.get("freshness") == "partial" and has_dirty:
        hint = (f"Results based on scanned commits. {freshness.get('dirty_projects', 0)} "
                f"project(s) have uncommitted changes not in the index. "
                f"Commit and re-query for completeness.")

    return json.dumps({
        "freshness": freshness,
        "query": query,
        "type": query_type,
        "count": len(results),
        "results": results,
        "hint": hint,
    }, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    import sys
    projects = sys.argv[1:] if len(sys.argv) > 1 else None
    result = scan(projects)
    print(result)
