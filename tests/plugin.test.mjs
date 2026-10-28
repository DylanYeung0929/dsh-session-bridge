#!/usr/bin/env node
/**
 * Offline test for lib/index.mjs (the DSH plugin entry).
 *
 * The real `@deepseek-ai/dsh-tools` only ships inside app.asar, so we install a
 * minimal stub that enforces the same defineTool contract and hand it our
 * `apply()`. This proves the plugin registers the three tools with sane
 * parameters without needing a DeepSeek Harness installation.
 *
 * Run: node tests/plugin.test.mjs
 */
import assert from "node:assert/strict";
import { cp, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const repo = new URL("..", import.meta.url).pathname;
const work = await mkdtemp(join(tmpdir(), "dsh-session-bridge-"));

try {
  await cp(join(repo, "lib"), join(work, "lib"), { recursive: true });
  await cp(join(repo, "tools"), join(work, "tools"), { recursive: true });
  const stub = join(work, "node_modules", "@deepseek-ai", "dsh-tools");
  await mkdir(stub, { recursive: true });
  await writeFile(join(stub, "package.json"),
    JSON.stringify({ name: "@deepseek-ai/dsh-tools", version: "0.0.0-stub", type: "module", main: "index.js" }));
  await writeFile(join(stub, "index.js"), `
export function defineTool(options) {
  for (const key of ["name", "description", "parameters", "output", "execute"]) {
    if (options[key] === undefined) throw new Error(\`defineTool(\${options.name}): missing \${key}\`);
  }
  if (typeof options.output.render !== "function") throw new Error(\`defineTool(\${options.name}): output.render must be a function\`);
  if (typeof options.output.schema !== "object") throw new Error(\`defineTool(\${options.name}): output.schema must be an object\`);
  for (const [param, spec] of Object.entries(options.parameters)) {
    if (!spec || typeof spec.type !== "string") throw new Error(\`defineTool(\${options.name}): parameter \${param} needs a type\`);
  }
  return { ...options };
}
`);

  const mod = await import(pathToFileURL(join(work, "lib", "index.mjs")).href);

  assert.equal(typeof mod.apply, "function", "plugin must export apply()");
  assert.ok(mod.name, "plugin must export name");

  const registered = [];
  const dispose = mod.apply({ tools: { register: (tool) => { registered.push(tool); return () => {}; } } }, {});
  assert.equal(typeof dispose, "function", "apply() must return a disposer");
  assert.deepEqual(registered.map((t) => t.name).sort(), ["session_chat", "session_list", "session_read", "session_send"]);

  const byName = Object.fromEntries(registered.map((t) => [t.name, t]));
  assert.equal(byName.session_list.parameters.full.type, "boolean");
  assert.equal(byName.session_read.parameters.session.required, true);
  assert.equal(byName.session_send.parameters.message.required, true);
  assert.equal(byName.session_chat.parameters.action.required, true);

  const rendered = byName.session_list.output.render({}, []);
  assert.equal(rendered[0].type, "text");

  dispose();
  console.log("plugin.test.mjs: ok — 4 tools registered, contract valid");
} finally {
  await rm(work, { recursive: true, force: true });
}
