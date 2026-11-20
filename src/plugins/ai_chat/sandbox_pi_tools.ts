import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface HostTool {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

type HostToolEvent =
  | { type: "heartbeat" }
  | {
      type: "result";
      ok: boolean;
      content?: Array<{ type: "text"; text: string } | { type: "image"; data: string; mimeType: string }>;
      error?: string;
    };

export default async function hostToolsExtension(pi: ExtensionAPI): Promise<void> {
  const baseUrl = (process.env.GAOJI_HOST_URL || "").replace(/\/+$/, "");
  const token = process.env.GAOJI_JOB_TOKEN || "";
  if (!baseUrl || !token) throw new Error("GAOJI_HOST_URL and GAOJI_JOB_TOKEN are required");
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };

  const response = await fetch(`${baseUrl}/job/tools`, { headers, signal: AbortSignal.timeout(30_000) });
  if (!response.ok) throw new Error(`GET /job/tools failed with HTTP ${response.status}`);
  const payload = (await response.json()) as { tools: HostTool[] };
  if (!Array.isArray(payload.tools)) throw new Error("Invalid host tool list");

  async function call(name: string, args: unknown, signal?: AbortSignal): Promise<HostToolEvent> {
    const deadline = AbortSignal.timeout(900_000);
    const response = await fetch(`${baseUrl}/job/tool`, {
      method: "POST", headers, body: JSON.stringify({ name, arguments: args ?? {} }),
      signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
    });
    if (!response.ok || !response.body) throw new Error(`Host tool failed with HTTP ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        buffer += value ? decoder.decode(value, { stream: true }) : "";
        if (done) buffer += decoder.decode();
        let newline = buffer.indexOf("\n");
        while (newline !== -1) {
          const line = buffer.slice(0, newline).trim();
          buffer = buffer.slice(newline + 1);
          if (line) {
            const event = JSON.parse(line) as HostToolEvent;
            if (event.type === "result") return event;
          }
          newline = buffer.indexOf("\n");
        }
        if (done) throw new Error("Host closed the tool stream without a complete result");
      }
    } finally {
      await reader.cancel().catch(() => {});
    }
  }

  for (const tool of payload.tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.parameters as never,
      async execute(_id, params, signal) {
        const result = await call(tool.name, params, signal);
        if (result.type !== "result" || !result.ok) {
          throw new Error(result.type === "result" ? result.error || `Host tool ${tool.name} failed` : "Invalid result");
        }
        return { content: result.content ?? [{ type: "text", text: "" }], details: {} };
      },
    });
  }
}
