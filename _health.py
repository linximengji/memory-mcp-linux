"""Memory health check system.

Layers:
  L1 — structural checks (orphans, broken links, stats): every run, no LLM
  L3 — LLM audit (quality/contradictions) + consolidation + completeness: gated by llm_enabled
  Task — auto-create pending tasks for items needing manual handling

Entry: run_memory_health(llm_enabled=False) -> dict
Consumed by: _health_cli.py (CLI), daily_report/runner.py (subprocess), MCP tools
"""

import os, re, json, time, urllib.request, sys

# Allow running from any CWD
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import date, datetime
from pathlib import Path
from collections import Counter
from typing import Any

from _storage import _strip_audit, _jaccard_similarity, VALID_SEGMENTS, SEGMENT_GUIDES
from config import MEMORY_ROOT, WORKSPACE_ROOT, PROXY_URL, LLM_MODEL

MEMORY_DIR = MEMORY_ROOT
INDEX_FILE = MEMORY_DIR / "MEMORY.md"
REVIEW_REPORT = MEMORY_DIR / "review-report.md"
DISMISSED_FILE = MEMORY_DIR / ".dismissed"
SECOND_MODEL = "qwen3.7-max"

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
WIKI_RE = re.compile(r"\[\[([^\]]+?)(?:\|[^\]]*)?\]\]")
AUDIT_RE = re.compile(r"<!--\s*audit-record\s+.*?-->", re.DOTALL)
SENSITIVE_PATTERNS = [
    (re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b"), "api_key"),
    (re.compile(r"\bgh[opsu]_[a-zA-Z0-9]{16,}\b"), "github_token"),
    (re.compile(r"\b-----BEGIN (RSA )?PRIVATE KEY-----\b"), "private_key"),
    (re.compile(r"https?://[^:]+:[^@]+@"), "url_creds"),
]


def _load_dismissed() -> set[str]:
    if DISMISSED_FILE.exists():
        return {line.strip() for line in DISMISSED_FILE.read_text("utf-8").splitlines() if line.strip()}
    return set()


def _read_file_safe(path: Path) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp." + path.name)
    tmp.write_text(content, encoding="utf-8", newline="")
    tmp.replace(path)


def _parse_frontmatter(path: Path) -> dict | None:
    raw = _read_file_safe(path)
    m = FM_RE.match(raw)
    if not m:
        return None
    fm: dict[str, Any] = {"path": path, "_raw": raw, "_body": m.group(2).strip()}
    for line in m.group(1).strip().split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            fm[k] = v
    fm["namespace"] = fm.get("namespace", "")
    fm["segment"] = fm.get("segment", "_index")
    fm["name"] = fm.get("name", path.stem)
    fm["_created"] = datetime.fromtimestamp(path.stat().st_ctime)
    return fm


def _age_days(fm: dict) -> float:
    now = datetime.now()
    age = (now - fm["_created"]).total_seconds() / 86400
    la = fm.get("_last_ts", 0)
    if la:
        la_age = (now.timestamp() - la) / 86400
        age = max(age, la_age)
    return age


def _call_llm(prompt: str, model: str = LLM_MODEL,
              max_tokens: int = 500, temperature: float = 0.3, timeout: int = 30) -> str:
    try:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode("utf-8")
        req = urllib.request.Request(
            PROXY_URL,
            data=payload,
            headers={"Content-Type": "application/json",
                     "x-api-key": "sk-memory-health",
                     "anthropic-version": "2023-06-01"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = "".join(
                block.get("text", "") for block in data.get("content", [])
                if block.get("type") == "text"
            ).strip()
        return text
    except Exception as e:
        return f"__LLM_ERROR__:{e}"


# ═══════════════════════════════════════════════════════
# L1 — structural checks
# ═══════════════════════════════════════════════════════

def _build_fm_cache() -> list[dict]:
    cache = []
    if not MEMORY_DIR.is_dir():
        return cache
    for ns_dir in sorted(MEMORY_DIR.iterdir()):
        if not ns_dir.is_dir() or ns_dir.name.startswith((".", "_")):
            continue
        for f in sorted(ns_dir.glob("*.md")):
            fm = _parse_frontmatter(f)
            if fm:
                cache.append(fm)
    return cache


def _check_orphans(fm_cache: list[dict]) -> list[dict]:
    idx_names: set[str] = set()
    if INDEX_FILE.exists():
        for line in INDEX_FILE.read_text("utf-8").split("\n"):
            m = re.match(r"-\s*\[([^\]]+)\]", line)
            if m:
                idx_names.add(m.group(1))
    cached_names = {fm.get("name", "") for fm in fm_cache}
    orphaned = []
    for ns_dir in sorted(MEMORY_DIR.iterdir()):
        if not ns_dir.is_dir() or ns_dir.name.startswith((".", "_")):
            continue
        for f in sorted(ns_dir.glob("*.md")):
            stem = f.stem
            if stem in ("MEMORY", "review-report"):
                continue
            in_cache = any(fm.get("path") == f for fm in fm_cache)
            if not in_cache:
                orphaned.append({"stem": stem, "path": str(f)})
    return orphaned


def _check_broken_links(fm_cache: list[dict]) -> list[dict]:
    available = {fm.get("name", "") for fm in fm_cache}
    broken = []
    for fm in fm_cache:
        body = fm.get("_body", "")
        for link in WIKI_RE.findall(body):
            name = link.split("/")[-1].strip()
            if name and name not in available:
                broken.append({"file": fm.get("name", "?"), "link": name})
    return broken


def _cleanup_orphans(orphans: list[dict], max_age_hours: int = 24) -> list[dict]:
    cleaned = []
    now = time.time()
    for o in orphans:
        try:
            mtime = os.path.getmtime(o["path"])
            if (now - mtime) / 3600 >= max_age_hours:
                os.remove(o["path"])
                cleaned.append(o)
        except (FileNotFoundError, OSError):
            pass
    return cleaned


def _fix_broken_links(broken: list[dict], fm_cache: list[dict]) -> int:
    cache_by_name = {fm["name"]: fm for fm in fm_cache}
    by_file: dict[str, set[str]] = {}
    for b in broken:
        by_file.setdefault(b["file"], set()).add(b["link"])
    fixed = 0
    for filename, links in by_file.items():
        fm = cache_by_name.get(filename)
        if not fm:
            continue
        raw = _read_file_safe(fm["path"])
        body_m = FM_RE.match(raw)
        if not body_m:
            continue
        body = body_m.group(2)
        new_body = body
        for link in links:
            new_body = re.sub(
                rf"\[\[{re.escape(link)}(\|[^\]]*)?\]\]",
                "", new_body,
            )
        if new_body != body:
            prefix = raw[: body_m.start(2)]
            suffix = raw[body_m.end(2):]
            _atomic_write(fm["path"], prefix + new_body + suffix)
            fixed += 1
    return fixed


def _compute_stats(fm_cache: list[dict]) -> dict:
    files_total = len(fm_cache)
    lines_total = 0
    domains: Counter = Counter()
    projects: Counter = Counter()
    oversized = []
    for fm in fm_cache:
        ns = fm.get("namespace", "unknown")
        seg = fm.get("segment", "_index")
        domains[f"{ns}/{seg}"] += 1
        proj = fm.get("project_id", "") or ns
        projects[proj] += 1
        nlines = fm.get("_raw", "").count("\n") + 1
        lines_total += nlines
        if nlines > 500:
            oversized.append({"stem": fm.get("name", ""), "lines": nlines})
    return {
        "files_total": files_total,
        "lines_total": lines_total,
        "by_domain": dict(domains),
        "by_project": dict(projects),
        "oversized": oversized,
    }


def _sensitive_scan(content: str) -> list[str]:
    matched = []
    for pat, label in SENSITIVE_PATTERNS:
        if pat.search(content):
            matched.append(label)
    return matched


# ── L1 cross-referencing checker ─────────────────────────────────────

def _check_segment_references(fm_cache: list[dict]) -> list[dict]:
    """Scan all memory files for stale segment references.

    Detects:
    - Deprecated segment names in body text (pitfalls, architecture, changelog)
    - Stale frontmatter segment fields (segment: pitfalls)
    - Outdated segment model numbers (e.g. "6 本手册", "4 段", "6 段")
    - Stale numeric claims vs actual VALID_SEGMENTS count
    """
    deprecated_segs = {"pitfalls"}  # segs removed from VALID_SEGMENTS
    dep_pattern = re.compile(
        r'\b' + r'\b|\b'.join(re.escape(s) for s in deprecated_segs) + r'\b',
        re.IGNORECASE,
    )
    audit_comment_re = re.compile(r'<!--.*?-->', re.DOTALL)
    count_pattern = re.compile(r'(\d+)\s*(本手册|段|本活页手册)')
    issues: list[dict] = []

    for fm in fm_cache:
        ns = fm.get("namespace", "?")
        name = fm.get("name", "?")
        segment = fm.get("segment", "")
        body = fm.get("_body", "")

        # Strip HTML comments (audit records) — they're operational metadata, not content
        body_clean = audit_comment_re.sub('', body)
        raw = fm.get("_raw", "")

        # Check frontmatter segment field
        if segment in deprecated_segs:
            issues.append({
                "file": name, "ns": ns, "segment": segment,
                "check": "stale_frontmatter",
                "detail": f"segment={segment} is deprecated, use one of {'|'.join(VALID_SEGMENTS)}",
            })

        # Check body text for deprecated segment names (skip audit-record comments)
        for m in dep_pattern.finditer(body_clean):
            issues.append({
                "file": name, "ns": ns, "segment": segment,
                "check": "deprecated_seg_ref",
                "detail": f"body mentions deprecated segment '{m.group()}'",
            })

        # Check body for stale numeric claims (skip audit-record comments)
        for m in count_pattern.finditer(body_clean):
            count = int(m.group(1))
            if count != len(VALID_SEGMENTS):
                issues.append({
                    "file": name, "ns": ns, "segment": segment,
                    "check": "stale_count",
                    "detail": f"says '{m.group().strip()}' but actual segment count is {len(VALID_SEGMENTS)}",
                })

    return issues
def _build_segment_guide_text() -> str:
    """Build a compact segment definition string for LLM prompts."""
    return "\n".join(
        f"  {seg}: {info['title']} — {info.get('what', info.get('style', ''))[:120]}"
        for seg, info in SEGMENT_GUIDES.items()
    )


# ═══════════════════════════════════════════════════════
# L3 — deep audit + consolidation
# ═══════════════════════════════════════════════════════

def _write_audit_record(path: Path, verdict: str, note: str) -> None:
    raw = _read_file_safe(path)
    today = date.today().isoformat()
    safe_note = note[:200].replace("\\", "/")
    record = (f"<!-- audit-record review_status=reviewed review_date={today} "
              f"review_verdict={verdict} review_note={safe_note} -->")
    if AUDIT_RE.search(raw):
        raw = AUDIT_RE.sub(lambda m: record, raw)
    else:
        raw = raw.rstrip() + "\n\n" + record + "\n"
    _atomic_write(path, raw)


def _increment_demote_count(path: Path) -> None:
    raw = _read_file_safe(path)
    m = FM_RE.match(raw)
    if not m:
        return
    meta_text = m.group(1)
    current = 0
    new_lines = []
    found = False
    for line in meta_text.strip().split("\n"):
        if line.startswith("demote_count:"):
            try:
                current = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                current = 0
            new_lines.append(f"demote_count: {current + 1}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append("demote_count: 1")
    new_fm = "---\n" + "\n".join(new_lines) + "\n---\n"
    body = m.group(2).strip()
    _atomic_write(path, new_fm + body + "\n")


def _deep_audit(fm_cache: list[dict]) -> list[dict]:
    max_candidates = 15
    dismissed = _load_dismissed()
    guide_text = _build_segment_guide_text()

    candidates = sorted(fm_cache, key=lambda fm: _age_days(fm))[:max_candidates]

    results = []
    for fm in candidates:
        name = fm.get("name", "")
        ns = fm.get("namespace", "")
        age = round(_age_days(fm), 1)
        body = _strip_audit(fm.get("_body", ""))[:2500]

        sensitive = _sensitive_scan(body)
        sensitive_note = f"\nNOTE: regex matched {sensitive}" if sensitive else ""

        prompt = (
            "You are a memory quality auditor. Rate this entry on 6 dimensions.\n"
            "Do NOT approve by default -- find at least one weakness per entry.\n"
            "Reply with JSON only.\n\n"
            "Memory segment definitions:\n"
            f"{guide_text}\n\n"
            "Dimensions:\n"
            "- valid: content is coherent, non-trivial, consistent\n"
            "- segment_correct: content belongs to the assigned segment (see definitions above)\n"
            "- recall_oriented: name matches likely search queries\n"
            "- fact_correct: mentioned function names, file paths, config keys,\n"
            "  parameters are plausible\n"
            "- sensitive: contains API keys, passwords, JWT tokens, private keys,\n"
            "  or other credentials in plaintext\n"
            "- action: keep|demote|flag\n"
            "  keep = no issues, demote = needs review, flag = valid but needs fixes\n\n"
            'JSON: {"valid":bool,"segment_correct":bool,"recall_oriented":bool,\n'
            '       "fact_correct":bool,"has_sensitive_content":bool,\n'
            '       "action":"keep"|"demote"|"flag",\n'
            '       "reason":"short CN reason"}\n\n'
            f"name: {name}\nns: {ns}\nage: {age}d\n---\n{body}{sensitive_note}"
        )

        reply = _call_llm(prompt, max_tokens=300, temperature=0.3)
        if reply.startswith("__LLM_ERROR__"):
            results.append({"file": name, "error": reply.split(":", 1)[1]})
            continue

        jm = re.search(r"\{[^{}]*\}", reply)
        if not jm:
            results.append({"file": name, "error": "no JSON in LLM response"})
            continue

        try:
            parsed = json.loads(jm.group())
        except json.JSONDecodeError:
            results.append({"file": name, "error": "JSON parse error"})
            continue

        action = parsed.get("action", "keep")
        has_sensitive = parsed.get("has_sensitive_content", False)
        reason = parsed.get("reason", "")[:200]

        if action == "keep" and not parsed.get("valid", True):
            action = "demote"

        results.append({
            "file": name,
            "namespace": ns,
            "action": action,
            "has_sensitive": has_sensitive,
            "reason": reason,
        })

    return results


def _consolidation_pass(fm_cache: list[dict]) -> list[dict]:
    bodies = {}
    for fm in fm_cache:
        bodies[fm["name"]] = _strip_audit(fm.get("_body", ""))

    pairs = []
    names = list(bodies.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            sim = _jaccard_similarity(bodies[names[i]], bodies[names[j]])
            if 0.30 <= sim < 0.60:
                pairs.append((names[i], names[j], sim))

    # Separate same-ns and cross-ns pairs
    lk = {fm["name"]: fm.get("namespace", "?") for fm in fm_cache}
    same_ns: dict[str, list[tuple]] = {}
    cross_ns: list[tuple] = []
    for a, b, sim in pairs:
        ns_a, ns_b = lk.get(a, "?"), lk.get(b, "?")
        if ns_a == ns_b:
            same_ns.setdefault(ns_a, []).append((a, b, sim))
        else:
            cross_ns.append((ns_a, ns_b, a, b, sim))

    all_candidates = []
    # Take top 3 per namespace group
    for ns, group in same_ns.items():
        group.sort(key=lambda x: x[2], reverse=True)
        for a, b, sim in group[:3]:
            all_candidates.append(
                f"- [{ns}] source={a}, target={b}, jaccard={sim:.2f}"
            )
    # Take top 10 cross-ns pairs (annotated for the LLM)
    cross_ns.sort(key=lambda x: x[4], reverse=True)
    for ns_a, ns_b, a, b, sim in cross_ns[:10]:
        all_candidates.append(
            f"- [CROSS-NS {ns_a}↔{ns_b}] source={a}, target={b}, "
            f"jaccard={sim:.2f} — SAME fact in different namespaces?"
        )

    if not all_candidates:
        return []

    prompt = (
        "You are a memory consolidation auditor. Review each candidate pair.\n"
        "Two entries with Jaccard 0.30-0.60 may overlap in topic. Only merge if:\n"
        "- They discuss the SAME topic / decision / problem from different angles\n"
        "- One is a strict subset of the other (merge subset INTO superset)\n"
        "- Cross-namespace duplicates describe the SAME dependency/constraint\n\n"
        "Cross-namespace pairs [CROSS-NS]: if they describe the same fact,\n"
        "keep in the 'owns' namespace (who defines the interface/config key),\n"
        "the other should just get a [[wiki-link|rel:depends-on]] reference.\n\n"
        "Do NOT merge if:\n"
        "- Different topics that happen to share keywords\n"
        "- One is _index and the other is overview/design\n"
        "- Both are already well-separated\n\n"
        'Reply with JSON array: [{"source":"name1","target":"name2","reason":"..."},...]\n\n'
        + "\n".join(all_candidates)
    )

    reply = _call_llm(prompt, max_tokens=500, temperature=0.2, timeout=60)
    if reply.startswith("__LLM_ERROR__"):
        return [{"error": reply.split(":", 1)[1]}]

    am = re.search(r"\[.*?\]", reply, re.DOTALL)
    if not am:
        return []
    try:
        merges = json.loads(am.group())
    except json.JSONDecodeError:
        return []

    results = []
    cache_by_name = {fm["name"]: fm for fm in fm_cache}

    for m in merges:
        src_name = m.get("source", "")
        tgt_name = m.get("target", "")
        reason = m.get("reason", "")
        src_fm = cache_by_name.get(src_name)
        tgt_fm = cache_by_name.get(tgt_name)
        if not src_fm or not tgt_fm:
            results.append({"source": src_name, "target": tgt_name,
                            "error": "source or target not found"})
            continue

        try:
            src_raw = _read_file_safe(src_fm["path"])
            tgt_raw = _read_file_safe(tgt_fm["path"])

            # idempotency: skip if already merged (prevents repeated appends across audit runs)
            if re.search(rf"<!--\s*merged\s+from\s+{re.escape(src_name)}\s*-->", tgt_raw):
                results.append({"source": src_name, "target": tgt_name,
                                "reason": "already merged"})
                continue

            src_body_m = FM_RE.match(src_raw)
            src_body = src_body_m.group(2).strip() if src_body_m else src_raw
            merged = tgt_raw.rstrip() + "\n\n"
            merged += f"<!-- merged from {src_name} -->\n{src_body}\n"
            _atomic_write(tgt_fm["path"], merged)
            _write_audit_record(src_fm["path"], "merged",
                                f"已合并至 {tgt_name}: {reason}")
            _write_audit_record(tgt_fm["path"], "consolidated",
                                f"合并自 {src_name}: {reason}")
            results.append({"source": src_name, "target": tgt_name, "reason": reason})
        except Exception as e:
            results.append({"source": src_name, "target": tgt_name, "error": str(e)})

    return results


def _detect_contradictions(fm_cache: list[dict]) -> list[dict]:
    ns_groups: dict[str, list[dict]] = {}
    for fm in fm_cache:
        ns = fm.get("namespace", "(global)")
        body = _strip_audit(fm.get("_body", ""))
        if not body or len(body) < 50:
            continue
        ns_groups.setdefault(ns, []).append(fm)

    candidates = []
    STOP_WORDS = {"这个", "那个", "什么", "如何",
                  "可以", "需要", "使用", "一个",
                  "没有", "已经", "如果", "因为",
                  "所以", "但是", "不是", "就是",
                  "并且", "其中", "之后"}

    for ns, entries in ns_groups.items():
        names = [e["name"] for e in entries if e.get("name")]
        bodies_dict = {}
        for e in entries:
            bodies_dict[e["name"]] = _strip_audit(e.get("_body", ""))
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                tokens_a = {t for t in re.split(r"[\s,　。.、/\-_:()\d]+",
                                                bodies_dict.get(a, ""))
                            if len(t) >= 3 and t not in STOP_WORDS}
                tokens_b = {t for t in re.split(r"[\s,　。.、/\-_:()\d]+",
                                                bodies_dict.get(b, ""))
                            if len(t) >= 3 and t not in STOP_WORDS}
                overlap = tokens_a & tokens_b
                if len(overlap) >= 2:
                    candidates.append((ns, a, b, list(overlap)[:5]))

    if not candidates:
        return []

    candidates.sort(key=lambda x: -len(x[3]))
    candidates = candidates[:10]

    prompt_lines = [
        "You are a memory contradiction auditor. Review each candidate pair.",
        "Two entries in the same namespace may CONTRADICT each other.",
        'Reply JSON array: [{"source":"name1","target":"name2","topic":"...",',
        '"claim1":"...","claim2":"...","verdict":"contradiction|consistent"}]',
        "",
    ]
    for ns, a, b, kw in candidates:
        fm_a = next((fm for fm in fm_cache if fm["name"] == a), {})
        fm_b = next((fm for fm in fm_cache if fm["name"] == b), {})
        body_a = _strip_audit(fm_a.get("_body", ""))[:800]
        body_b = _strip_audit(fm_b.get("_body", ""))[:800]
        prompt_lines.append(f"--- ns={ns} ---")
        prompt_lines.append(f"Entry A [{a}]: {body_a}")
        prompt_lines.append(f"Entry B [{b}]: {body_b}")
        prompt_lines.append("")

    reply = _call_llm("\n".join(prompt_lines), max_tokens=800, temperature=0.2, timeout=60)
    if reply.startswith("__LLM_ERROR__"):
        return [{"error": reply.split(":", 1)[1]}]

    am = re.search(r"\[.*?\]", reply, re.DOTALL)
    if not am:
        return []
    try:
        contradictions = json.loads(am.group())
    except json.JSONDecodeError:
        return []

    return [c for c in contradictions if c.get("verdict") == "contradiction"]


def _check_completeness(fm_cache: list[dict]) -> list[dict]:
    REQUIRED: dict[str, list[tuple]] = {
        "_index": [
            (r"(描述|简介|概述|项目|what|是什么)", "项目描述"),
            (r"(功能|能力|用途|purpose|负责|职责)", "功能/用途"),
        ],
        "overview": [
            (r"(范围|scope|组件|component|模块)", "范围/组件"),
            (r"(流程|数据流|data.?flow|调用链|链路)", "数据流/流程"),
        ],
        "deps": [
            (r"(依赖|依赖方|被.*?依赖|调用|使用.*?服务)", "依赖方"),
            (r"(接口|API|端口|URL|路径|topic|endpoint)", "接口"),
            (r"(影响|后果|如果.*?断|当.*?不可用|失败)", "影响范围"),
        ],
        "design": [
            (r"(为什么|原因|背景|动机|目的|decision|决策)", "决策原因"),
            (r"(方案|设计|架构|结构|备选|替代|alternative|权衡|trade.?off)", "方案/权衡"),
            (r"(约束|局限|条件|限制|前提|resource|time|scope)", "约束条件"),
            (r"(脆弱|薄弱|风险|弱点|脆弱点|易出问题)", "已知脆弱点"),
        ],
        "sop": [
            (r"(触发|条件|如果.*?时|当.*?时|在.*?情况下)", "触发条件"),
            (r"(原因|根因|为什么|因为|由于|导致|表现为)", "根因解释"),
            (r"(诊断|排查|检查|查.*?看.*?看|怎么.*?判断|判断)", "诊断思路"),
        ],
    }

    results = []
    for fm in fm_cache:
        seg = fm.get("segment", "")
        body = fm.get("_body", "")
        name = fm.get("name", "")
        if not body or seg not in REQUIRED:
            continue
        missing = [label for pat, label in REQUIRED[seg]
                   if not re.search(pat, body, re.IGNORECASE)]
        if missing:
            results.append({"name": name, "ns": fm.get("namespace", ""),
                            "segment": seg, "missing_elements": missing,
                            "check": "completeness"})
    return results


_CONFIG_FILES = [
    WORKSPACE_ROOT / ".claude" / "mcp.json",
    WORKSPACE_ROOT / "ops-daemon" / "config.yaml",
    Path.home() / ".claude" / "model_proxy.py",
    Path.home() / ".claude" / "litellm_config.yaml",
    Path.home() / ".claude" / "router.py",
    WORKSPACE_ROOT / "memory-mcp" / "_storage.py",
]

_KNOWN_VALID_PREFIXES = [
    str(WORKSPACE_ROOT) + "/",
    str(Path.home() / ".claude") + "/",
    "~/.claude/",
]
if sys.platform == "win32":
    _KNOWN_VALID_PREFIXES.append(str(Path.home() / "AppData") + "/")


def _is_valid_path_prefix(p: str) -> bool:
    return any(p.startswith(pref) for pref in _KNOWN_VALID_PREFIXES)


def _verify_file_paths(fm_cache: list[dict]) -> list[dict]:
    PATH_PAT = re.compile(
        r'(?:\b(?:[A-Za-z]:/|~/)'
        r'[\w\-.\\/()\[\]{} ]+?'
        r'(?:\.\w{1,4})\b)'
    )
    results = []
    for fm in fm_cache:
        body = fm.get("_body", "")
        name = fm.get("name", "")
        ns = fm.get("namespace", "")
        paths_found = set()
        for m in PATH_PAT.finditer(body):
            p = m.group(0).strip().rstrip(".,;:)]}")
            if not _is_valid_path_prefix(p):
                continue
            expanded = Path(p.replace("~", str(Path.home()), 1))
            if not expanded.exists():
                paths_found.add(p)
        if paths_found:
            results.append({"name": name, "ns": ns,
                            "broken_paths": list(sorted(paths_found)[:5]),
                            "check": "broken_path"})
    return results


def _run_single_model_audit(candidates: list[dict], model: str) -> list[dict]:
    guide_text = _build_segment_guide_text()
    results = []
    for fm in candidates:
        name = fm.get("name", "")
        ns = fm.get("namespace", "")
        age = round(_age_days(fm), 1)
        body = _strip_audit(fm.get("_body", ""))[:2500]
        sensitive = _sensitive_scan(body)
        sensitive_note = f"\nNOTE: regex matched {sensitive}" if sensitive else ""

        prompt = (
            "You are a memory quality auditor. Rate this entry.\n"
            "Do NOT approve by default.\nReply with JSON only.\n\n"
            "Memory segment definitions:\n"
            f"{guide_text}\n\n"
            "Dimensions:\n"
            "- valid: content is coherent, non-trivial, consistent\n"
            "- segment_correct: content belongs to the assigned segment\n"
            "- action: keep|demote|flag\n"
            'JSON: {"valid":bool,"segment_correct":bool,"action":"keep"|"demote"|"flag","reason":"short CN reason"}\n\n'
            f"name: {name}\nns: {ns}\nage: {age}d\n---\n{body}{sensitive_note}"
        )

        reply = _call_llm(prompt, model=model, max_tokens=300, temperature=0.3)
        if reply.startswith("__LLM_ERROR__"):
            results.append({"file": name, "error": reply.split(":", 1)[1]})
            continue
        jm = re.search(r"\{[^{}]*\}", reply)
        if not jm:
            results.append({"file": name, "error": "no JSON"})
            continue
        try:
            parsed = json.loads(jm.group())
        except json.JSONDecodeError:
            results.append({"file": name, "error": "JSON parse"})
            continue
        action = parsed.get("action", "keep")
        reason = parsed.get("reason", "")[:200]
        if action == "keep" and not parsed.get("valid", True):
            action = "demote"
        results.append({"file": name, "action": action, "reason": reason})
    return results


def _multi_model_audit(fm_cache: list[dict],
                       flash_results: list[dict] | None = None) -> list[dict]:
    dismissed = _load_dismissed()
    if flash_results is None:
        flash_results = _deep_audit(fm_cache)

    candidates = sorted(fm_cache, key=lambda fm: _age_days(fm))[:15]

    qwen_results = _run_single_model_audit(candidates, SECOND_MODEL)

    flash_by_name = {r["file"]: r for r in flash_results}
    qwen_by_name = {r["file"]: r for r in qwen_results}
    disagreements = []
    for fname, flash_r in flash_by_name.items():
        qwen_r = qwen_by_name.get(fname)
        if not qwen_r:
            continue
        flash_action = flash_r.get("action", "keep")
        qwen_action = qwen_r.get("action", "keep")
        flash_keep = flash_action in ("keep", None, "")
        qwen_keep = qwen_action in ("keep", None, "")
        if flash_keep != qwen_keep:
            disagreements.append({
                "file": fname,
                "flash_action": flash_action,
                "qwen_action": qwen_action,
                "flash_reason": flash_r.get("reason", ""),
                "qwen_reason": qwen_r.get("reason", ""),
                "check": "multi_model_disagreement",
            })
    return disagreements


# ═══════════════════════════════════════════════════════
# Task creation helpers
# ═══════════════════════════════════════════════════════

TASKS_FILE = WORKSPACE_ROOT / "tasks" / "index.json"


def _load_tasks() -> dict[str, dict]:
    """Return Record<id, entry>. Keeps id in the key, not the entry itself."""
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        # legacy array format — upgrade to object
        if isinstance(data, list):
            obj: dict[str, dict] = {}
            for t in data:
                tid = t.get("id") or _legacy_id(t)
                obj[tid] = t
            return obj
        return {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _legacy_id(entry: dict) -> str:
    """Generate an id for legacy array entries that lack one."""
    created = entry.get("created_at", "")
    if created and isinstance(created, str):
        prefix = created[:10].replace("-", "/")
    else:
        prefix = "unknown"
    summary = entry.get("summary", "untitled")[:20]
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in summary)
    return f"{prefix}/{slug}"


def _save_tasks(tasks: dict[str, dict]) -> None:
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_FILE.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TASKS_FILE)


def _next_seq(tasks: dict[str, dict], prefix: str) -> str:
    existing = {tid.split("/")[-1] for tid in tasks if tid.startswith(prefix)}
    n = 1
    while f"{n:03d}" in existing:
        n += 1
    return f"{prefix}{n:03d}"


def _has_pending_memory_review(tasks: dict[str, dict]) -> bool:
    return any(
        t.get("status") in ("pending", "in_progress")
        and t.get("summary", "").startswith("记忆体待处理")
        for t in tasks.values()
    )


def _create_memory_review_tasks(flag_items: list[dict], all_issues: list[dict] | None = None) -> int:
    total = len(flag_items) + (len(all_issues) if all_issues else 0)
    if total == 0:
        return 0
    tasks = _load_tasks()
    if _has_pending_memory_review(tasks):
        return 0

    today = date.today().isoformat()
    prefix = f"{today}/"
    seq = _next_seq(tasks, prefix)
    task_id = f"{prefix}{seq}memory-review-pending"

    names = ", ".join(r.get("file", "?") for r in flag_items[:5])
    names += "..." if len(flag_items) > 5 else ""
    summary = f"记忆体待处理 (待审核 {len(flag_items)} 条, 问题 {len(all_issues)} 条): {names}(共{total}项)"

    progress_lines = []
    if flag_items:
        progress_lines.append("=== 待审核（需运行 memory_review confirm 确认）===")
        for r in flag_items[:10]:
            progress_lines.append(f"- [{r['file']}] {r.get('reason','')[:60]}")
    if all_issues:
        progress_lines.append("=== 质量检查 ===")
        for item in all_issues[:20]:
            ck = item.get("check", "?")
            name = item.get("name") or item.get("file", "?")
            if ck == "completeness":
                detail = ", ".join(item.get("missing_elements", []))
            elif ck == "broken_path":
                detail = ", ".join(item.get("broken_paths", []))
            else:
                detail = item.get("reason", "") or item.get("detail", "")
            progress_lines.append(f"- [{name}] {ck}: {detail[:60]}"[:120])

    tasks[task_id] = {
        "summary": summary,
        "type": "task",
        "source": "ops-daemon",
        "priority": "medium" if flag_items else "low",
        "status": "pending",
        "progress": "\n".join(progress_lines[:30]),
        "created_at": today,
    }
    _save_tasks(tasks)
    return 1


# ═══════════════════════════════════════════════════════
# Review report writing
# ═══════════════════════════════════════════════════════

REVIEW_JSON = MEMORY_DIR / "review-report.json"


def _write_review_json(flagged: list[dict], all_issues: list[dict] | None = None) -> None:
    """Write L3 audit results to review-report.json (auto-execute + human-confirm model).
    flagged items that pass action=keep/demote are applied immediately but recorded for review.
    """
    dismissed = _load_dismissed()
    pending = []
    for item in flagged:
        name = item.get("file", "")
        if name in dismissed:
            continue
        pending.append({
            "name": name,
            "namespace": item.get("namespace", ""),
            "action": item.get("action", "keep"),
            "reason": (item.get("reason") or item.get("error", ""))[:120],
        })
    if all_issues:
        for issue in all_issues:
            name = issue.get("name") or issue.get("file", "")
            if name in dismissed:
                continue
            pending.append({
                "name": name,
                "namespace": issue.get("ns", ""),
                "action": issue.get("check", "issue"),
                "reason": str(issue.get("detail", ""))[:120],
            })
    _atomic_write(REVIEW_JSON, json.dumps(pending, ensure_ascii=False, indent=2) + "\n")


def _clear_review_json() -> None:
    if REVIEW_JSON.exists():
        REVIEW_JSON.unlink()


# ═══════════════════════════════════════════════════════
# HTML formatting (for daily_report consumption)
# ═══════════════════════════════════════════════════════

def format_html(result: dict) -> str:
    """Convert health check result dict to HTML fragment for daily report."""
    parts = []
    stats = result.get("stats", {})
    orphans = result.get("orphans", [])
    broken = result.get("broken_links", [])
    cleaned = result.get("cleaned", 0)
    fixed_links = result.get("fixed_links", 0)

    summary_parts = [f"文件 {stats.get('files_total', 0)} 个"]
    total_lines = stats.get("lines_total", 0)
    if total_lines:
        summary_parts.append(f"共 {total_lines:,} 行")
    if cleaned:
        summary_parts.append(f'<span class="tag-ok">清理 {cleaned} 个孤儿文件</span>')
    elif orphans:
        summary_parts.append(f'<span class="tag-info">0 个孤儿文件待清理（均不足 24h）</span>')
    if fixed_links:
        summary_parts.append(f'<span class="tag-ok">修复 {fixed_links} 个损坏链接</span>')
    elif broken:
        summary_parts.append(f'<span class="tag-info">{len(broken)} 个损坏链接</span>')

    parts.append(" | ".join(summary_parts))

    # ── by_domain distribution table (L1, no LLM needed) ──
    by_domain = stats.get("by_domain", {})
    if by_domain:
        dom_rows = "".join(
            f'<tr><td>{seg}</td><td class="num">{count}</td></tr>\n'
            for seg, count in sorted(by_domain.items(), key=lambda x: -x[1])
        )
        parts.append(
            f'<br><div style="font-size:12px;font-weight:700;color:#2d3436;margin-top:8px">'
            f'段分布</div>'
            f'<table class="data-table">'
            f'<col style="width:70%"><col style="width:30%" class="num">'
            f'<tr><th>段</th><th class="num">文件数</th></tr>{dom_rows}</table>'
        )

    # ── oversized files (L1) ──
    oversized = stats.get("oversized", [])
    if oversized:
        o_rows = "".join(
            f'<tr><td class="mem-name">{o["stem"][:35]}</td>'
            f'<td class="num">{o["lines"]}</td>'
            f'<td style="font-size:11px;color:#e17055">建议拆分</td></tr>\n'
            for o in oversized
        )
        parts.append(
            f'<br><div style="font-size:12px;font-weight:700;color:#e17055;margin-top:8px">'
            f'超大文件 ({len(oversized)} 个)'
            f'</div>'
            f'<table class="data-table">'
            f'<col style="width:50%"><col style="width:20%" class="num"><col style="width:30%">'
            f'<tr><th>文件</th><th class="num">行数</th><th>建议</th></tr>{o_rows}</table>'
        )

    audit_results = result.get("audit_results", [])
    flag_items = result.get("flag_items", [])
    consolidations = result.get("consolidations", [])
    contradictions = result.get("contradictions", [])
    completeness_issues = result.get("completeness_issues", [])
    path_issues = result.get("path_issues", [])
    model_disagreements = result.get("model_disagreements", [])

    auto_items = [r for r in audit_results if r.get("action") in ("keep", "demote", None)]

    if auto_items:
        a_rows = ""
        for r in auto_items[:10]:
            action = r.get("action", "")
            if not action and "error" in r:
                action = "error"
            cls = "ok" if action == "keep" else ("warn" if action == "demote" else "err")
            reason = (r.get("reason") or r.get("error", ""))[:80]
            a_rows += (
                f'<tr><td class="mem-name">{r["file"][:30]}</td>'
                f'<td class="{cls}">{action}</td>'
                f'<td style="font-size:12px;color:#636e72">{reason}</td></tr>\n'
            )
        if a_rows:
            parts.append(
                f'<br><table class="data-table">'
                f'<col style="width:35%"><col style="width:10%"><col style="width:55%">'
                f'<tr><th>文件</th><th>操作</th><th>说明</th></tr>{a_rows}</table>'
            )

    if flag_items:
        flag_rows = "".join(
            f'<tr><td class="mem-name">{r["file"][:30]}</td>'
            f'<td style="font-size:12px;color:#636e72">{(r.get("reason") or r.get("error",""))[:60]}</td></tr>\n'
            for r in flag_items
        )
        parts.append(
            f'<br><div style="font-size:12px;font-weight:700;color:#e17055;margin-top:8px">'
            f'待审核 ({len(flag_items)} 条) '
            f'<span style="font-weight:400;font-size:11px;color:#b2bec3">'
            f'需确认后执行 memory_review confirm</span></div>'
            f'<table class="data-table">'
            f'<col style="width:40%"><col style="width:60%">'
            f'<tr><th>文件</th><th>原因</th></tr>{flag_rows}</table>'
        )

    if consolidations:
        c_rows = "".join(
            f'<tr><td class="mem-name">{c["source"][:20]}</td>'
            f'<td class="mem-name">{c["target"][:20]}</td>'
            f'<td style="font-size:12px;color:#636e72">'
            f'{(c.get("reason") or c.get("error",""))[:60]}</td></tr>\n'
            for c in consolidations[:5]
        )
        parts.append(
            f'<br><div style="font-size:12px;font-weight:700;color:#6c5ce7;margin-top:8px">'
            f'记忆合并 ({len(consolidations)} 对)</div>'
            f'<table class="data-table">'
            f'<tr><th>来源</th><th>目标</th><th>说明</th></tr>{c_rows}</table>'
        )

    if contradictions:
        x_rows = "".join(
            f'<tr><td class="mem-name">{c["source"][:20]}</td>'
            f'<td class="mem-name">{c["target"][:20]}</td>'
            f'<td style="font-size:12px;color:#d63031">{c.get("topic","")[:40]}</td>'
            f'<td style="font-size:12px;color:#636e72">{c.get("claim1","")[:40]} vs {c.get("claim2","")[:40]}</td></tr>\n'
            for c in contradictions
        )
        parts.append(
            f'<br><div style="font-size:12px;font-weight:700;color:#d63031;margin-top:8px">'
            f'记忆矛盾 ({len(contradictions)} 处)</div>'
            f'<table class="data-table">'
            f'<col style="width:25%"><col style="width:25%"><col style="width:20%"><col style="width:30%">'
            f'<tr><th>来源</th><th>目标</th><th>主题</th><th>矛盾</th></tr>{x_rows}</table>'
        )

    if completeness_issues:
        comp_rows = "".join(
            f'<tr><td class="mem-name">{c["name"][:28]}</td>'
            f'<td style="font-size:12px;color:#636e72">{c["segment"]}</td>'
            f'<td style="font-size:12px;color:#d63031">{", ".join(c.get("missing_elements",[]))}</td></tr>\n'
            for c in completeness_issues[:12]
        )
        parts.append(
            f'<br><div style="font-size:12px;font-weight:700;color:#e17055;margin-top:8px">'
            f'内容不完整 ({len(completeness_issues)} 条)</div>'
            f'<table class="data-table">'
            f'<col style="width:30%"><col style="width:15%"><col style="width:55%">'
            f'<tr><th>文件</th><th>段</th><th>缺少要素</th></tr>{comp_rows}</table>'
        )

    if path_issues:
        p_rows = "".join(
            f'<tr><td class="mem-name">{p["name"][:28]}</td>'
            f'<td style="font-size:11px;color:#d63031">{p["ns"]}</td>'
            f'<td style="font-size:11px;color:#636e72">{", ".join(p.get("broken_paths",[])[:3])}</td></tr>\n'
            for p in path_issues[:8]
        )
        parts.append(
            f'<br><div style="font-size:12px;font-weight:700;color:#d63031;margin-top:8px">'
            f'路径不可达 ({len(path_issues)} 条)</div>'
            f'<table class="data-table">'
            f'<col style="width:30%"><col style="width:15%"><col style="width:55%">'
            f'<tr><th>文件</th><th>空间</th><th>路径</th></tr>{p_rows}</table>'
        )

    if model_disagreements:
        m_rows = "".join(
            f'<tr><td class="mem-name">{d["file"][:28]}</td>'
            f'<td><span class="warn">{d["flash_action"]}</span></td>'
            f'<td><span class="err">{d["qwen_action"]}</span></td>'
            f'<td style="font-size:11px;color:#636e72">flash: {d["flash_reason"][:40]}</td></tr>\n'
            for d in model_disagreements[:10]
        )
        parts.append(
            f'<br><div style="font-size:12px;font-weight:700;color:#e17055;margin-top:8px">'
            f'模型审核分歧 ({len(model_disagreements)} 条)</div>'
            f'<table class="data-table">'
            f'<col style="width:28%"><col style="width:10%"><col style="width:12%"><col style="width:50%">'
            f'<tr><th>文件</th><th>flash</th><th>qwen</th><th>原因</th></tr>{m_rows}</table>'
        )

    return "".join(parts)


# ═══════════════════════════════════════════════════════
# Main entry
# ═══════════════════════════════════════════════════════

def run_memory_health(llm_enabled: bool = False) -> dict:
    """Run memory health check and return structured result dict.

    Auto-cleanup:
      L1 — orphan files (>24h) deleted, broken links removed
      L3 — merge candidates applied, demote_count auto-incremented
      flag — REVIEW REQUIRED (shown in report, not auto-applied)

    Returns:
        dict with keys: stats, orphans, broken_links, overview,
        audit_results, flag_items, consolidations, contradictions,
        completeness_issues, path_issues, model_disagreements,
        cleaned, fixed_links
    """
    fm_cache = _build_fm_cache()
    if not fm_cache:
        return {"error": "memory directory not found", "stats": {}}

    stats = _compute_stats(fm_cache)
    orphans = _check_orphans(fm_cache)
    broken = _check_broken_links(fm_cache)
    cleaned = _cleanup_orphans(orphans)
    fixed_links = _fix_broken_links(broken, fm_cache)
    segment_refs = _check_segment_references(fm_cache)

    result: dict[str, Any] = {
        "stats": stats,
        "orphans": orphans,
        "broken_links": broken,
        "cleaned": len(cleaned),
        "fixed_links": fixed_links,
        "segment_refs": segment_refs,
        "audit_results": [],
        "flag_items": [],
        "consolidations": [],
        "contradictions": [],
        "completeness_issues": [],
        "path_issues": [],
        "model_disagreements": [],
    }

    if llm_enabled:
        audit_results = _deep_audit(fm_cache)
        consolidations = _consolidation_pass(fm_cache)
        result["audit_results"] = audit_results
        result["flag_items"] = [r for r in audit_results if r.get("action") == "flag"]
        result["consolidations"] = consolidations
        result["contradictions"] = _detect_contradictions(fm_cache)
        result["completeness_issues"] = _check_completeness(fm_cache)
        result["path_issues"] = _verify_file_paths(fm_cache)
        result["model_disagreements"] = _multi_model_audit(fm_cache, flash_results=audit_results)

        all_issues = (result["completeness_issues"] + result["path_issues"]
                      + result["model_disagreements"])
        _write_review_json(result["flag_items"], all_issues)
        _create_memory_review_tasks(result["flag_items"], all_issues)

    return result


def health_snapshot() -> dict:
    """Lightweight L1 snapshot for extraction prompt (no auto-cleanup, no LLM)."""
    fm_cache = _build_fm_cache()
    return {
        "segment_refs": _check_segment_references(fm_cache),
        "stats": _compute_stats(fm_cache),
    }
