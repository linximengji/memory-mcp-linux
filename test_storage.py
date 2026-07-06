"""Verify memory-mcp storage CRUD and search (simplified frontmatter)."""
import sys, os, time, re, json, urllib.request
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding='utf-8')

from pathlib import Path
import _storage as s

passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL [{name}]: {detail}")

UNIQ = str(int(time.time() * 1000) % 1000000000)
TEST_NS = "memory-mcp"

def write_test_mem(name, path, content, namespace=TEST_NS, segment="_index"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n\n{content}\n", encoding="utf-8")

# ── memory_store + memory_delete ──
print("── memory_store & delete ──")

tn = f"del-{UNIQ}"
r = s.memory_store(tn, "delete test content unique enough", namespace=TEST_NS, segment="_index")
check("store succeeds", tn in r, r)
fp = s.MEMORY_ROOT / TEST_NS / f"{tn}.md"
check("file created", fp.exists(), str(fp))

r = s.memory_get(tn)
check("memory_get returns content", "delete test" in r, r[:100])

r = s.memory_delete(tn)
check("delete returns name", tn in r, r)
check("file deleted", not fp.exists())

r2 = s.memory_delete(f"nonex-{UNIQ}")
check("nonexistent returns error", "error" in r2.lower(), r2)

# ── duplicate name detection ──
print("── duplicate name detection ──")
tn2 = f"dup-{UNIQ}"
tp2 = s.MEMORY_ROOT / TEST_NS / f"{tn2}.md"
write_test_mem(tn2, tp2, content="unique content for duplicate test")
r = s.memory_store(tn2, "different content to avoid jaccard match")
check("rejects exact duplicate name", "already exists" in r, r)
tp2.unlink()

# ── memory_update ──
print("── memory_update ──")
tn3 = f"upd-{UNIQ}"
s.memory_store(tn3, "original content", namespace=TEST_NS)
r = s.memory_update(tn3, content="updated content")
check("update succeeds", "updated" in r, r)
r2 = s.memory_get(tn3)
check("content updated on disk", "updated content" in r2, r2[:200])
s.memory_delete(tn3)

# ── _keyword_score ──
print("── _keyword_score ──")
mem = {"name": "restart-procedures", "content": "restart the proxy service",
       "namespace": "model-proxy"}
score = s._keyword_score("restart", mem, "model-proxy")
check("keyword name match with namespace boost", abs(score - 1.0 * 1.2) < 0.01, f"score={score}")

mem_body = {"name": "some-name", "content": "redis connection pool configuration",
            "namespace": "ops-daemon"}
score_body = s._keyword_score("redis", mem_body, "ops-daemon")
check("body hit scores 0.9 * namespace_boost", abs(score_body - 0.9 * 1.2) < 0.01, f"score={score_body}")

# ── memory_recall ──
print("── memory_recall ──")
r_empty = s.memory_recall("", top_k=5)
check("empty query returns results", len(r_empty) > 0, f"got {len(r_empty)}")
check("results have name/score", "name" in r_empty[0] and "score" in r_empty[0], str(r_empty[0])[:200])

# ── MEMORY.md rebuild ──
print("── MEMORY.md rebuild ──")
s.memory_rebuild_index()
idx = (s.MEMORY_ROOT / "MEMORY.md").read_text(encoding="utf-8")
check("has entries", len(idx.splitlines()) > 5)
check("memory-mcp in index", "memory-mcp" in idx)

# ── _resolve_path ──
print("── _resolve_path ──")
p = s._resolve_path("_index", "memory-mcp")
check("resolve ns/segment", p is not None and p.name == "_index.md", str(p))
p2 = s._resolve_path("_index")
check("resolve segment across ns", p2 is not None, str(p2))

# ── _llm_rerank (if proxy reachable) ──
print("── _llm_rerank ──")
try:
    req = urllib.request.Request("http://localhost:4000/health", method="GET")
    urllib.request.urlopen(req, timeout=2)
    proxy_ok = True
except Exception:
    proxy_ok = False

if proxy_ok:
    cands = [
        {"name": "restart-procedures", "content": "restart the proxy service",
         "namespace": "model-proxy"},
        {"name": "model-proxy-index", "content": "proxy routing architecture",
         "namespace": "model-proxy"},
    ]
    ranked = s._llm_rerank("how to restart proxy", cands, 2)
    if ranked:
        top_name = ranked[0][1]["name"] if ranked else "none"
        check("LLM ranks restart-procedures first", top_name == "restart-procedures",
              f"got {[(s,m['name']) for s,m in ranked]}")
    else:
        check("LLM reachable but rerank failed", False, "returned None")
else:
    print("  (proxy not reachable, skipping LLM test)")
    check("proxy not reachable", False, "cannot test LLM rerank")

# ── memory_update namespace-aware matching ──
print("── memory_update namespace-aware ──")
NS1 = f"ns-a-{UNIQ}"
NS2 = f"ns-b-{UNIQ}"
r = s.memory_store("buga-a", "unique routing configuration for namespace A", namespace=NS1)
check("store to NS1", "stored" in r, r)
r = s.memory_store("buga-b", "redis cache topology for namespace B", namespace=NS2)
check("store to NS2", "stored" in r, r)

r = s.memory_update("buga-a", content="routing updated", namespace=NS1)
check("update with namespace succeeds", "updated" in r, r)
r1 = s.memory_get("buga-a", namespace=NS1)
check("ns-a file was updated", "routing updated" in r1, r1[:200])
r2 = s.memory_get("buga-b", namespace=NS2)
check("ns-b file was NOT updated", "namespace B" in r2, r2[:200])

r3 = s.memory_update("buga-a", content="should fail", namespace="nonexistent-ns")
check("update with bad namespace errors", "error" in r3.lower(), r3)

r4 = s.memory_update("buga-a", content="fallback update")
check("no-namespace fallback works", "updated" in r4, r4)

s.memory_delete("buga-a")
s.memory_delete("buga-b")

# ── memory_upsert ──
print("── memory_upsert ──")
upsert_n = f"upsert-{UNIQ}"
r = s.memory_upsert(upsert_n, "test upsert fresh content", namespace=TEST_NS)
check("upsert new", "stored" in r, r)

r = s.memory_upsert(upsert_n, "test upsert updated content", namespace=TEST_NS)
check("upsert existing", "updated" in r, r)

r = s.memory_get(upsert_n)
check("upsert content updated", "updated content" in r, r[:200])
s.memory_delete(upsert_n)

# ── frontmatter segment test ──
print("── frontmatter segment ──")
seg_n = f"seg-{UNIQ}"
s.memory_store(seg_n, "segment test content", namespace=TEST_NS, segment="sop")
seg_raw = s.memory_get(seg_n)
check("segment written to frontmatter", "segment: sop" in seg_raw, seg_raw[:200])
# Update should preserve segment
s.memory_update(seg_n, content="segment test updated")
seg_raw2 = s.memory_get(seg_n)
check("segment preserved after update", "segment: sop" in seg_raw2, seg_raw2[:200])
check("content updated", "test updated" in seg_raw2, seg_raw2[:200])
s.memory_delete(seg_n)

# ── memory_delete with namespace ──
print("── memory_delete namespace-aware ──")
del_ns = f"del-ns-a-{UNIQ}"
del_n = f"del-target-{UNIQ}"
s.memory_store(del_n, "delete ns test", namespace=del_ns)
r_del_ok = s.memory_delete(del_n, namespace=del_ns)
check("delete with ns succeeds", "deleted" in r_del_ok, r_del_ok)
r_del_bad = s.memory_delete(del_n, namespace="nonexistent-ns")
check("delete with bad ns errors", "not found" in r_del_bad.lower(), r_del_bad)

# ── memory_review JSON-based ──
print("── memory_review structured ──")
# Store a test result in review-report.json
import json
from pathlib import Path
rev_path = s.MEMORY_ROOT / "review-report.json"
test_review = [{"name": "test-mem", "namespace": "memory-mcp", "action": "flag", "reason": "test reason"}]
rev_path.write_text(json.dumps(test_review, ensure_ascii=False), encoding="utf-8")
r_list = s.memory_review("list")
check("review list shows item", "test-mem" in r_list, r_list[:120])
r_confirm = s.memory_review("confirm", "test-mem", "memory-mcp")
check("review confirm succeeds", "confirmed" in r_confirm, r_confirm)
r_list2 = s.memory_review("list")
check("review list empty after confirm", not r_list2 or "no pending" in r_list2, str(r_list2))
# dismiss test
rev_path.write_text(json.dumps(test_review, ensure_ascii=False), encoding="utf-8")
r_dismiss = s.memory_review("dismiss", "test-mem", "memory-mcp")
check("review dismiss succeeds", "dismissed" in r_dismiss, r_dismiss)
r_list3 = s.memory_review("list")
check("review list empty after dismiss", not r_list3 or "no pending" in r_list3, str(r_list3))
# cleanup
if rev_path.exists():
    rev_path.unlink()

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
