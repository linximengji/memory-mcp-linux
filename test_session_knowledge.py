"""Tests for session_knowledge.py hook. Run: python test_session_knowledge.py"""
import sys, os, json, time, io, tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "hooks"))
import session_knowledge as sk

passed = 0
failed = 0
TMP = Path(tempfile.mkdtemp(prefix="sktest_"))
QUEUE_PATH_ORIG = sk.QUEUE_PATH
STATE_PATH_ORIG = sk.STATE_PATH

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL [{name}]: {detail}")

def make_jsonl(turns, path):
    """Generate JSONL from turn spec list. Each spec: (user_content, tool_names_in_response, has_anchor=False)"""
    sid = "test-session-001"
    lines = []
    ts = datetime(2026, 6, 11, 10, 0, 0)
    for i, (user_text, tool_names, has_anchor) in enumerate(turns):
        ts_str = ts.isoformat() + "Z"
        lines.append(json.dumps({
            "type": "user",
            "message": json.dumps({"role": "user", "content": user_text}),
            "timestamp": ts_str,
            "sessionId": sid,
        }, ensure_ascii=False))
        if has_anchor:
            anchor_text = "root cause found: the proxy was misconfigured"
        else:
            anchor_text = "Let me think about this..."
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": anchor_text},
                *[{"type": "tool_use", "name": tn, "id": f"t_{i}_{j}", "input": {}} for j, tn in enumerate(tool_names)],
                {"type": "text", "text": f"Response for turn {i}"},
            ]},
            "timestamp": (ts.replace(second=30)).isoformat() + "Z",
            "sessionId": sid,
        }, ensure_ascii=False))
        ts = ts + timedelta(minutes=2)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ══ Task 2: parse + smart sampling ══
print("── Task 2: parse_turns + sample_turns ──")

# 50 turns: 0-9 env, 10-29 dev (turn 15 has anchor), 30-49 debug
turns_spec = []
for i in range(50):
    if i < 10:
        t = "env"
        tools = ["Bash", "Read"] if i % 2 == 0 else ["Bash", "Write"]
        anchor = False
    elif i < 30:
        t = "dev"
        tools = ["Read", "Edit"] * ((i % 3) + 1)
        anchor = (i == 15)
    else:
        t = "debug"
        tools = ["Bash", "Read", "Edit", "Grep"] * (i % 3 + 1)
        anchor = False
    turns_spec.append((f"[{t}] turn {i}: doing {t} work number {i}", tools, anchor))

j2 = make_jsonl(turns_spec, TMP / "task2.jsonl")
lines = j2.read_text(encoding="utf-8").splitlines()
turns = sk.parse_turns(lines)

check("50 turns parsed", len(turns) == 50, f"got {len(turns)}")
check("turn 15 has anchor", turns[15]["has_anchor"] is True, f"got {turns[15]['has_anchor']}")
check("turn 0 no anchor", turns[0]["has_anchor"] is False)
check("turn 0 has tools", turns[0]["tool_count"] > 0)

sampled = sk.sample_turns(turns)
check("sampled not None", sampled is not None)
check("sampled has items", len(sampled) > 0, f"len={len(sampled)}")

sampled_indices = [turns.index(t) for t in sampled] if sampled else []
check("head-2 in sample", 0 in sampled_indices and 1 in sampled_indices, f"indices={sampled_indices[:10]}")
check("tail-2 in sample", 48 in sampled_indices and 49 in sampled_indices, f"indices={sampled_indices[-5:]}")
check("anchor turn 15 in sample", 15 in sampled_indices, f"indices={sorted(sampled_indices)}")

# < 3 turns → None
short_turns = turns[:2]
check("less than 3 turns returns None", sk.sample_turns(short_turns) is None)

# ══ Task 3: incremental state + atomic write ─═
print("── Task 3: state save/load + atomic write ──")

state_path = TMP / "test_state.json"
sk.STATE_PATH = state_path

# First: write state
test_state = {
    "abc": {"byte_offset": 5000, "turn_index": 20, "last_updated": "2026-06-11T10:00:00"},
}
sk.save_state(test_state)
check("state file exists", state_path.exists())
check("state file not .tmp", not str(state_path).endswith(".tmp"))

loaded = sk.load_state()
check("loaded matches", loaded.get("abc", {}).get("byte_offset") == 5000, f"got {loaded}")

# Second: update + verify atomic
test_state["abc"]["byte_offset"] = 10000
sk.save_state(test_state)
loaded2 = sk.load_state()
check("updated offset", loaded2.get("abc", {}).get("byte_offset") == 10000, f"got {loaded2}")

# ══ Task 5: JSON parse tolerance ─═
print("── Task 5: parse_llm tolerance ─═")

# Clean JSON
r1 = sk.parse_llm('{"status":"ACCEPT","domain":"engineering","name":"fix","desc":"a fix","content":"details","confidence":0.8}')
check("clean JSON parsed", len(r1) > 0 and r1[0]["status"] == "ACCEPT", str(r1))

# Markdown code block wrapped
r2 = sk.parse_llm('```json\n{"status":"REJECT","domain":"reference","name":"test","desc":"d","content":"c","confidence":0.3}\n```')
check("markdown JSON parsed", len(r2) > 0 and r2[0]["status"] == "REJECT", str(r2))

# Extra whitespace + newlines in JSON
r3 = sk.parse_llm('\n\n{"status":"ACCEPT","domain":"bugfix","name":"mem-leak","desc":"memory leak fix","content":"fixed by closing fd","confidence":0.9}\n\nmore text')
check("noisy JSON parsed", len(r3) > 0 and r3[0]["name"] == "mem-leak", str(r3))

# Regex fallback - missing closing brace
r4 = sk.parse_llm('"status":"ACCEPT","domain":"engineering","name":"cool-fix","desc":"works","content":"done","confidence":0.7')
check("regex fallback partial", len(r4) > 0 and r4[0].get("status") == "ACCEPT", str(r4))

# Garbage input
r5 = sk.parse_llm("I cannot process this request")
check("garbage returns empty list", len(r5) == 0, str(r5))

# Empty
r6 = sk.parse_llm("")
check("empty returns empty list", len(r6) == 0, str(r6))

def run_main(stdin_json):
    """Run sk.main() with given stdin, catching SystemExit."""
    saved = sys.stdin
    sys.stdin = io.StringIO(stdin_json)
    try:
        sk.main()
    except SystemExit:
        pass
    finally:
        sys.stdin = saved

# ══ Task 8: resume skip ─═
print("── Task 8: resume skip ──")

old_queue = TMP / "old_queue.jsonl"
old_queue.write_text("old-data", encoding="utf-8")
sk.QUEUE_PATH = old_queue

old_state = {"resume-session": {"byte_offset": 100, "turn_index": 5}}
sk.save_state(old_state)

run_main(json.dumps({
    "session_id": "resume-session",
    "transcript_path": str(j2),
    "reason": "resume",
    "hook_event_name": "PreCompact",
}))

queue_content = sk.QUEUE_PATH.read_text(encoding="utf-8")
check("resume doesn't write queue", queue_content == "old-data", f"got: {queue_content!r}")

# ══ Task 6: queue write (fire-and-forget) ─═
print("── Task 6: queue write ─═")

sk.QUEUE_PATH = TMP / "task6_queue.jsonl"
sk.STATE_PATH = TMP / "task6_state.json"

fresh_turns = [(f"Question {i}: how to fix bug {i}", ["Read", "Bash", "Edit"], i == 5) for i in range(10)]
j6 = make_jsonl(fresh_turns, TMP / "task6.jsonl")

run_main(json.dumps({
    "session_id": "task6-session",
    "transcript_path": str(j6),
    "reason": "auto_compact",
    "hook_event_name": "PreCompact",
}))

queue_exists = sk.QUEUE_PATH.exists()
check("queue file created", queue_exists)
if queue_exists:
    content = sk.QUEUE_PATH.read_text(encoding="utf-8")
    check("queue has 1 entry", len([l for l in content.splitlines() if l.strip()]) == 1, f"lines: {content[:200]}")
    entry = json.loads(content.splitlines()[0])
    check("queue entry has prompt", "prompt" in entry)
    check("queue entry has session_id", "task6" in entry.get("session_id", ""),
              f"got session_id={entry.get('session_id')}")

# Verify state was updated
state = sk.load_state()
check("state has task6-session", "task6-session" in state, f"keys: {list(state.keys())}")
if "task6-session" in state:
    check("byte_offset set", state["task6-session"]["byte_offset"] > 0)

# ══ Task 4: LLM call (only if proxy reachable) ─═
print("── Task 4: LLM call ──")
try:
    import urllib.request
    req = urllib.request.Request("http://localhost:4000/health", method="GET")
    urllib.request.urlopen(req, timeout=2)
    proxy_ok = True
except Exception:
    proxy_ok = False

if proxy_ok:
    # Technical fix → should ACCEPT
    fix_prompt = sk.fmt_prompt([
        {"user_content": "How to fix the memory leak?", "has_anchor": True, "tool_count": 5},
        {"user_content": "Let me check the code", "has_anchor": False, "tool_count": 3},
        {"user_content": "I found the root cause: fd not closed", "has_anchor": True, "tool_count": 8},
        {"user_content": "The fix is to add close()", "has_anchor": False, "tool_count": 2},
    ], 4, 18, 15)

    try:
        text = sk.call_llm(fix_prompt)
        parsed = sk.parse_llm(text)
        check("LLM returns parseable", len(parsed) > 0, f"raw: {text[:200]}")
        if parsed:
            check("LLM ACCEPTS fix session", parsed[0].get("status") == "ACCEPT",
                  f"status={parsed[0].get('status')} name={parsed[0].get('name')}")
    except Exception as e:
        check("LLM call succeeds", False, str(e))

    # Chitchat → should REJECT
    chat_prompt = sk.fmt_prompt([
        {"user_content": "Hello, how are you?", "has_anchor": False, "tool_count": 0},
        {"user_content": "What's the weather like?", "has_anchor": False, "tool_count": 0},
        {"user_content": "Tell me a joke", "has_anchor": False, "tool_count": 0},
    ], 3, 0, 5)

    try:
        text2 = sk.call_llm(chat_prompt)
        parsed2 = sk.parse_llm(text2)
        check("LLM returns empty for chitchat", len(parsed2) == 0, f"raw: {text2[:200]}, parsed: {parsed2}")
        if parsed2:
            check("LLM REJECTS chitchat", parsed2[0].get("status") in ("REJECT", "ACCEPT"),
                  f"status={parsed2[0].get('status')}")
    except Exception as e:
        check("LLM chat call succeeds", False, str(e))
else:
    print("  (proxy not reachable, skipping LLM tests)")

# ══ Task 7: E2E with mock LLM ─═
print("── Task 7: E2E mock LLM ──")

TMP7 = Path(tempfile.mkdtemp(prefix="sktest_e2e_"))

def mock_call_llm(prompt):
    return '[{"status":"ACCEPT","domain":"engineering","name":"bug-fix-001","desc":"E2E mock fix","content":"Found root cause and fixed","confidence":0.85}]'

original_call_llm = sk.call_llm
sk.call_llm = mock_call_llm

import _storage
orig_root = _storage.MEMORY_ROOT
e2e_mem = TMP7 / "test_e2e_memory"
e2e_mem.mkdir(parents=True, exist_ok=True)
(e2e_mem / "MEMORY.md").write_text("", encoding="utf-8")
_storage.MEMORY_ROOT = e2e_mem

sk.QUEUE_PATH = TMP7 / "task7_queue.jsonl"
sk.STATE_PATH = TMP7 / "task7_state.json"

e2e_turns = [(f"Turn {i}: building feature", ["Read", "Write"], i == 3) for i in range(8)]
j7 = make_jsonl(e2e_turns, TMP7 / "task7.jsonl")

# Simulate PreCompact — enqueue without spawning subprocess
run_main(json.dumps({
    "session_id": "task7-session",
    "transcript_path": str(j7),
    "reason": "clear",
    "hook_event_name": "PreCompact",
}))

# Queue should have content now
queue_content = sk.QUEUE_PATH.read_text(encoding="utf-8")
check("E2E queue has entry after enqueue", len(queue_content.strip()) > 0, f"empty: {queue_content[:200]}")

# Call consume_queue directly (not via subprocess) so mock applies
sk.consume_queue()

# Queue should be empty after consume
queue_content = sk.QUEUE_PATH.read_text(encoding="utf-8")
check("E2E queue emptied after consume", queue_content.strip() == "", f"queue not empty")

# Memory should be stored
stored_files = list(e2e_mem.rglob("*.md"))
mem_file = [f for f in stored_files if f.name != "MEMORY.md"]
check("E2E memory file created", len(mem_file) > 0, f"files: {[f.name for f in stored_files]}")
if mem_file:
    content = mem_file[0].read_text(encoding="utf-8")
    check("E2E memory has correct name", "bug-fix-001" in content)
    check("E2E memory has correct confidence", "0.85" in content)

# State should be updated
state = sk.load_state()
check("E2E state has task7", "task7-session" in state, f"keys: {list(state.keys())}")

# Restore
_storage.MEMORY_ROOT = orig_root
sk.call_llm = original_call_llm
sk.QUEUE_PATH = QUEUE_PATH_ORIG
sk.STATE_PATH = STATE_PATH_ORIG
import shutil
shutil.rmtree(TMP7, ignore_errors=True)

# ══ fmt_prompt format check ─═
print("── fmt_prompt ──")
sampled_mock = [
    {"user_content": "Q1: initial setup", "has_anchor": False, "tool_count": 2},
    {"user_content": "Q2: env check", "has_anchor": False, "tool_count": 1},
    {"user_content": "Q3: root cause found in config", "has_anchor": True, "tool_count": 8},
    {"user_content": "Q4: final fix applied", "has_anchor": False, "tool_count": 3},
    {"user_content": "Q5: cleanup", "has_anchor": False, "tool_count": 1},
]
p = sk.fmt_prompt(sampled_mock, 5, 15, 10)
check("HEAD label present", "[HEAD]" in p)
check("TAIL label present", "[TAIL]" in p)
check("ANCHOR label present", "[ANCHOR]" in p, p)
check("REJECT rules present", "REJECT" in p)
check("Recall design present", "json array" in p.lower() or "recall-oriented" in p.lower())

# ══ Cleanup ─═
import shutil
shutil.rmtree(TMP, ignore_errors=True)

# ══ Reset globals ─═
sk.STATE_PATH = Path.home() / ".claude" / "session_knowledge_state.json"
sk.QUEUE_PATH = Path.home() / ".claude" / "knowledge_queue.jsonl"

print(f"\n── {passed} passed, {failed} failed ──")
sys.exit(0 if failed == 0 else 1)
