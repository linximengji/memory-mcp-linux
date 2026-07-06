"""One-shot migration: fix scope/domain/descriptions/placement, then rebuild MEMORY.md."""
import re, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import MEMORY_ROOT, WORKSPACE_ROOT
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL | re.MULTILINE)

VALID_SCOPES = {
    "project", "infra", "reference", "workflow",
    "feedback", "engineering", "bugfix", "decision",
}

# Mappings from bad → good
SCOPE_FIX = {
    "ClaudeProjects": "project",
    "D:/ClaudeProjects": "project",
    "global": "global",  # keep as-is
}

# scope values that are never legal scope names, always domain-like
SCOPE_DOMAIN_VALUES = {
    "feature": "project",
    "general": "project",
}

DOMAIN_TO_SCOPE = {
    "infra": "infra", "reference": "reference", "workflow": "workflow",
    "engineering": "engineering", "feedback": "feedback",
    "bugfix": "bugfix", "decision": "decision",
    "project": "project", "convention": "project",
    "pattern": "project", "feature": "project",
    "proxy": "project", "ops-dashboard": "project",
    "tool": "reference", "research": "reference",
}


def git_commit(msg):
    subprocess.run(["git", "add", "-A"], cwd=str(MEMORY_ROOT), capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=str(MEMORY_ROOT), capture_output=True)


def parse_file(path):
    raw = path.read_text(encoding="utf-8")
    m = FM_RE.match(raw)
    if not m:
        return None, raw, None
    fm = {}
    for line in m.group(1).splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2:
            fm[parts[0].strip()] = parts[1].strip()
    return fm, raw, m


def write_file(path, fm, body):
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body.strip() + "\n", encoding="utf-8")


def fix_scope(fm):
    old = fm.get("scope", "")
    old_domain = fm.get("domain", "")
    pid = fm.get("project_id", "").strip()
    if old in SCOPE_FIX:
        fixed = SCOPE_FIX[old]
        if fixed == "global" and not pid:
            return DOMAIN_TO_SCOPE.get(old_domain, "project")
        return fixed
    if old in SCOPE_DOMAIN_VALUES:
        return SCOPE_DOMAIN_VALUES[old]
    if old in VALID_SCOPES:
        return old
    if not pid:
        return DOMAIN_TO_SCOPE.get(old_domain, "project")
    return "project"


def fix_domain(fm, pid):
    """Derive domain from project_id."""
    if pid:
        return pid
    return "(global)"


def guess_description(fm, body):
    """Extract a description from body text if frontmatter description is empty."""
    desc = fm.get("description", "").strip()
    if desc and len(desc) >= 5:
        return desc
    # Try to extract first meaningful line from body
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("-") and len(line) >= 10:
            # Take first sentence or first 80 chars
            sentence = line.split("。")[0].split(".")[0].strip()
            if len(sentence) >= 5:
                return sentence[:80]
    return "[NEEDS REVIEW]"


def main():
    print("=== Memory Migration ===")
    print(f"Memory root: {MEMORY_ROOT}")

    # 0. Backup commit
    print("\n[0] Git commit before migration...")
    git_commit("backup before migration")

    changes = []
    moved = []
    desc_filled = []

    for mp in sorted(MEMORY_ROOT.rglob("*.md")):
        if mp.name in ("MEMORY.md", "review-report.md"):
            continue

        fm, raw, match = parse_file(mp)
        if fm is None:
            print(f"  SKIP {mp.name}: cant parse frontmatter")
            continue

        pid = fm.get("project_id", "").strip()
        old_scope = fm.get("scope", "")
        old_domain = fm.get("domain", "")
        old_desc = fm.get("description", "")

        # Fix scope
        new_scope = fix_scope(fm)
        if new_scope != old_scope:
            fm["scope"] = new_scope

        # Fix domain
        new_domain = fix_domain(fm, pid)
        if new_domain != old_domain:
            fm["domain"] = new_domain

        # Fix description
        body = match.group(2) if match else ""
        new_desc = guess_description(fm, body)
        if new_desc != old_desc:
            fm["description"] = new_desc
            desc_filled.append((mp.name, old_desc, new_desc))

        # Check if file needs to move
        rel = mp.relative_to(MEMORY_ROOT)
        target_dir = MEMORY_ROOT
        if pid and new_scope in VALID_SCOPES:
            target_dir = MEMORY_ROOT / "projects" / pid

        should_be = target_dir / mp.name

        # Write updated frontmatter
        write_file(mp, fm, body)

        if should_be != mp:
            moved.append((str(rel), str(should_be.relative_to(MEMORY_ROOT))))

        if new_scope != old_scope or new_domain != old_domain or new_desc != old_desc:
            changes.append(f"  {mp.name}: scope={old_scope}->{new_scope} domain={old_domain}->{new_domain} desc={'[empty]' if not old_desc else 'updated'}")

    # Apply moves
    for src_rel, dst_rel in moved:
        src = MEMORY_ROOT / src_rel
        dst = MEMORY_ROOT / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            print(f"  CONFLICT {dst_rel} already exists, skipping move of {src_rel}")
            continue
        shutil.move(str(src), str(dst))
        print(f"  MOVE {src_rel} -> {dst_rel}")

    # Rebuild index
    print("\n[Rebuild] MEMORY.md...")
    sys.path.insert(0, str(WORKSPACE_ROOT / "memory-mcp"))
    import _storage
    _storage._rebuild_index()

    # Summary
    print(f"\n=== Migration Complete ===")
    print(f"Metadata changes: {len(changes)}")
    for c in changes:
        print(c)
    print(f"Files moved: {len(moved)}")
    for s, d in moved:
        print(f"  {s} -> {d}")
    print(f"Descriptions filled: {len(desc_filled)}")
    for name, old, new in desc_filled:
        print(f"  {name}: '{old}' -> '{new}'")

    # Post-migration commit
    print("\n[Final] Git commit after migration...")
    git_commit("migration: fix scope/domain/descriptions, rebuild MEMORY.md")
    print("Done.")


if __name__ == "__main__":
    main()
