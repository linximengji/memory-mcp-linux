"""Shared memory storage — namespace/segment directory tree."""
import json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

from config import MEMORY_ROOT, PROXY_URL, LLM_MODEL
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL | re.MULTILINE)

VALID_SEGMENTS = frozenset({"_index", "overview", "deps", "design", "sop"})

# Single source of truth for segment definitions — consumed by extraction prompt, health checks
# Core principle: memories reflect CURRENT reality, NOT historical trajectory.
# When a system definition changes (segments, topology, paths), UPDATE existing memories
# to match the new reality — never create "what changed" records.
SEGMENT_GUIDES = {
    "_index": {
        "title": "项目名片",
        "what": "一句话定义项目的本质功能和触发场景。类 README 首段。",
        "example": "model-proxy：本地多模型路由代理，L1+L2 智能路由 + 7 条模型链路。",
        "style": "一句话说清项目是什么、什么场景触发",
    },
    "overview": {
        "title": "系统地图",
        "what": "项目结构当前状态的拓扑：入口文件、核心流程、文件清单。",
        "example": "入口 src/cli.ts → src/index.ts startBot，三通道（feishu/dingtalk/discord）。",
        "style": "入口在哪、核心文件、流程简述",
    },
    "deps": {
        "title": "依赖映射",
        "what": "跨项目/跨服务的端口、API、配置键、文件路径的**当前**准确依赖关系。",
        "example": "ops-daemon 的 process_manager.py 通过 saekill PS1 脚本终止 claudetalk 进程，端口 9878。",
        "style": "谁依赖谁、接口在哪、断了的后果",
    },
    "design": {
        "title": "决策记录",
        "what": "有明确取舍的决策：选了 A 否了 B+原因、当时的约束条件、这个设计的已知脆弱点。",
        "example": "决定用 WebSocket 替代轮询，延迟降 3x，代价是连接管理更复杂。脆弱点：WS 重连期间丢消息。",
        "style": "为什么选 A、B 为什么不行、约束条件、哪里容易出问题",
    },
    "sop": {
        "title": "方法论",
        "what": "具有现实意义的方法论：诊断思路（看到症状 X 先查 Y）、防御性知识（系统边界行为 A 会表现为 B，别误判为 C）、行动规范（做 X 前先做 Y）。回答为什么系统会这样行为。",
        "example": "SSE 连接空闲 30s 断开，根因是 HTTP/1.1 keep-alive 超时，不是 service down。误判为服务不可用会导致不必要的重启。",
        "style": "触发条件 → 排查怎么做 → 根因 → 误判后果。每条约立，不依赖历史上下文。",
    },
}

LINK_RE = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')
AUDIT_RECORD_RE = re.compile(r'\n?<!--\s*audit-record\s*\n.*?\n-->', re.DOTALL | re.IGNORECASE)


REL_WEIGHTS = {
    "rel:root-cause": 1.3,
    "rel:depends-on": 1.2,
    "rel:extends": 1.1,
    "rel:related-to": 1.0,
    "rel:supersedes": 0.8,
}
if sys.platform == "win32":
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore
else:
    _NO_WINDOW = 0


def _url_to_id(url: str) -> str:
    return re.sub(r"^https?://|\.git$|/$", "", url).replace(":", "/").replace("\\", "/")


def _detect_namespace(cwd: str | None = None) -> str:
    for d in ([cwd] if cwd else [os.getcwd()]):
        if not d:
            continue
        d = str(d)
        try:
            remote = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, cwd=d, timeout=5,
                creationflags=_NO_WINDOW,
            ).stdout.strip()
            if remote:
                name = Path(d).name
                return name
        except Exception:
            pass
        try:
            for root in [d] + list(Path(d).parents):
                pt = Path(root) / ".project-type"
                if pt.is_file():
                    return Path(root).name
        except Exception:
            pass
    return "global"


def _detect_project_id(cwd: str | None = None) -> str | None:
    for d in ([cwd] if cwd else [os.getcwd()]):
        if not d:
            continue
        d = str(d)
        try:
            remote = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, cwd=d, timeout=5,
                creationflags=_NO_WINDOW,
            ).stdout.strip()
            if remote:
                return _url_to_id(remote)
        except Exception:
            pass
    return None


def _render_frontmatter(name: str, namespace: str, segment: str) -> str:
    return f"---\nname: {name}\nsegment: {segment}\n---\n"


def _pick_target_path(namespace: str, name: str) -> Path:
    ns_dir = MEMORY_ROOT / namespace
    ns_dir.mkdir(parents=True, exist_ok=True)
    return ns_dir / f"{name}.md"


def _parse_file(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            raw = path.read_text(encoding="gbk")
            path.write_text(raw, encoding="utf-8")
        except Exception:
            return None
    m = FM_RE.match(raw)
    if not m:
        return None
    fm = {"name": path.stem}
    for line in m.group(1).splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2:
            fm[parts[0].strip()] = parts[1].strip()
    ns = fm.get("namespace", path.parent.name)
    return {
        "name": fm.get("name", path.stem),
        "namespace": ns,
        "segment": fm.get("segment", "_index"),
        "content": m.group(2).strip(),
        "path": path,
    }


def _iter_memories():
    for ns_dir in MEMORY_ROOT.iterdir():
        if not ns_dir.is_dir() or ns_dir.name.startswith("_") or ns_dir.name.startswith("."):
            continue
        for p in ns_dir.glob("*.md"):
            parsed = _parse_file(p)
            if parsed:
                yield parsed


def _touch_memory(path: Path) -> None:
    """Ensure file exists. No-op since we removed tracking metadata."""
    pass


INDEX_FILE = MEMORY_ROOT / "MEMORY.md"


def _generate_index_line(ns: str) -> str | None:
    """Generate a single index line for the given namespace."""
    ns_dir = MEMORY_ROOT / ns
    if not ns_dir.is_dir():
        return None

    desc = ""
    segs: set[str] = set()
    for f in ns_dir.glob("*.md"):
        mem = _parse_file(f)
        if mem is None:
            continue
        segs.add(mem["segment"])
        if mem["segment"] == "_index" and mem.get("description"):
            # Prefer primary: _index.md > {ns}.md > {ns}-index.md > first found
            stem = f.stem
            is_primary = (f.name == "_index.md"
                          or stem == ns
                          or stem == f"{ns}-index")
            if is_primary or not desc:
                desc = mem["description"][:120]

    if not segs:
        return None

    segs_str = ", ".join(sorted(segs))
    return f"- **{ns}** — {desc} (segments: {segs_str})  <!-- category: {category} -->"


def _update_index(ns: str) -> None:
    """Update MEMORY.md by rebuilding the full categorized index."""
    _rebuild_index()


def _rebuild_index() -> None:
    """Full rebuild: categorized MEMORY.md grouped by category."""
    ns_map: dict[str, set[str]] = {}
    for mem in _iter_memories():
        ns_map.setdefault(mem["namespace"], set()).add(mem["segment"])

    INDEX_FILE.write_text(
        "# Memory Index\n\n" +
        "\n".join(
            f"- **{ns}** — (segments: {', '.join(sorted(segs))})"
            for ns, segs in sorted(ns_map.items())
        ) + "\n",
        encoding="utf-8",
    )


def _resolve_path(name: str, namespace: str | None = None) -> Path | None:
    """Resolve name like 'ns/segment' or just 'name' to file path."""
    if "/" in name and not namespace:
        parts = name.split("/", 1)
        namespace = parts[0]
        name = parts[1]
    if namespace:
        path = MEMORY_ROOT / namespace / f"{name}.md"
        if path.exists():
            return path
    # Search all namespaces
    for ns_dir in MEMORY_ROOT.iterdir():
        if not ns_dir.is_dir() or ns_dir.name.startswith("_") or ns_dir.name.startswith("."):
            continue
        path = ns_dir / f"{name}.md"
        if path.exists():
            return path
    # Try _index as fallback
    for ns_dir in MEMORY_ROOT.iterdir():
        if not ns_dir.is_dir() or ns_dir.name.startswith("_") or ns_dir.name.startswith("."):
            continue
        if ns_dir.name == name:
            return ns_dir / "_index.md"
    # Last resort: iterate all memories and match frontmatter name
    for mem in _iter_memories():
        if mem["name"] == name:
            if namespace and mem.get("namespace") != namespace and mem.get("namespace") != "(global)":
                continue
            return mem["path"]
    return None


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    def ngrams(s, n=2):
        s = s.lower()
        return {s[i:i+n] for i in range(len(s) - n + 1)}
    a_set = ngrams(text_a)
    b_set = ngrams(text_b)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)



def _keyword_score(query: str, mem: dict, pid: str | None) -> float:
    boost = 1.0
    if pid:
        if mem.get("namespace", "") == pid:
            boost = 1.2
        elif mem["namespace"] == "(global)":
            boost = 1.05
        else:
            boost = 0.9
    ql = query.lower()
    name = mem["name"].lower()
    body = mem["content"].lower()
    if ql in name:
        score = 1.0
    elif ql in body:
        score = 0.9
    else:
        tokens = [t for t in re.split(r"[\s,，.。、/\\\-_()（）]+", ql) if t and len(t) >= 2]
        best = 0.1
        for tok in tokens:
            if tok in name:
                best = max(best, 1.0)
            elif tok in body:
                best = max(best, 0.9)
            else:
                best = max(best, 0.15)
        score = best
    return score * boost


def _llm_rerank(query: str, candidates: list[dict], top_k: int) -> list[tuple[float, dict]] | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return [(1.0, candidates[0])]
    items = [
        f"{i}. {mem['name']} [{mem.get('namespace','?')}] - {mem['content'][:80]}"
        for i, mem in enumerate(candidates)
    ]
    prompt = (
        f"Which are most relevant to \"{query}\"?\n"
        + "\n".join(items)
        + "\n\nAnswer with ONLY the item numbers in order, like: 0,1"
    )
    try:
        payload = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 50,
        }).encode("utf-8")
        req = urllib.request.Request(
            PROXY_URL,
            data=payload,
            headers={"Content-Type": "application/json", "x-api-key": "sk-memory-mcp",
                     "anthropic-version": "2023-06-01"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = "".join(
                block.get("text", "") for block in data["content"]
                if block.get("type") == "text"
            ).strip()
    except Exception:
        return None

    indices = [int(tok.rstrip(".")) for tok in re.split(r"[,，\s]+", text) if tok.strip().rstrip(".").isdigit()]
    ranked = []
    seen = set()
    for idx in indices:
        if 0 <= idx < len(candidates) and idx not in seen:
            seen.add(idx)
            ranked.append((1.0 - len(seen) * 0.01, candidates[idx]))
    return ranked if ranked else None


def _parse_links(content: str) -> list[tuple[str, str]]:
    result = []
    for m in LINK_RE.finditer(content):
        target = m.group(1).strip()
        rel = m.group(2).strip() if m.group(2) else "rel:related-to"
        result.append((target, rel))
    return result


def _expand_via_links(ranked: list[tuple[float, dict]], top_k: int) -> list[tuple[float, dict]]:
    if not ranked:
        return ranked
    all_by_name = {m["name"]: m for m in _iter_memories()}
    expanded = {m["name"]: (s, m) for s, m in ranked}
    for score, mem in ranked:
        links = _parse_links(mem.get("content", ""))
        for target, rel in links:
            if target not in all_by_name or target in expanded:
                continue
            weight = REL_WEIGHTS.get(rel, 1.0)
            expanded[target] = (min(score * weight * 0.7, 1.0), all_by_name[target])
    result = sorted(expanded.values(), key=lambda x: -x[0])
    return result[:top_k]


# ── Public API ──

SIM_THRESHOLD = 0.6
NEARBY_THRESHOLD = 0.25


def memory_upsert(name: str, content: str,
                   namespace: str | None = None, segment: str = "_index") -> str:
    """Auto-detect: insert new or update existing via Jaccard similarity."""
    ns = namespace or _detect_namespace()

    # Find sim match (content overlap)
    closest_name = None
    closest_score = 0.0
    for mem in _iter_memories():
        if namespace and mem["namespace"] != namespace and mem.get("namespace") != "(global)":
            continue
        jd = _jaccard_similarity(content, mem["content"])
        if jd >= SIM_THRESHOLD and jd > closest_score:
            closest_name = mem["name"]
            closest_score = jd

    if closest_name:
        return memory_update(closest_name, content=content, namespace=namespace)

    # No sim match: check exact name match (same namespace)
    for mem in _iter_memories():
        if mem["name"] == name:
            if namespace and mem["namespace"] != namespace:
                continue
            return memory_update(name, content=content, namespace=namespace)

    # Nothing found → fresh insert
    return memory_store(name, content, namespace=namespace, segment=segment)


def memory_store(name: str, content: str,
                  namespace: str | None = None, segment: str = "_index") -> str:
    if segment not in VALID_SEGMENTS:
        return f"error: invalid segment '{segment}'. Must be: {', '.join(sorted(VALID_SEGMENTS))}"
    if not re.match(r'^[a-z0-9][-a-z0-9]*$', name):
        return f"error: name must be lowercase kebab-case"
    ns = namespace or _detect_namespace()
    if not re.match(r'^[a-z][a-z0-9-]*$', ns):
        return f"error: namespace must be lowercase kebab-case (a-z, 0-9, hyphens only), got: {repr(ns)}"

    filepath = _pick_target_path(ns, name)
    if filepath.exists():
        return f"error: '{name}' already exists in '{ns}/'. Use memory_update."

    for mem in _iter_memories():
        if mem["name"] == name:
            return f"error: '{name}' already exists at {mem['path']}. Use memory_update."

    fm = _render_frontmatter(name, ns, segment)
    filepath.write_text(fm + content + "\n", encoding="utf-8")
    _update_index(ns)
    return f"stored: {filepath}"


def memory_update(name: str, content: str | None = None,
                   namespace: str | None = None,
                   _skip_rebuild: bool = False) -> str:

    # When namespace is given, resolve exact path
    if namespace is not None:
        path = _resolve_path(name, namespace)
        # _resolve_path falls back to search all namespaces — reject that
        if not path or path.parent.name != namespace:
            return f"error: '{name}' not found in namespace '{namespace}'"
        mems = [m for m in _iter_memories() if m["path"] == path]
        mem = mems[0] if mems else None
        if not mem:
            return f"error: '{name}' not found at {path}"
    else:
        # No namespace: fallback to first name match across all namespaces
        mem = None
        for m in _iter_memories():
            if m["name"] == name:
                mem = m
                break
    if mem is None:
        return f"error: '{name}' not found"

    raw = mem["path"].read_text(encoding="utf-8")
    m = FM_RE.match(raw)
    if not m:
        return f"error: cant parse {mem['path']}"
    body = m.group(2)
    if content is not None:
        body = content

    # Preserve existing frontmatter fields (name, segment), only update what's provided
    fm_fields = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm_fields[k.strip()] = v.strip()
    fm_fields["name"] = mem["name"]
    new_fm = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm_fields.items()) + "\n---\n"
    mem["path"].write_text(new_fm + body.strip() + "\n", encoding="utf-8")
    if not _skip_rebuild:
        _update_index(mem["namespace"])
    return f"updated: {mem['path']}"


def memory_recall(query: str = "", namespace: str | None = None,
                   top_k: int = 5) -> list[dict]:
    """Search memories by query. Returns list of dicts with score, name, namespace, segment."""
    pid = _detect_namespace()
    candidates = []
    for mem in _iter_memories():
        if namespace and mem["namespace"] != namespace and mem["namespace"] != "(global)":
            continue
        candidates.append(mem)

    if not candidates:
        return []

    if query:
        ranked = _llm_rerank(query, candidates, top_k)
        if ranked is None:
            ranked = sorted(
                ((_keyword_score(query, m, pid), m) for m in candidates),
                key=lambda x: -x[0],
            )
    else:
        ranked = []
        for mem in candidates:
            score = 1.0
            if pid:
                if mem.get("namespace", "") == pid:
                    score *= 1.2
                elif mem["namespace"] == "(global)":
                    score *= 1.05
                else:
                    score *= 0.9
            ranked.append((score, mem))
        ranked.sort(key=lambda x: -x[0])

    ranked = ranked[:top_k]
    if query:
        ranked = _expand_via_links(ranked, top_k)

    return [
        {"name": m["name"], "namespace": m["namespace"],
         "segment": m["segment"], "score": round(score, 3)}
        for score, m in ranked
    ]


def format_recall_text(results: list[dict]) -> str:
    """Format list[dict] from memory_recall() into human-readable text."""
    return "\n".join(
        f"- [{r['name']}] [{r['namespace']}] seg={r['segment']} score={r['score']}"
        for r in results
    )


def memory_get(name: str, namespace: str | None = None) -> str:
    path = _resolve_path(name, namespace)
    if not path:
        return f"error: '{name}' not found"
    for mem in _iter_memories():
        if mem["path"] == path:
            _touch_memory(path)
            break
    return path.read_text(encoding="utf-8")


def memory_list(namespace: str | None = None) -> str:
    ns_map: dict[str, list[str]] = {}
    for mem in _iter_memories():
        if namespace and mem["namespace"] != namespace:
            continue
        ns_map.setdefault(mem["namespace"], []).append(mem["name"])

    lines = []
    for ns in sorted(ns_map.keys()):
        lines.append(f"## {ns}")
        for name in sorted(ns_map[ns]):
            lines.append(f"  {name}")
    return "\n".join(lines) if lines else ""


def memory_delete(name: str, namespace: str | None = None) -> str:
    """Delete a memory by name. Pass namespace for exact match."""
    if namespace:
        path = _resolve_path(name, namespace)
        if not path or path.parent.name != namespace:
            return f"error: '{name}' not found in namespace '{namespace}'"
        path.unlink()
        _update_index(namespace)
        return f"deleted: {path}"
    # No namespace: search all namespaces, take first match
    for mem in _iter_memories():
        if mem["name"] == name:
            ns = mem["namespace"]
            mem["path"].unlink()
            _update_index(ns)
            return f"deleted: {mem['path']}"
    return f"error: '{name}' not found"


def memory_rebuild_index() -> str:
    _rebuild_index()
    return "MEMORY.md rebuilt"


def memory_review(action: str, name: str = "", namespace: str | None = None) -> str:
    """Manage review findings. action: list | confirm | dismiss | dismiss-all.
    list — show pending review items
    confirm name [ns] — remove from pending
    dismiss name [ns] — remove from pending + add to .dismissed
    dismiss-all — clear all pending items
    """
    report_path = MEMORY_ROOT / "review-report.json"
    dismissed_path = MEMORY_ROOT / ".dismissed"

    def _load_pending() -> list[dict]:
        if not report_path.exists():
            return []
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []

    def _save_pending(items: list[dict]) -> None:
        report_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_dismissed_set() -> set[str]:
        if not dismissed_path.exists():
            return set()
        return {l.strip() for l in dismissed_path.read_text("utf-8").splitlines() if l.strip()}

    def _add_dismissed(file_name: str) -> None:
        d = _load_dismissed_set()
        d.add(file_name.strip())
        dismissed_path.write_text("\n".join(sorted(d)) + "\n", encoding="utf-8")

    if action == "list":
        items = _load_pending()
        if not items:
            return "no pending review items"
        return "\n".join(
            f"- [{it.get('name','?')}] [{it.get('namespace','?')}] "
            f"action={it.get('action','?')} reason={it.get('reason','')[:80]}"
            for it in items
        )

    if action == "confirm":
        if not name:
            return "error: name required for confirm"
        items = _load_pending()
        before = len(items)
        items = [it for it in items if not (it.get("name") == name
                 and (namespace is None or it.get("namespace") == namespace))]
        if len(items) == before:
            return f"no pending item found: {name}"
        _save_pending(items)
        return f"confirmed: {name}"

    if action == "dismiss":
        if not name:
            return "error: name required for dismiss"
        items = _load_pending()
        before = len(items)
        items = [it for it in items if not (it.get("name") == name
                 and (namespace is None or it.get("namespace") == namespace))]
        if len(items) < before:
            _save_pending(items)
        _add_dismissed(name)
        return f"dismissed: {name}"

    if action == "dismiss-all":
        _save_pending([])
        return "all pending items dismissed"

    return f"unknown action: {action}"


def memory_cleanup(action: str = "list", names: list[str] | None = None) -> str:
    review_path = MEMORY_ROOT / "review-report.json"
    if action == "list":
        if not review_path.exists():
            return "no low-quality candidates"
        try:
            items = json.loads(review_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return "no low-quality candidates"
        if not items:
            return "no low-quality candidates"
        lines = [f"name={it['name']} ns={it.get('ns','')} action={it.get('action','?')}"
                 for it in items if it.get("action") in ("demote", "flag")]
        if not lines:
            return "no low-quality candidates"
        return "low-quality candidates:\n" + "\n".join(lines)
    if action == "delete" and names:
        return "\n".join(memory_delete(n) for n in names)
    return f"unknown action: {action}"


def _strip_audit(content: str) -> str:
    return AUDIT_RECORD_RE.sub("", content).strip()


def _extract_audit(content: str) -> str | None:
    m = AUDIT_RECORD_RE.search(content)
    return m.group(0) if m else None


def _ensure_audit_record(content: str) -> str:
    if _extract_audit(content):
        return content
    today = time.strftime("%Y-%m-%d")
    return content.rstrip() + (
        f"\n\n<!-- audit-record\n"
        f"source_session: unknown\n"
        f"extraction_date: {today}\n"
        f"intended_recall_query: (auto)\n"
        f"review_status: pending\n"
        f"-->"
    )


# ── Session → memory mapping ──────────────────────────────────────

def _session_map_path() -> Path:
    return MEMORY_ROOT / "session_memory_map.json"


def _load_session_map() -> dict[str, list[str]]:
    p = _session_map_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_session_map(mapping: dict[str, list[str]]):
    _session_map_path().write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def memory_add_to_session_map(session_id: str, memory_name: str):
    """Record that memory_name was extracted from this session (O(1) lookup)."""
    if not session_id or not memory_name:
        return
    sid_key = session_id[:24]
    mapping = _load_session_map()
    names = mapping.setdefault(sid_key, [])
    if memory_name not in names:
        names.append(memory_name)
        _save_session_map(mapping)


def memory_get_session_memories(session_id: str) -> list[str]:
    """Return memory names previously extracted from this session. Empty list if none."""
    if not session_id:
        return []
    mapping = _load_session_map()
    return mapping.get(session_id[:24], [])


def memory_snapshot(mem_cache: list[dict] | None = None) -> dict:
    """Build runtime memory snapshot for extraction prompt."""
    coverage: dict[str, dict[str, int]] = {}

    mems = mem_cache if mem_cache is not None else list(_iter_memories())
    for mem in mems:
        ns = mem["namespace"]
        seg = mem["segment"]
        coverage.setdefault(ns, {}).setdefault(seg, 0)
        coverage[ns][seg] += 1

    return {
        "coverage": coverage,
        "total_namespaces": len(coverage),
        "total_memories": len(mems),
        "active_ns": _detect_namespace(),
    }
