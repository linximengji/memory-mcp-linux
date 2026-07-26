import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));

const PYTHON_CANDIDATES = [
  process.env.MM_PYTHON,
  "python3",
  "python",
].filter(Boolean) as string[];

function findPython(): string {
  // Check absolute paths first
  for (const p of PYTHON_CANDIDATES) {
    if (p.includes("/") || p.includes("\\")) {
      if (existsSync(p)) return p;
    }
  }
  // Then check PATH-based named candidates
  for (const p of PYTHON_CANDIDATES) {
    if (!(p.includes("/") || p.includes("\\"))) {
      try {
        const result = spawnSync(p, ["--version"], { stdio: "pipe", windowsHide: true, timeout: 5000 });
        if (result.status === 0) return p;
      } catch { /* try next candidate */ }
    }
  }
  return "python";
}

const PYTHON = findPython();
const BRIDGE_SCRIPT = join(__dirname, "..", "_storage_bridge.py");

interface BridgeResponse {
  id?: number;
  ok: boolean;
  result: string;
  error: string;
}

interface Pending {
  resolve: (value: string) => void;
  reject: (reason: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class Bridge {
  private proc: ChildProcess | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private buffer = "";
  private stderrBuf = "";

  async call(method: string, params: Record<string, unknown>, timeoutMs?: number): Promise<string> {
    const timeout = timeoutMs ?? 30000;
    const proc = this.ensureProc();
    const id = this.nextId++;

    return new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`bridge timeout after ${timeout}ms for method: ${method}`));
      }, timeout);

      this.pending.set(id, { resolve, reject, timer });
      try {
        proc.stdin!.write(JSON.stringify({ id, method, params }) + "\n");
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error(`bridge write failed: ${(err as Error).message}`));
      }
    });
  }

  private ensureProc(): ChildProcess {
    if (this.proc && this.proc.exitCode === null && this.proc.signalCode === null) {
      return this.proc;
    }
    const proc = spawn(PYTHON, ["-u", BRIDGE_SCRIPT], {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
    });

    proc.stdout!.on("data", (data: Buffer) => {
      this.buffer += data.toString("utf-8");
      this.drainBuffer();
    });

    proc.stderr!.on("data", (data: Buffer) => {
      const chunk = data.toString("utf-8");
      this.stderrBuf += chunk;
      process.stderr.write(`[bridge-py] ${chunk}`);
    });

    proc.on("error", (err) => {
      this.failAll(`bridge spawn failed: ${err.message}`);
      this.proc = null;
    });

    proc.on("close", (code) => {
      this.proc = null;
      this.failAll(`bridge exited (code ${code}): ${this.stderrBuf.slice(-500)}`);
      this.stderrBuf = "";
    });

    this.proc = proc;
    return proc;
  }

  private drainBuffer(): void {
    let idx = this.buffer.indexOf("\n");
    while (idx !== -1) {
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 1);
      idx = this.buffer.indexOf("\n");
      if (!line.trim()) continue;
      this.handleLine(line);
    }
  }

  private handleLine(line: string): void {
    let resp: BridgeResponse;
    try {
      resp = JSON.parse(line);
    } catch {
      process.stderr.write(`[bridge] unparseable response: ${line.slice(0, 200)}\n`);
      return;
    }
    const id = resp.id;
    if (id == null) return;
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    clearTimeout(pending.timer);
    if (resp.ok) {
      // Some tools (health_check) return a dict that gets parsed as
      // an object, but MCP content.text must be a string.
      pending.resolve(typeof resp.result === "string" ? resp.result : JSON.stringify(resp.result));
    } else {
      pending.reject(new Error(resp.error || "unknown bridge error"));
    }
  }

  private failAll(reason: string): void {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(new Error(reason));
    }
    this.pending.clear();
    this.buffer = "";
  }
}
