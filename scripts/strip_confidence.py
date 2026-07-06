"""Batch remove `confidence:` lines from all .md frontmatter files under MEMORY_DIR."""
import re, sys
from pathlib import Path
MEMORY_DIR = MEMORY_ROOT
pattern = re.compile(r"^confidence: .*$", re.MULTILINE)

total = 0
changed = 0

for f in sorted(MEMORY_DIR.rglob("*.md")):
    relative = f.relative_to(MEMORY_DIR)
    # Skip MEMORY.md index files
    if f.name == "MEMORY.md":
        continue

    raw = f.read_text(encoding="utf-8")

    # Only process files with frontmatter (starts with ---)
    if not raw.startswith("---"):
        continue

    # Find the closing ---
    end = raw.find("---", 3)
    if end == -1:
        continue

    frontmatter = raw[3:end]
    body = raw[end + 3:]

    new_fm, n = pattern.subn("", frontmatter)
    if n == 0:
        continue

    # Clean up blank lines caused by removal
    new_fm = re.sub(r"\n{3,}", "\n\n", new_fm)

    result = f"---{new_fm}---{body}"
    f.write_text(result, encoding="utf-8")
    changed += 1
    print(f"  [{changed}] {relative}  ({n} line(s) removed)")

    total += n

print(f"\nDone. {changed} files changed, {total} confidence lines removed.")
