import { NodeSDK } from "@opentelemetry/sdk-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { HttpInstrumentation } from "@opentelemetry/instrumentation-http";
import { trace } from "@opentelemetry/api";

const otelSdk = new NodeSDK({
  serviceName: "memory-mcp",
  traceExporter: new OTLPTraceExporter({ url: "http://localhost:4317/v1/traces" }),
  instrumentations: [new HttpInstrumentation()],
});
otelSdk.start();
const _tracer = trace.getTracer("memory-mcp");

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { Bridge } from "./bridge.js";

const VALID_SEGMENTS = ["_index", "overview", "deps", "design", "sop", "persona"] as const;

const SCHEMA_NOTE =
  "namespace is the module/project name (e.g. model-proxy, claudetalk). Auto-detected if omitted. " +
  "segment is one of: _index|overview|deps|design|sop. ";

const MemoryStoreSchema = z.object({
  name: z.string(),
  content: z.string(),
  namespace: z.string().optional(),
  segment: z.enum(VALID_SEGMENTS).default("_index"),
});

const MemoryUpdateSchema = z.object({
  name: z.string(),
  content: z.string().optional(),
  namespace: z.string().optional(),
});

const MemoryRecallSchema = z.object({
  query: z.string().default(""),
  namespace: z.string().optional(),
  top_k: z.number().int().min(1).max(50).default(5),
});

const MemoryGetSchema = z.object({
  name: z.string(),
  namespace: z.string().optional(),
});

const MemoryListSchema = z.object({
  namespace: z.string().optional(),
});

const MemoryDeleteSchema = z.object({
  name: z.string(),
});

const MemoryReviewSchema = z.object({
  action: z.enum(["apply", "dismiss"]),
  file: z.string().default(""),
});

const MemoryCleanupSchema = z.object({
  action: z.enum(["list", "delete"]).default("list"),
  names: z.array(z.string()).optional(),
});

type ToolResult = { content: Array<{ type: "text"; text: string }> };

const txt = (text: string): ToolResult => ({ content: [{ type: "text", text }] });

const call = (method: string, params: Record<string, unknown>) =>
  bridge.call(method, params).then(txt);

const callLong = (method: string, params: Record<string, unknown>) =>
  bridge.call(method, params, 120_000).then(txt);

function toolHandler(name: string, fn: (params: any) => Promise<ToolResult>) {
  return async (params: any): Promise<ToolResult> => {
    return _tracer.startActiveSpan(`tool:${name}`, async (span) => {
      span.setAttribute("tool", name);
      try {
        return await fn(params);
      } finally {
        span.end();
      }
    });
  };
}

const logLine = (msg: string) => {
  const ts = new Date().toISOString();
  process.stderr.write(`[${ts}] [server] ${msg}\n`);
};

logLine("memory-mcp-ts starting");

const bridge = new Bridge();

const server = new McpServer({
  name: "memory-mcp-server",
  version: "1.0.0",
});

server.registerTool(
  "memory_store",
  { description: SCHEMA_NOTE, inputSchema: MemoryStoreSchema.shape },
  toolHandler("memory_store", async (params) => call("memory_store", params as Record<string, unknown>))
);

server.registerTool(
  "memory_update",
  { description: "Update an existing memory. Only provided fields are changed.", inputSchema: MemoryUpdateSchema.shape },
  toolHandler("memory_update", async (params) => call("memory_update", params as Record<string, unknown>))
);

server.registerTool(
  "memory_recall",
  { description: "Search memories by query. Returns top_k most relevant results with scores.", inputSchema: MemoryRecallSchema.shape },
  toolHandler("memory_recall", async (params) => call("memory_recall", params as Record<string, unknown>))
);

server.registerTool(
  "memory_get",
  { description: "Read a specific memory by its name (kebab-case slug).", inputSchema: MemoryGetSchema.shape },
  toolHandler("memory_get", async (params) => call("memory_get", params as Record<string, unknown>))
);

server.registerTool(
  "memory_list",
  { description: "List all memories. Filter by namespace (project module name).", inputSchema: MemoryListSchema.shape },
  toolHandler("memory_list", async (params) => call("memory_list", params as Record<string, unknown>))
);

server.registerTool(
  "memory_delete",
  { description: "Delete a memory by name.", inputSchema: MemoryDeleteSchema.shape },
  toolHandler("memory_delete", async (params) => call("memory_delete", params as Record<string, unknown>))
);

server.registerTool(
  "memory_rebuild_index",
  { description: "Rebuild MEMORY.md index from all memory files.", inputSchema: z.object({}).shape },
  toolHandler("memory_rebuild_index", async () => call("memory_rebuild_index", {}))
);

server.registerTool(
  "memory_review",
  { description: "Apply or dismiss suggestions from the review report. action: 'apply' or 'dismiss'.", inputSchema: MemoryReviewSchema.shape },
  toolHandler("memory_review", async (params) => call("memory_review", params as Record<string, unknown>))
);

server.registerTool(
  "memory_cleanup",
  { description: "List or delete low-quality memories. action='list' shows candidates, action='delete' removes them.", inputSchema: MemoryCleanupSchema.shape },
  toolHandler("memory_cleanup", async (params) => call("memory_cleanup", params as Record<string, unknown>))
);

const HealthCheckSchema = z.object({
  llm_enabled: z.boolean().default(false),
  output_format: z.enum(["json", "html"]).default("json"),
});

server.registerTool(
  "memory_health_check",
  { description: "Run memory health check. L1 (structural) runs always; L3 (LLM audit) requires llm_enabled=true.",
    inputSchema: HealthCheckSchema.shape },
  toolHandler("memory_health_check", async (params) => callLong("health_check", { llm_enabled: params.llm_enabled }))
);

// --- Code Graph tools ---

const CodeScanSchema = z.object({
  projects: z.array(z.string()).optional(),
  git_scan: z.boolean().default(true),
});

const TraceImpactSchema = z.object({
  target: z.string(),
  depth: z.number().int().min(1).max(6).default(3),
  min_risk: z.enum(["low", "medium", "high"]).optional()
    ,
});

const CodeQuerySchema = z.object({
  query: z.string(),
  type: z.enum(["symbol", "consumer", "exporter"]).default("symbol")
    ,
  fuzzy: z.boolean().default(true),
});

server.registerTool(
  "memory_code_scan",
  { description: "[Trigger: 首次会话、怀疑依赖过时] 扫描25个项目的跨项目运行时依赖。产出 edges.json(HTTP/subprocess/fs依赖) 和 symbols.json(函数/类定义索引)。支持增量。",
    inputSchema: CodeScanSchema.shape },
  toolHandler("memory_code_scan", async (params) => {
    const args: Record<string, unknown> = {};
    if (params.projects) args.projects_filter = params.projects;
    return callLong("memory_code_scan", args);
  })
);

server.registerTool(
  "memory_trace_impact",
  { description: "[Trigger: 改接口/改函数签名/改文件路径/跨项目重构/删文件前] BFS影响链追踪。传入目标文件(支持 project/rel/path 或绝对路径)，返回所有下游依赖方(项目+文件+距离)。先查此工具再动手改。",
    inputSchema: TraceImpactSchema.shape },
  toolHandler("memory_trace_impact", async (params) => {
    const args: Record<string, unknown> = { target_file: params.target, max_depth: params.depth };
    return callLong("memory_trace_impact", args);
  })
);

server.registerTool(
  "memory_code_query",
  { description: "[Trigger: 搜函数定义/查谁在调用/找文件归属] 查询符号。type=symbol→搜定义位置; type=consumer→查谁在调用该符号; type=exporter→按文件聚合导出者。",
    inputSchema: CodeQuerySchema.shape },
  toolHandler("memory_code_query", async (params) => {
    const args: Record<string, unknown> = { query: params.query, query_type: params.type };
    return call("memory_code_query", args);
  })
);

const transport = new StdioServerTransport();
await server.connect(transport);

logLine("memory-mcp-ts ready (stdio)");

const shutdown = async (signal: string) => {
  logLine(`Received ${signal}, shutting down`);
  await server.close();
  otelSdk.shutdown();
  process.exit(0);
};

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
