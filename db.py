"""SQLite-backed storage for code-graph data.

Replaces edges.json / symbols.json / projects.json with a single
WAL-mode SQLite database for atomic per-project updates and fast queries.
"""

import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from config import WORKSPACE_ROOT

DB_RETRY_ATTEMPTS = 3
DB_RETRY_BASE_MS = 100

DATA_ROOT = WORKSPACE_ROOT / ".claude" / "code-graph"
DB_PATH = DATA_ROOT / "code_graph.db"
# Legacy JSON paths (used for one-shot migration)
LEGACY_EDGES = DATA_ROOT / "edges.json"
LEGACY_SYMBOLS = DATA_ROOT / "symbols.json"
LEGACY_PROJECTS = DATA_ROOT / "projects.json"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    last_hashed TEXT  -- JSON dict of {rel_path: hash16}
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,  -- 'function', 'class', 'export', 'interface', 'type', 'const'
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    project TEXT NOT NULL REFERENCES projects(name)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_project ON symbols(project);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    src_project TEXT NOT NULL,
    src_file TEXT NOT NULL,
    dst_project TEXT NOT NULL,
    dst_file TEXT DEFAULT '',
    dst_symbol TEXT DEFAULT '',
    kind TEXT NOT NULL,           -- 'http-calls', 'subprocess-spawn', 'filesystem-reads', 'import-dep', 'pip-depends'
    confidence TEXT DEFAULT 'extracted',
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_project, src_file);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_project, dst_file);
CREATE INDEX IF NOT EXISTS idx_edges_dst_symbol ON edges(dst_project, dst_symbol);

CREATE TABLE IF NOT EXISTS scan_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- FTS5 for symbol full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, kind, file_path, docstring, project,
    content='symbols', content_rowid='id'
);
"""

# Dummy docstring column — we store '' since symbols table lacks docstring.
# When symbols gain a docstring column, update the trigger.
FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, kind, file_path, docstring, project)
    VALUES (new.id, new.name, new.kind, new.file_path, '', new.project);
END;
CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, kind, file_path, docstring, project)
    VALUES ('delete', old.id, old.name, old.kind, old.file_path, '', old.project);
END;
CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, kind, file_path, docstring, project)
    VALUES ('delete', old.id, old.name, old.kind, old.file_path, '', old.project);
    INSERT INTO symbols_fts(rowid, name, kind, file_path, docstring, project)
    VALUES (new.id, new.name, new.kind, new.file_path, '', new.project);
END;
"""


def _conn() -> sqlite3.Connection:
    last_err = None
    for attempt in range(DB_RETRY_ATTEMPTS):
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_PATH), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            if attempt < DB_RETRY_ATTEMPTS - 1:
                delay = (DB_RETRY_BASE_MS * (2 ** attempt)
                         + random.randint(0, 50)) / 1000.0
                time.sleep(delay)
    raise last_err or sqlite3.OperationalError("cannot open database")


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _conn()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(FTS_TRIGGERS_SQL)
        conn.commit()
    finally:
        conn.close()


def _execute(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = _conn()
    try:
        cur = conn.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _execute_many(sql: str, params_list: list[tuple]) -> None:
    if not params_list:
        return
    conn = _conn()
    try:
        conn.executemany(sql, params_list)
        conn.commit()
    finally:
        conn.close()


def replace_project_edges(project: str, edges: list[dict]) -> None:
    """Atomic per-project edge replacement.

    Deletes all edges for *project* then inserts *edges* in a transaction.
    """
    conn = _conn()
    try:
        conn.execute("DELETE FROM edges WHERE src_project = ?", (project,))
        if edges:
            rows = [
                (
                    project,
                    e.get("src", {}).get("file", ""),
                    e.get("dst", {}).get("project", ""),
                    e.get("dst", {}).get("file", ""),
                    e.get("dst", {}).get("symbol", ""),
                    e.get("kind", ""),
                    e.get("confidence", "extracted"),
                    e.get("detail", ""),
                )
                for e in edges
            ]
            conn.executemany(
                """INSERT INTO edges
                   (src_project, src_file, dst_project, dst_file, dst_symbol,
                    kind, confidence, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        conn.commit()
    finally:
        conn.close()


def replace_project_symbols(project: str, symbols: list[dict]) -> None:
    """Atomic per-project symbol replacement."""
    conn = _conn()
    try:
        conn.execute("DELETE FROM symbols WHERE project = ?", (project,))
        if symbols:
            rows = [
                (s["name"], s.get("kind", "symbol"), s.get("file", ""),
                 s.get("line", 0), project)
                for s in symbols
            ]
            conn.executemany(
                "INSERT INTO symbols (name, kind, file_path, line, project) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        conn.commit()
    finally:
        conn.close()


def load_projects() -> dict[str, dict[str, any]]:
    """Load projects table into in-memory dict."""
    rows = _execute("SELECT name, root, last_hashed FROM projects")
    result: dict[str, dict] = {}
    for r in rows:
        result[r["name"]] = {
            "root": r["root"],
            "files": json.loads(r["last_hashed"]) if r["last_hashed"] else {},
        }
    return result


def upsert_projects(projects: dict[str, dict], changed_only: set[str] | None = None) -> None:
    """Upsert projects — only writes changed projects when *changed_only* is given,
    preserving all other rows in the table."""
    conn = _conn()
    try:
        for pname, pinfo in projects.items():
            if changed_only is not None and pname not in changed_only:
                continue
            hashed = json.dumps(pinfo.get("files", {}), ensure_ascii=False)
            conn.execute(
                "INSERT OR REPLACE INTO projects (name, root, last_hashed) VALUES (?, ?, ?)",
                (pname, pinfo.get("root", ""), hashed),
            )
        conn.execute(
            "INSERT OR REPLACE INTO scan_meta (key, value) VALUES ('last_scanned', ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"),),
        )
        conn.commit()
    finally:
        conn.close()


def save_projects(projects: dict[str, dict]) -> None:
    """Legacy: full replace of projects table (kept for migration compat)."""
    upsert_projects(projects)


# --- Migration ---

def migrate_from_json() -> bool:
    """One-shot: import legacy JSON files into SQLite.

    Returns True if migration happened, False if SQLite already has data.
    """
    conn = _conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM sqlite_master WHERE type='table' AND name='projects'").fetchone()
        if row and row[0]:
            # Table exists — check if it has data
            cnt = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()
            if cnt and cnt[0] > 0:
                return False
    finally:
        conn.close()
    # Table may exist empty — still run init/migrate
    init_db()

    # projects
    proj_rows: list[tuple] = []
    if LEGACY_PROJECTS.exists():
        try:
            data = json.loads(LEGACY_PROJECTS.read_text(encoding="utf-8"))
            for pname, pdata in data.get("projects", {}).items():
                hashed = json.dumps(pdata.get("files", {}), ensure_ascii=False)
                proj_rows.append((pname, pdata.get("root", ""), hashed))
        except Exception:
            pass
    if proj_rows:
        _execute_many(
            "INSERT OR REPLACE INTO projects (name, root, last_hashed) VALUES (?, ?, ?)",
            proj_rows,
        )

    # edges
    edge_rows: list[tuple] = []
    if LEGACY_EDGES.exists():
        try:
            edges = json.loads(LEGACY_EDGES.read_text(encoding="utf-8"))
            for e in edges:
                edge_rows.append((
                    e.get("src", {}).get("project", ""),
                    e.get("src", {}).get("file", ""),
                    e.get("dst", {}).get("project", ""),
                    e.get("dst", {}).get("file", ""),
                    e.get("dst", {}).get("symbol", ""),
                    e.get("kind", ""),
                    e.get("confidence", "extracted"),
                    e.get("detail", ""),
                ))
        except Exception:
            pass
    if edge_rows:
        _execute_many(
            """INSERT INTO edges
               (src_project, src_file, dst_project, dst_file, dst_symbol,
                kind, confidence, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            edge_rows,
        )

    # symbols
    sym_rows: list[tuple] = []
    if LEGACY_SYMBOLS.exists():
        try:
            symbols = json.loads(LEGACY_SYMBOLS.read_text(encoding="utf-8"))
            for s in symbols:
                sym_rows.append((
                    s.get("name", ""),
                    s.get("type", "symbol"),
                    s.get("file", ""),
                    s.get("line", 0),
                    _infer_project_from_path(s.get("file", ""), set()),
                ))
        except Exception:
            pass
    if sym_rows:
        _execute_many(
            "INSERT INTO symbols (name, kind, file_path, line, project) VALUES (?, ?, ?, ?, ?)",
            sym_rows,
        )

    return True


def _infer_project_from_path(path: str, _known: set[str] | None = None) -> str:
    """Extract project name from a file path under WORKSPACE_ROOT."""
    raw = path.replace("\\", "/")
    ws_str = str(WORKSPACE_ROOT).replace("\\", "/") + "/"
    idx = raw.find(ws_str)
    if idx >= 0:
        return raw[idx + len(ws_str):].split("/")[0]
    return ""


# --- Queries ---

def search_symbols(query: str) -> list[dict]:
    """LIKE-based symbol search (fallback)."""
    rows = _execute(
        "SELECT name, kind, file_path, line, project FROM symbols WHERE name LIKE ?",
        (f"%{query}%",),
    )
    return [dict(r) for r in rows]


def search_symbols_fts(query: str) -> list[dict]:
    """FTS5 full-text symbol search."""
    try:
        rows = _execute(
            """SELECT s.name, s.kind, s.file_path, s.line, s.project
               FROM symbols_fts f JOIN symbols s ON f.rowid = s.id
               WHERE symbols_fts MATCH ?
               LIMIT 50""",
            (query,),
        )
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # FTS5 may not have data yet — fallback
        return search_symbols(query)


def get_symbols_in_file(file_path: str) -> list[dict]:
    rows = _execute(
        "SELECT name, kind, line, project FROM symbols WHERE file_path = ?",
        (_normalize_path(file_path),),
    )
    return [dict(r) for r in rows]


def get_consumers_of_symbol(symbol_name: str) -> list[dict]:
    """Return edges whose dst.symbol matches. (Phase 4 full impl)"""
    rows = _execute(
        """SELECT src_project, src_file, dst_project, dst_file, kind, detail
           FROM edges WHERE dst_symbol = ?""",
        (symbol_name,),
    )
    return [dict(r) for r in rows]


def get_consumers_of_file(file_path: str) -> list[dict]:
    rows = _execute(
        """SELECT src_project, src_file, dst_project, dst_file, kind, detail
           FROM edges WHERE dst_file = ?""",
        (_normalize_path(file_path),),
    )
    return [dict(r) for r in rows]


def get_reverse_edges(dst_project: str, dst_file: str, max_depth: int) -> list[dict]:
    """Return all edges pointing to the given destination (project + optional file)."""
    if dst_file:
        rows = _execute(
            """SELECT src_project, src_file, dst_project, dst_file, kind, detail
               FROM edges
               WHERE (dst_project = ? AND dst_file = ?)
                  OR (dst_project = ? AND dst_file = '')""",
            (dst_project, _normalize_path(dst_file), dst_project),
        )
    else:
        rows = _execute(
            "SELECT src_project, src_file, dst_project, dst_file, kind, detail FROM edges WHERE dst_project = ?",
            (dst_project,),
        )
    return [dict(r) for r in rows]


def get_all_edges() -> list[dict]:
    rows = _execute(
        "SELECT src_project, src_file, dst_project, dst_file, dst_symbol, kind, confidence, detail FROM edges"
    )
    return [dict(r) for r in rows]


def get_all_symbols() -> list[dict]:
    rows = _execute("SELECT name, kind, file_path, line, project FROM symbols")
    return [dict(r) for r in rows]


def get_edge_count() -> int:
    r = _execute("SELECT COUNT(*) AS cnt FROM edges")
    return r[0]["cnt"] if r else 0


def get_symbol_count() -> int:
    r = _execute("SELECT COUNT(*) AS cnt FROM symbols")
    return r[0]["cnt"] if r else 0


def get_last_scanned() -> str | None:
    """Return last_scanned timestamp from scan_meta, or None if never scanned."""
    r = _execute("SELECT value FROM scan_meta WHERE key='last_scanned'")
    return r[0]["value"] if r else None

def _normalize_path(path: str) -> str:
    """Normalize separator to platform default for consistent DB matching."""
    if sys.platform == "win32":
        return path.replace("/", "\\")
    return path.replace("\\", "/")
