#!/usr/bin/env node
/** npm bin shim: forwards to tools/dshctl.py so `npx dsh-session-bridge` works. */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const CLI = join(HERE, "..", "tools", "dshctl.py");
const python = process.env.DSH_SESSION_BRIDGE_PYTHON || "python3";

const child = spawn(python, [CLI, ...process.argv.slice(2)], { stdio: "inherit", env: process.env });
child.on("close", (code) => process.exit(code ?? 1));
child.on("error", (error) => {
  console.error(`dshctl: cannot start ${python} — ${error.message}`);
  process.exit(1);
});
