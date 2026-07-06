"""Minimize all .md memory frontmatter to only `name:` field."""
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import MEMORY_DIR as MEMORY_ROOT
MEMORY_DIR = MEMORY_ROOT
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL | re.MULTILINE)

total = 0
changed = 0

for f in sorted(MEMORY_DIR.rglob("*.md")):
    relative = f.relative_to(MEMORY_DIR)
    if f.name == "MEMORY.md":
        continue

    raw = f.read_text(encoding="utf-8")
    m = FM_RE.match(raw)
    if not m:
        continue

    body = raw[m.end():]
    meta_lines = m.group(1).strip().split("\n")

    # Extract name
    name = f.stem
    for line in meta_lines:
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip()
            break

    new_fm = f"---\nname: {name}\n---\n"
    if new_fm == raw[:len(new_fm)]:
        continue  # already minimal

    f.write_text(new_fm + body.strip() + "\n", encoding="utf-8")
    changed += 1
    print(f"  [{changed}] {relative}")

print(f"\nDone. {changed} files minimized.")
