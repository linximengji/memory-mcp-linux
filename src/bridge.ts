import { spawn, spawnSync } from "node:child_process";
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
  ok: boolean;
  result: string;
  error: string;
}

export class Bridge {
  async call(method: string, params: Record<string, unknown>, timeoutMs?: number): Promise<string> {
    const payload = JSON.stringify({ method, params });
    const timeout = timeoutMs ?? 30000;

    return new Promise<string>((resolve, reject) => {
      const proc = spawn(PYTHON, ["-u", BRIDGE_SCRIPT], {
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
      });

      let stdout = "";
      let stderr = "";

      const timer = setTimeout(() => {
        proc.kill();
        reject(new Error(`bridge timeout after ${timeout}ms for method: ${method}`));
      }, timeout);

      proc.stdout!.on("data", (data: Buffer) => {
        stdout += data.toString("utf-8");
      });

      proc.stderr!.on("data", (data: Buffer) => {
        stderr += data.toString("utf-8");
      });

      proc.on("error", (err) => {
        clearTimeout(timer);
        reject(new Error(`bridge spawn failed: ${err.message}`));
      });

      proc.on("close", (code) => {
        clearTimeout(timer);
        if (code !== 0) {
          reject(new Error(`bridge exit ${code}: ${stderr.slice(0, 200)}`));
          return;
        }
        const line = stdout.trim().split("\n").pop() || "";
        try {
          const resp: BridgeResponse = JSON.parse(line);
          if (resp.ok) {
            // Some tools (health_check) return a dict that gets parsed as
            // an object, but MCP content.text must be a string.
            resolve(typeof resp.result === "string" ? resp.result : JSON.stringify(resp.result));
          } else {
            reject(new Error(resp.error || "unknown bridge error"));
          }
        } catch {
          reject(new Error(`unparseable response: ${line.slice(0, 200)}`));
        }
      });

      proc.stdin!.end(payload + "\n");
    });
  }
}
