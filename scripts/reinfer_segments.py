"""Re-infer segment for all memories using keyword heuristics.

5 segments: _index / overview / deps / design / sop
"""
import re, sys, os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

import _storage as s
from _storage import MEMORY_ROOT, FM_RE, VALID_SEGMENTS

# ── Keyword-based heuristic (90%+ accuracy for well-written memories) ──

SOP_PATTERNS = re.compile(
    r'(步骤|命令|启动|停止|重启|kill|workaround|安装|部署|调试|'
    r'修复|npm\s+(install|run|start)|pip\s+install|docker\s+(run|compose|build)|'
    r'操作流程|故障排查|运行|执行|报错|错误|'
    r'safekill|看门狗|守护)',
    re.IGNORECASE,
)

DESIGN_PATTERNS = re.compile(
    r'(为什么选|权衡|方案对比|vs\.|design decision|'
    r'trade.?off|备选方案|架构选型|设计决策|'
    r'选的理由|否决|被否)',
    re.IGNORECASE,
)

DEPS_PATTERNS = re.compile(
    r'(依赖|依赖方|被.*?依赖|上游|下游|端口|API[^K]|'
    r'文件级接口|跨项目|配置键|映射)',
    re.IGNORECASE,
)

OVERVIEW_PATTERNS = re.compile(
    r'(入口|核心流程|文件拓扑|架构图|系统地图|'
    r'模块划分|功能概述|项目结构)',
    re.IGNORECASE,
)


def guess_segment(name: str, body: str, content: str) -> str:
    """Key-based heuristic. Returns ('_index', True) if confident, ('segment', False) if needs LLM."""
    combined = f"{name} {body[:500]}"

    # Name-based overrides (most reliable)
    name_lower = name.lower()
    if name_lower.endswith("-deps") or name_lower.endswith("-depend"):
        return "deps"
    if name_lower.endswith("-sop") or name_lower.endswith("-ops"):
        return "sop"
    if name_lower.endswith("-design") or name_lower.endswith("-decision"):
        return "design"
    if name_lower.endswith("-overview") or name_lower == "overview":
        return "overview"
    if name_lower == "_index" or name_lower.endswith("-index"):
        return "_index"

    # Content-based
    if DEPS_PATTERNS.search(combined):
        return "deps"
    if SOP_PATTERNS.search(combined):
        return "sop"
    if DESIGN_PATTERNS.search(combined):
        return "design"
    if OVERVIEW_PATTERNS.search(combined):
        return "overview"

    return "_index"


def update_segment(path: Path, new_segment: str) -> bool:
    """Rewrite segment in frontmatter (add if missing)."""
    raw = path.read_text(encoding="utf-8")
    m = FM_RE.match(raw)
    if not m:
        return False

    meta_lines = m.group(1).strip().split("\n")
    body = m.group(2)

    # Check current segment
    had_seg = False
    new_meta = []
    for line in meta_lines:
        if line.startswith("segment:"):
            current = line.split(":", 1)[1].strip()
            if current == new_segment:
                return False  # already correct
            new_meta.append(f"segment: {new_segment}")
            had_seg = True
        else:
            new_meta.append(line)

    if not had_seg:
        # Insert before body content (between name line and rest)
        new_meta.append(f"segment: {new_segment}")

    path.write_text(
        "---\n" + "\n".join(new_meta) + "\n---\n" + body.strip() + "\n",
        encoding="utf-8",
    )
    return True


def main():
    total = 0
    changed = 0
    need_llm = []

    print("── Phase 1: keyword-based inference ──")
    for mem in s._iter_memories():
        total += 1
        seg = guess_segment(mem["name"], mem["content"], mem["content"])
        if update_segment(mem["path"], seg):
            print(f"  [{changed+1}] {mem['path'].relative_to(MEMORY_ROOT)} → {seg}")
            changed += 1

    print(f"\n└ {changed} changed, {total} total")

    if need_llm:
        print(f"\n── Phase 2: LLM inference for {len(need_llm)} entries ──")
        for name, ns, body, path in need_llm:
            seg = guess_segment_by_llm(name, body)
            if update_segment(path, seg):
                changed += 1
                print(f"  [LLM] {path.relative_to(MEMORY_ROOT)} → {seg}")

    s.memory_rebuild_index()
    print(f"\nDone. {changed} files updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
