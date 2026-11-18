"use strict";

// Container-side run_code runner, persistent Chromium CDP SDK, and ffmpeg video transcoder.
// Shares the host's unified /job/tool & /job/tools HTTP gateway via /run/gaoji credentials.

const { execFile } = require("node:child_process");
const { randomUUID } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { mkdir, stat, unlink, utimes, writeFile } = require("node:fs/promises");
const { format, inspect, promisify } = require("node:util");

const execFileAsync = promisify(execFile);

const CDP_URL = "http://127.0.0.1:9222";
const MAX_VIDEO_SECONDS = 600;
const MAX_VIDEO_BYTES = 32 * 1024 * 1024;
const VIDEO_SCALE = "scale='min(640,iw)':'min(640,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2";

const writeStdout = process.stdout.write.bind(process.stdout);
const writeStderr = text => process.stderr.write(text);

class ToolError extends Error {
  constructor(tool, error) {
    const msg = typeof error === "string" ? error : (error?.message || JSON.stringify(error));
    const code = typeof error === "object" && error?.code ? error.code : "tool_failed";
    super(`${tool}: ${msg}`);
    this.name = "ToolError";
    this.code = code;
  }
}

const failed = (code, message) => ({ ok: false, error: { code, message } });

function resolveLocalPath(work, rawPath) {
  if (typeof rawPath !== "string" || !rawPath.trim()) {
    throw new Error("path must be a non-empty string");
  }
  const p = rawPath.trim();
  if (p.startsWith("/workspace/")) return `${work}/${p.slice("/workspace/".length)}`;
  if (p === "/workspace" || p === "/work") return work;
  if (p.startsWith("/work/")) return `${work}/${p.slice("/work/".length)}`;
  if (p.startsWith("/")) return p;
  return `${work}/${p}`;
}

function toWorkPath(work, rawPath) {
  const p = rawPath.trim();
  if (p.startsWith("/work/")) return p;
  if (p.startsWith("/workspace/")) return `/work/${p.slice("/workspace/".length)}`;
  if (p.startsWith(`${work}/`)) return `/work/${p.slice(work.length + 1)}`;
  if (!p.startsWith("/")) return `/work/${p}`;
  return p;
}

async function makeScratch(work, ext = "") {
  const dir = `${work}/.gaoji/shown`;
  await mkdir(dir, { recursive: true });
  const name = `${randomUUID()}${ext}`;
  return [`${dir}/${name}`, `/work/.gaoji/shown/${name}`];
}

function parseTime(value) {
  if (value === undefined || value === null || value === "") return undefined;
  if (typeof value === "number") return Number.isFinite(value) && value >= 0 ? value : null;
  if (typeof value !== "string" || !/^\d+(:\d{1,2}){0,2}(\.\d+)?$/.test(value.trim())) return null;
  return value.trim().split(":").reduce((acc, part) => acc * 60 + Number(part), 0);
}

function lastFfmpegLine(error, file) {
  const lines = String(error?.stderr ?? "").trim().split("\n").filter(Boolean);
  const last = lines.at(-1) ?? error?.message ?? "unknown ffmpeg error";
  return last.startsWith(`${file}: `) ? last.slice(file.length + 2) : last;
}

async function watchVideo(work, path, { start, end } = {}) {
  if (typeof path !== "string" || !path.trim()) {
    return failed("bad_arguments", "video requires {path}, with optional start and end");
  }
  const from = parseTime(start);
  const to = parseTime(end);
  if (from === null || to === null) {
    return failed("bad_time", 'start and end take seconds or "MM:SS" / "HH:MM:SS"');
  }
  const local = resolveLocalPath(work, path);
  let duration;
  try {
    const { stdout } = await execFileAsync("ffprobe", [
      "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", local,
    ], { timeout: 30_000 });
    duration = Number(stdout.trim());
  } catch (err) {
    return failed("not_a_video", `cannot read video ${path}: ${lastFfmpegLine(err, local)}`);
  }
  const begin = from ?? 0;
  const stop = to ?? duration;
  if (Number.isFinite(duration) && duration > 0 && begin >= duration) {
    return failed("bad_time", `start (${begin}s) is at or past video duration (${Math.round(duration)}s)`);
  }
  if (Number.isFinite(stop) && stop <= begin) {
    return failed("bad_time", "end must be greater than start");
  }
  if (Number.isFinite(stop) && stop - begin > MAX_VIDEO_SECONDS) {
    return failed(
      "too_long",
      `clip is ${Math.round(stop - begin)}s; at most ${MAX_VIDEO_SECONDS / 60} minutes (600s) can be watched at once — pass start and end`,
    );
  }
  const [written, workPath] = await makeScratch(work, ".mp4");
  const clipSec = Number.isFinite(stop) ? Math.min(stop - begin, MAX_VIDEO_SECONDS) : MAX_VIDEO_SECONDS;
  const cut = ["-ss", String(begin), "-t", String(clipSec)];
  try {
    await execFileAsync("ffmpeg", [
      "-nostdin", "-y", "-v", "error",
      ...cut, "-i", local,
      "-map", "0:v:0", "-map", "0:a:0?",
      "-vf", `fps=1,${VIDEO_SCALE}`,
      "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
      "-c:a", "aac", "-ac", "1", "-b:a", "32k",
      "-movflags", "+faststart",
      written,
    ], { timeout: 180_000 });
  } catch (err) {
    await unlink(written).catch(() => {});
    return failed("not_a_video", `cannot transcode ${path}: ${lastFfmpegLine(err, local)}`);
  }
  const info = await stat(written);
  if (info.size <= 0 || info.size > MAX_VIDEO_BYTES) {
    await unlink(written).catch(() => {});
    return failed("video_too_large", `transcoded clip size (${info.size} bytes) exceeds 32 MiB limit`);
  }
  const seconds = Number.isFinite(stop) ? Math.max(1, Math.round(stop - begin)) : undefined;
  return { ok: true, value: { path: workPath, seconds, bytes: info.size } };
}

async function stageImage(work, arg) {
  if (arg instanceof Uint8Array) {
    const [written, workPath] = await makeScratch(work, ".png");
    await writeFile(written, arg);
    return { ok: true, value: { path: workPath } };
  }
  const src = typeof arg === "string" ? arg : arg?.path;
  if (typeof src !== "string" || !src.trim()) {
    return failed("bad_arguments", "show takes {path} under /work or a Uint8Array");
  }
  const local = resolveLocalPath(work, src);
  try {
    const info = await stat(local);
    if (!info.isFile()) return failed("not_a_picture", `${src} is not a regular file`);
  } catch {
    return failed("not_found", `${src} does not exist`);
  }
  return { ok: true, value: { path: toWorkPath(work, src) } };
}

function loadCredentials(input = {}) {
  if (input.url && input.token) {
    return { baseUrl: String(input.url).replace(/\/+$/, ""), token: String(input.token).trim() };
  }
  if (process.env.GAOJI_HOST_URL && process.env.GAOJI_JOB_TOKEN) {
    return {
      baseUrl: process.env.GAOJI_HOST_URL.trim().replace(/\/+$/, ""),
      token: process.env.GAOJI_JOB_TOKEN.trim(),
    };
  }
  const dir = process.env.GAOJI_CRED_DIR || "/run/gaoji";
  const token = readFileSync(`${dir}/token`, "utf8").trim();
  const conn = JSON.parse(readFileSync(`${dir}/connection.json`, "utf8"));
  const parsed = new URL(conn.base_url);
  if (parsed.protocol !== "http:" || (parsed.hostname !== "10.0.2.2" && parsed.hostname !== "127.0.0.1")) {
    throw new Error("invalid host gateway URL in connection.json");
  }
  return { baseUrl: conn.base_url.replace(/\/+$/, ""), token };
}

async function listHostTools(creds) {
  const resp = await fetch(`${creds.baseUrl}/job/tools`, {
    method: "GET",
    headers: { Authorization: `Bearer ${creds.token}` },
  });
  const data = await resp.json();
  if (!resp.ok || !Array.isArray(data?.tools)) {
    throw new Error(`failed to list /job/tools (${resp.status})`);
  }
  return data.tools.map(t => t?.function?.name).filter(Boolean);
}

async function callHostTool(creds, name, args, signal) {
  let response;
  try {
    response = await fetch(`${creds.baseUrl}/job/tool`, {
      method: "POST",
      signal,
      headers: {
        Authorization: `Bearer ${creds.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ name, arguments: args ?? {} }),
    });
  } catch (err) {
    if (err?.name === "AbortError") {
      return failed("cancelled", "tool call was cancelled");
    }
    return failed("unreachable", `cannot reach host /job/tool: ${err.message}`);
  }
  if (!response.ok) {
    const raw = await response.text();
    try {
      const parsed = JSON.parse(raw);
      return failed(parsed?.code || "http_error", parsed?.error || parsed?.detail || raw.slice(0, 300));
    } catch {
      return failed("http_error", `HTTP ${response.status}: ${raw.slice(0, 300)}`);
    }
  }
  const decoder = new TextDecoder();
  let buf = "";
  for await (const chunk of response.body) {
    buf += decoder.decode(chunk, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        return failed("protocol_error", "invalid NDJSON from /job/tool");
      }
      if (event.type === "heartbeat") continue;
      if (event.type === "result") {
        if (!event.ok) {
          return failed("tool_failed", event.error || "host tool returned ok=false");
        }
        const texts = (event.content || [])
          .filter(c => c && c.type === "text" && typeof c.text === "string")
          .map(c => c.text);
        const joined = texts.join("\n");
        let parsed = joined;
        try {
          parsed = JSON.parse(joined);
        } catch {}
        if (parsed && typeof parsed === "object" && parsed.ok === false) {
          return failed(parsed.code || "tool_failed", parsed.error || parsed.message || JSON.stringify(parsed));
        }
        return { ok: true, value: parsed };
      }
    }
  }
  return failed("protocol_error", "host closed /job/tool stream without a result");
}

// Persistent Chromium CDP helpers
let browserPromise;

async function touchBrowserUsed() {
  const stamp = "/run/gaoji-browser.used";
  const now = new Date();
  await utimes(stamp, now, now).catch(() => writeFile(stamp, "").catch(() => {}));
}

async function connectBrowser() {
  let chromium;
  try {
    ({ chromium } = require("playwright-core"));
  } catch (err) {
    if (err.code === "MODULE_NOT_FOUND") {
      throw new Error("playwright-core is not installed in this environment");
    }
    throw err;
  }
  await touchBrowserUsed();
  try {
    return await chromium.connectOverCDP(CDP_URL, { timeout: 2000 });
  } catch {
    await execFileAsync("systemctl", ["start", "gaoji-browser.service"], { timeout: 30_000 });
    return await chromium.connectOverCDP(CDP_URL, { timeout: 8000 });
  }
}

async function browser() {
  await touchBrowserUsed();
  browserPromise ??= connectBrowser().catch(err => {
    browserPromise = undefined;
    throw err;
  });
  const conn = await browserPromise;
  if (!conn.isConnected()) {
    browserPromise = undefined;
    return browser();
  }
  return conn.contexts()[0];
}

async function page() {
  const context = await browser();
  const pages = context.pages();
  if (pages.length === 0) return context.newPage();
  try {
    const ids = await Promise.all(pages.map(async p => {
      const session = await context.newCDPSession(p);
      try {
        return (await session.send("Target.getTargetInfo")).targetInfo.targetId;
      } finally {
        await session.detach();
      }
    }));
    const list = await (await fetch(`${CDP_URL}/json/list`)).json();
    const newest = list.find(t => t.type === "page" && ids.includes(t.id));
    return newest ? pages[ids.indexOf(newest.id)] : pages.at(-1);
  } catch {
    return pages.at(-1);
  }
}

async function search(engine, query) {
  const safeEngine = typeof engine === "string" && engine.length <= 32 ? engine : "";
  const safeQuery = typeof query === "string" && query.length <= 512 ? query : null;
  try {
    const { stdout } = await execFileAsync("gaoji-python", ["-m", "sandbox_search", JSON.stringify([safeEngine, safeQuery])], { timeout: 25_000 });
    return JSON.parse(stdout);
  } catch {
    return { status: "error", error: "helper_error", results: [] };
  }
}

async function snapshot(targetPage, { selector, depth } = {}) {
  if (!targetPage || typeof targetPage.locator !== "function") {
    throw new TypeError("gaoji.snapshot requires a Playwright page");
  }
  const loc = selector ? targetPage.locator(selector).first() : targetPage.locator("body");
  let tree;
  try {
    if (typeof targetPage.ariaSnapshot === "function") {
      const root = selector ? targetPage.locator(selector).first() : targetPage;
      tree = await root.ariaSnapshot({ mode: "ai", ...(depth ? { depth } : {}) });
    } else {
      tree = await loc.ariaSnapshot();
    }
  } catch {
    tree = await loc.ariaSnapshot();
  }
  return `${await targetPage.title()}\n${targetPage.url()}\n${tree}`;
}

async function wait_for_login(check, { timeout = 180, interval = 3 } = {}) {
  if (typeof timeout !== "number" || !(timeout > 0 && timeout <= 180)
      || typeof interval !== "number" || !(interval > 0 && interval <= 10)) {
    throw new RangeError("timeout must be (0, 180]s and interval must be (0, 10]s");
  }
  const deadline = Date.now() + timeout * 1000;
  while (Date.now() < deadline) {
    const ok = await check();
    if (typeof ok !== "boolean") {
      throw new TypeError("login check must return a boolean based on a real logged-in indicator");
    }
    if (ok) return true;
    await new Promise(r => setTimeout(r, interval * 1000));
  }
  return false;
}

function formatResult(value) {
  if (value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    const s = JSON.stringify(value);
    if (s !== undefined) return s;
  } catch {}
  return inspect(value, { depth: 6 });
}

function formatFailure(error) {
  if (!(error instanceof Error)) return `threw ${formatResult(error)}`;
  const [first, ...frames] = String(error.stack ?? error).split("\n");
  const own = frames.flatMap(line => {
    const m = /<anonymous>:(\d+):(\d+)\)?$/.exec(line);
    return m ? [`    at line ${Number(m[1]) - 2}, column ${m[2]}`] : [];
  });
  return [first, ...own].join("\n");
}

async function runCodeMain() {
  process.stdout.write = writeStderr;
  for (const level of ["log", "info", "warn", "error", "debug"]) {
    console[level] = (...vals) => writeStderr(format(...vals) + "\n");
  }

  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  const work = input.work || "/work";
  const shown = [];
  let delivered = false;

  let creds = null;
  const getCreds = () => {
    if (!creds) creds = loadCredentials(input);
    return creds;
  };

  let toolNames = Array.isArray(input.tools) ? input.tools : null;
  if (!toolNames) {
    try {
      toolNames = await listHostTools(getCreds());
    } catch {
      toolNames = ["history", "web_search", "web_fetch", "use_skill", "say", "send_file", "send_image", "action_status", "github"];
    }
  }

  const controllers = new WeakMap();
  const tracked = (promise, controller) => {
    const proxy = new Proxy(promise, {
      get(target, key) {
        if (key === "then" || key === "catch" || key === "finally") return target[key].bind(target);
        if (typeof key === "symbol") return target[key];
        throw new TypeError("a tool call returns a Promise: await it first, e.g. const r = await tools.web_search({queries: [query]})");
      },
    });
    if (controller) controllers.set(proxy, controller);
    return proxy;
  };

  const dispatch = async (name, args, signal) => {
    if (name === "show") {
      const out = await stageImage(work, args);
      if (out.ok) shown.push({ kind: "image", path: out.value.path });
      return out;
    }
    if (name === "video") {
      const out = await watchVideo(work, args?.path, { start: args?.start, end: args?.end });
      if (out.ok) {
        shown.push({ kind: "video", path: out.value.path, seconds: out.value.seconds, bytes: out.value.bytes });
        return { ok: true, value: { seconds: out.value.seconds, bytes: out.value.bytes } };
      }
      return out;
    }
    const out = await callHostTool(getCreds(), name, args, signal);
    if (out.ok && ["say", "send_file", "send_image"].includes(name)) {
      const v = out.value;
      if (!v || typeof v !== "object" || v.ok !== false) {
        delivered = true;
      }
    }
    return out;
  };

  const raw = (tool, args = {}) => {
    if (!(tool === "show" && args instanceof Uint8Array) && (!args || typeof args !== "object" || Array.isArray(args))) {
      throw new TypeError("a tool's arguments must be an object");
    }
    const controller = new AbortController();
    return tracked(dispatch(tool, args, controller.signal), controller);
  };

  const call = (tool, args = {}) => {
    const outcome = raw(tool, args);
    const val = outcome.then(res => {
      if (res.ok) return res.value;
      throw new ToolError(tool, res.error);
    });
    return tracked(val, controllers.get(outcome));
  };

  const cancel = promise => controllers.get(promise)?.abort();
  const race = promises => {
    const list = Array.from(promises);
    const abortRest = idx => list.forEach((p, i) => i !== idx && cancel(p));
    return Promise.race(list.map((p, idx) => p.then(
      v => { abortRest(idx); return v; },
      e => { abortRest(idx); throw e; },
    )));
  };

  const tools = new Proxy(Object.create(null), {
    get(_, prop) {
      if (typeof prop !== "string" || prop === "then") return undefined;
      return args => call(prop, args);
    },
    ownKeys() {
      return Array.from(new Set([...toolNames, "show", "video"]));
    },
    getOwnPropertyDescriptor(_, prop) {
      if (typeof prop === "string" && (toolNames.includes(prop) || prop === "show" || prop === "video")) {
        return { enumerable: true, configurable: true };
      }
      return undefined;
    },
  });

  const gaoji = Object.freeze({ raw, cancel, race, browser, page, search, snapshot, wait_for_login });
  const AsyncFunction = (async () => {}).constructor;
  const fn = new AsyncFunction("tools", "gaoji", "args", "max", input.code);
  try {
    const value = await fn(tools, gaoji, input.args ?? {}, gaoji);
    return { ok: true, result: formatResult(value), shown, delivered, exitCode: 0 };
  } catch (err) {
    return { ok: false, error: formatFailure(err), shown, delivered, exitCode: 1 };
  }
}

function flushAndExit(payload, code) {
  writeStdout(JSON.stringify(payload) + "\n", () => process.stderr.write("", () => process.exit(code)));
}

if (process.argv[2] === "--video") {
  const req = JSON.parse(process.argv[3] || "{}");
  watchVideo(req.work || "/work", req.path, { start: req.start, end: req.end }).then(
    out => flushAndExit(out, out.ok ? 0 : 1),
    err => flushAndExit(failed("internal", String(err?.message || err)), 1),
  );
} else {
  runCodeMain().then(
    out => {
      const { exitCode, ...body } = out;
      flushAndExit(body, exitCode);
    },
    err => flushAndExit({ ok: false, error: formatFailure(err), shown: [], delivered: false }, 1),
  );
}
