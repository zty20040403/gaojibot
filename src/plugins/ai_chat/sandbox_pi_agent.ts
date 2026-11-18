import * as fs from "node:fs";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const jobId = process.env.GAOJI_JOB_ID ?? "";
const unit = process.env.GAOJI_JOB_UNIT ?? (jobId ? `gaoji-job-${jobId}` : "");
const dir = process.env.GAOJI_JOB_DIR ?? "";
const outDir = process.env.GAOJI_JOB_OUT_DIR ?? (jobId ? `/work/.gaoji/jobs/job-${jobId}` : "");
const reports = process.env.GAOJI_JOB_REPORTS ?? "/work/.gaoji/jobs";
const inbox = `${dir}/inbox`;
const HOLD = Number(process.env.GAOJI_AGENT_HOLD_MS ?? 10 * 60_000);
const WARNING = 5 * 60_000;
const WRAP_UP = "还剩 5 分钟就到点了。别再查新的：现在先更新目录笔记，再把已经查到的写成报告，没做完的写明做到哪了。";
const NO_ANSWER = "前台在等待时间内未收到答复，请自行判断继续，并在报告里写明采用的假设。";

const readText = (file: string): string | null => {
  try {
    return fs.readFileSync(file, "utf8");
  } catch {
    return null;
  }
};

function taken(): string[] {
  let names: string[] = [];
  try {
    names = fs.readdirSync(inbox).filter(name => !name.startsWith("."));
  } catch {
    return [];
  }
  const steers: string[] = [];
  for (const name of names.sort()) {
    const steer = readText(`${inbox}/${name}`);
    fs.rmSync(`${inbox}/${name}`, { force: true });
    if (steer?.trim()) steers.push(steer.trim());
  }
  return steers;
}

const waiting = (): boolean => {
  try {
    return fs.readdirSync(inbox).some(name => !name.startsWith("."));
  } catch {
    return false;
  }
};

function written(message: { content?: unknown }): string {
  const content = Array.isArray(message.content) ? message.content : [];
  return content
    .filter((part): part is { type: "text"; text: string } => Boolean(part && typeof part === "object" && (part as { type?: unknown }).type === "text" && typeof (part as { text?: unknown }).text === "string"))
    .map(part => part.text)
    .join("")
    .trim();
}

export default function gaojiAgentExtension(pi: ExtensionAPI): void {
  if (!dir) return;
  let ctx: ExtensionContext | undefined;
  let stopping = false;
  let text: string | null = null;
  let error: string | null = null;
  let reported = false;
  const usage = { input: 0, output: 0, cached: 0, total: 0 };

  const stop = () => {
    stopping = true;
    try {
      fs.writeFileSync(`${dir}/stopped`, "");
    } catch {}
    ctx?.abort();
  };
  process.on("SIGUSR2", stop);

  const started = Number(readText(`${dir}/started`) ?? 0) * 1000;
  const limit = Number(readText(`${dir}/limit`) ?? 0) * 1000;
  const deadlineMs = started > 0 && limit > 0 ? started + limit : 0;
  const until = deadlineMs > 0 ? deadlineMs - WARNING - Date.now() : -1;
  const timer = until > 0
    ? setTimeout(() => {
        try {
          fs.mkdirSync(inbox, { recursive: true });
          fs.writeFileSync(`${inbox}/wrap-up`, WRAP_UP);
        } catch {}
      }, until)
    : undefined;
  timer?.unref();

  const comes = (ms: number) =>
    new Promise<void>(resolve => {
      if (ms <= 0) {
        resolve();
        return;
      }
      const done = () => {
        clearInterval(looking);
        clearTimeout(over);
        resolve();
      };
      const looking = setInterval(() => (stopping || waiting()) && done(), 250);
      const over = setTimeout(done, ms);
    });

  const persistResult = () => {
    if (reported) return;
    reported = true;
    clearTimeout(timer);
    try {
      if (text) {
        const title = (readText(`${dir}/title`) ?? "").trim();
        fs.mkdirSync(reports, { recursive: true });
        const reportName = jobId ? `job-${jobId}` : unit;
        fs.writeFileSync(`${reports}/${reportName}.md`, title ? `# ${reportName} ${title}\n\n${text}\n` : `${text}\n`);
      }
      if (outDir) {
        fs.mkdirSync(outDir, { recursive: true });
        const tmp = `${outDir}/.result.json.tmp`;
        fs.writeFileSync(
          tmp,
          JSON.stringify({
            text: text ?? "",
            error: stopping ? "" : (error ?? ""),
            stopped: stopping,
            usage,
          }),
        );
        fs.renameSync(tmp, `${outDir}/result.json`);
      }
    } catch {}
  };

  pi.on("agent_start", (_event, context) => {
    ctx = context;
  });

  pi.on("turn_start", (_event, context) => {
    ctx = context;
  });

  pi.on("tool_execution_start", event => {
    const toolName = (event as { toolName?: string }).toolName ?? "";
    if (!toolName) return;
    try {
      fs.writeFileSync(`${dir}/last_tool`, toolName);
    } catch {}
  });

  pi.on("message_end", event => {
    const message = event.message as {
      role?: string;
      content?: unknown;
      stopReason?: string;
      errorMessage?: string;
      usage?: Record<string, number>;
    };
    if (message.role !== "assistant") return;
    const wrote = written(message);
    if (wrote) text = wrote;
    if (message.usage && typeof message.usage === "object") {
      const u = message.usage;
      usage.input += Number(u.input || 0) + Number(u.cacheRead || 0) + Number(u.cacheWrite || 0);
      usage.output += Number(u.output || 0);
      usage.cached += Number(u.cacheRead || 0);
      usage.total += Number(u.totalTokens || 0);
    }
    error = message.stopReason === "error" ? (message.errorMessage ?? "Pi 运行失败。") : null;
  });

  pi.on("turn_end", () => {
    if (stopping) return;
    const steers = taken();
    if (steers.length > 0) fs.rmSync(`${dir}/asked`, { force: true });
    for (const steer of steers) pi.sendUserMessage(steer, { deliverAs: "steer" });
  });

  pi.on("agent_before_settle", async event => {
    if (stopping) return;
    let steers = taken();
    if (steers.length === 0 && fs.existsSync(`${dir}/asked`)) {
      const remaining = deadlineMs > 0 ? Math.max(0, deadlineMs - Date.now()) : HOLD;
      await comes(Math.min(HOLD, remaining));
      steers = taken();
      if (steers.length === 0 && !stopping) {
        steers = [NO_ANSWER];
      }
    }
    fs.rmSync(`${dir}/asked`, { force: true });
    if (stopping || steers.length === 0) return;
    const said = steers.map(content => ({
      type: "custom_message" as const,
      customType: "gaoji",
      content,
      display: true,
    }));
    return { entries: [...event.entries, ...said], continue: true };
  });

  pi.on("agent_settled", persistResult);
  pi.on("session_shutdown", persistResult);
}
