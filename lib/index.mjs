/**
 * dsh-session-bridge — expose "read / send to any DSH session" as DSH tools.
 *
 * The heavy lifting lives in tools/dshctl.py (local JSON-RPC over HTTP).
 * This module only adapts it to three first-party tools:
 *   session_list  — list every session (id, title, cwd, running)
 *   session_read  — read the last N messages of one session from its transcript
 *   session_send  — append a message to a session and (optionally) wait for the turn
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { defineTool } from "@deepseek-ai/dsh-tools";

export const name = "dsh-session-bridge";
export const inject = ["tools"];

const HERE = dirname(fileURLToPath(import.meta.url));
const CLI = join(HERE, "..", "tools", "dshctl.py");
const PYTHON = process.env.DSH_SESSION_BRIDGE_PYTHON || "python3";

function runCli(args, timeoutMs = 180_000) {
  return new Promise((resolve, reject) => {
    if (!existsSync(CLI)) {
      reject(new Error(`dsh-session-bridge: missing CLI at ${CLI}`));
      return;
    }
    const child = spawn(PYTHON, [CLI, ...args], {
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`dsh-session-bridge: ${args[0]} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => { clearTimeout(timer); reject(error); });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolve(stdout);
      else reject(new Error(`dsh-session-bridge: ${args[0]} exited ${code}\n${stderr || stdout}`.trim()));
    });
  });
}

async function runJson(args, timeoutMs) {
  const out = await runCli(args, timeoutMs);
  try {
    return JSON.parse(out);
  } catch {
    throw new Error(`dsh-session-bridge: expected JSON from "dshctl ${args.join(" ")}", got:\n${out.slice(0, 800)}`);
  }
}

const text = (value) => [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }];
const jsonOutput = { schema: { type: "json" }, render: (_args, value) => text(value) };

export function apply(ctx, _config) {
  const disposers = [];

  disposers.push(ctx.tools.register(defineTool({
    name: "session_list",
    description:
      "List every DeepSeek Harness session known to the running desktop app. " +
      "Returns compact rows (id, title, cwd, running, updatedAt, outputTokens); full=true returns the raw rows. " +
      "Read-only. Use it before session_send so you target the right session id.",
    parameters: {
      full: { type: "boolean", description: "Return raw rows with agent availability, format status and every projection." },
    },
    output: jsonOutput,
    isConcurrencySafe: () => true,
    async execute(args) {
      return runJson(["list", "--json", ...(args.full ? ["--full"] : [])], 60_000);
    },
  })));

  disposers.push(ctx.tools.register(defineTool({
    name: "session_read",
    description:
      "Read the last N messages of one DSH session straight from its on-disk transcript (works even when the app is closed). " +
      "Accepts a full session-<uuid> or a unique title substring; ambiguous needles fail with the candidate list.",
    parameters: {
      session: { type: "string", description: "session-<uuid> or a unique title substring.", required: true },
      count: { type: "number", description: "How many messages to return (default 12)." },
    },
    output: jsonOutput,
    isConcurrencySafe: () => true,
    async execute(args) {
      const n = Number.isFinite(args.count) && args.count > 0 ? Math.floor(args.count) : 12;
      return runJson(["tail", args.session, String(n), "--json"], 60_000);
    },
  })));

  disposers.push(ctx.tools.register(defineTool({
    name: "session_send",
    description:
      "Append a message to an existing DSH session and let that session's own agent run a turn. " +
      "mode=queue waits politely; mode=steer injects into a running turn. " +
      "Say which session and why in one line before sending — never send on the user's behalf without their instruction. " +
      "Ambiguous session needles are rejected instead of guessed.",
    parameters: {
      session: { type: "string", description: "session-<uuid> or a unique title substring.", required: true },
      message: { type: "string", description: "Exact text to deliver.", required: true },
      steer: { type: "boolean", description: "Interrupt the running turn instead of queueing behind it." },
      wait: { type: "number", description: "Seconds to wait for the turn to finish (0 = fire and forget)." },
    },
    output: { schema: { type: "json" }, render: (_args, value) => text(value) },
    isConcurrencySafe: () => false,
    async execute(args) {
      const wait = Number.isFinite(args.wait) ? Math.max(0, Math.floor(args.wait)) : 0;
      const cli = ["prompt", args.session, args.message];
      if (args.steer) cli.push("--steer");
      if (wait > 0) cli.push("--wait", String(wait));
      const out = await runCli(cli, (wait > 0 ? wait + 60 : 60) * 1000);
      return { session: args.session, mode: args.steer ? "steer" : "queue", output: out.trim() };
    },
  })));

  disposers.push(ctx.tools.register(defineTool({
    name: "session_chat",
    description:
      "Keep a long-lived chat channel with one DSH session, reusing the same focus state as `dshctl.py chat` " +
      "(~/.cache/dshctl-chat.json). Actions: open (pick a session and show a menu + last messages), " +
      "say (send text to the focused session, wait for its turn, return its reply), peek (re-read context), " +
      "close (drop focus), status (who am I talking to). Use this instead of repeating session_send when you " +
      "will exchange several messages with one session.",
    parameters: {
      action: { type: "string", description: "open | say | peek | close | status", required: true },
      session: { type: "string", description: "For action=open: session-<uuid> or a unique title substring." },
      text: { type: "string", description: "For action=say: exact message to deliver." },
      lines: { type: "number", description: "Context messages to return (default 6)." },
      wait: { type: "number", description: "For action=say: seconds to wait for the turn (default 180)." },
    },
    output: jsonOutput,
    isConcurrencySafe: () => false,
    async execute(args) {
      const action = String(args.action || "").toLowerCase();
      const lines = Number.isFinite(args.lines) && args.lines > 0 ? Math.floor(args.lines) : 6;
      const focus = async () => {
        const out = (await runCli(["where"], 30_000)).trim();
        try { return JSON.parse(out); } catch { return null; }
      };
      const peek = async (sessionId, n) => {
        return runJson(["tail", sessionId, String(n), "--json"], 60_000);
      };
      const menu = [
        "1 发消息（自动等它回完）",
        "2 看上下文（最后若干条）",
        "3 换一个会话（action=open）",
        "4 结束对话（action=close）",
      ];
      if (action === "open") {
        if (!args.session) throw new Error("session_chat: action=open needs a session");
        const note = (await runCli(["focus", args.session], 60_000)).trim();
        const f = await focus();
        if (!f?.sessionId) throw new Error("session_chat: focus did not stick");
        const page = await peek(f.sessionId, lines);
        return { action, focused: { sessionId: f.sessionId, title: f.title },
                 menu, messages: page.messages, note };
      }
      if (action === "close") {
        const note = (await runCli(["focus", "-"], 30_000)).trim();
        return { action, focused: null, menu: [], note };
      }
      const f = await focus();
      if (action === "status") return { action, focused: f ? { sessionId: f.sessionId, title: f.title } : null, menu };
      if (!f?.sessionId) {
        return { action, focused: null, menu,
                 error: "没有进行中的会话：先用 action=open 指定一个 session" };
      }
      if (action === "peek") {
        const page = await peek(f.sessionId, lines);
        return { action, focused: { sessionId: f.sessionId, title: f.title }, menu, messages: page.messages };
      }
      if (action === "say") {
        if (!args.text) throw new Error("session_chat: action=say needs text");
        const wait = Number.isFinite(args.wait) ? Math.max(0, Math.floor(args.wait)) : 180;
        const out = await runCli(["prompt", f.sessionId, args.text, "--wait", String(wait)], (wait + 60) * 1000);
        const page = await peek(f.sessionId, 4);
        const reply = [...page.messages].reverse().find((m) => m.role === "assistant")?.text ?? null;
        return { action, focused: { sessionId: f.sessionId, title: f.title },
                 reply, menu, messages: page.messages, output: out.trim() };
      }
      throw new Error(`session_chat: unknown action ${args.action}`);
    },
  })));

  return () => {
    for (const dispose of disposers) {
      try { dispose?.(); } catch { /* ignore */ }
    }
  };
}
