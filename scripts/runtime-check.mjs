import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { setTimeout as delay } from "node:timers/promises";

const spec = JSON.parse(readFileSync("/test-spec.json"));
const home = "/home/node/.openclaw";
function cli(args) {
  const result = spawnSync("node", ["/app/openclaw.mjs", ...args], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stdout + result.stderr);
  return result.stdout;
}
function bootstrap() {
  const result = spawnSync("node", ["/bootstrap/bootstrap.mjs"], { stdio: "inherit" });
  assert.equal(result.status, 0, "Init must succeed");
}
async function execute(command) {
  const child = spawn(command[0], command.slice(1), { stdio: "ignore" });
  return new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", resolve);
  });
}

bootstrap();
const first = JSON.parse(cli(["plugins", "inspect", "slack", "--runtime", "--json"]));
assert.equal(first.plugin.version, spec.version);
assert.equal(first.plugin.status, "loaded");
assert.equal(first.plugin.imported, true);
assert.equal(first.plugin.trustedOfficialInstall, true);
assert(first.plugin.channelIds.includes("slack"));
assert.equal(first.install.spec, `@openclaw/slack@${spec.version}`);
const provider = JSON.parse(cli(["plugins", "inspect", "openrouter", "--runtime", "--json"]));
assert.equal(provider.plugin.status, "loaded");
assert(provider.plugin.providerIds.includes("openrouter"));

const original = readFileSync(`${home}/openclaw.json`, "utf8");
const metadata = JSON.parse(original).meta;
writeFileSync(`${home}/openclaw.json`, original.replace('"loopback"', '"lan"'));
writeFileSync(`${home}/workspace/AGENTS.md`, "Runtime edits must not override Git policy.");
writeFileSync(`${home}/workspace/persistence-sentinel`, "preserve workspace");
writeFileSync(`${home}/conversation-sentinel`, "preserve conversations");
bootstrap();
const reapplied = JSON.parse(readFileSync(`${home}/openclaw.json`, "utf8"));
assert.deepEqual(reapplied.meta, metadata, "Migration metadata must survive config reapplication");
delete reapplied.meta;
assert.deepEqual(reapplied, JSON.parse(readFileSync("/config/openclaw.json", "utf8")));
assert.equal(readFileSync(`${home}/workspace/AGENTS.md`, "utf8"), readFileSync("/config/AGENTS.md", "utf8"));
assert.equal(readFileSync(`${home}/workspace/persistence-sentinel`, "utf8"), "preserve workspace");
assert.equal(readFileSync(`${home}/conversation-sentinel`, "utf8"), "preserve conversations");
const second = JSON.parse(cli(["plugins", "inspect", "slack", "--json"]));
assert.equal(second.install.installedAt, first.install.installedAt, "Repeat startup must not reinstall");
assert.equal(second.install.integrity, first.install.integrity, "Install metadata must survive");
assert(cli(["--version"]).includes(spec.version));

// Status-only/HTML catch-all responses must never satisfy Kubernetes probes.
let response = "<html>not a health endpoint</html>";
let status = 200;
const fake = createServer((_, res) => { res.writeHead(status); res.end(response); });
await new Promise(resolve => fake.listen(18789, "127.0.0.1", resolve));
for (const command of Object.values(spec.probes)) assert.notEqual(await execute(command), 0);
response = JSON.stringify({ ok: true, status: "started" });
for (const command of Object.values(spec.probes)) assert.equal(await execute(command), 0);
status = 503;
for (const command of Object.values(spec.probes)) assert.notEqual(await execute(command), 0);
await new Promise(resolve => fake.close(resolve));

console.log("BOOTSTRAP_READY_FOR_OFFLINE");
for (let i = 0; !existsSync("/tmp/offline-ready"); i++) {
  assert(i < 120, "Host must disconnect external network before starting the dummy gateway");
  await delay(1000);
}

const [program, ...entryArgs] = spec.entrypoint;
const gateway = spawn(program, [...entryArgs, ...spec.args], { stdio: "inherit" });
let outcome;
const exited = new Promise(resolve => gateway.once("exit", (code, signal) => {
  outcome = { code, signal };
  resolve(outcome);
}));
try {
  let healthy = false;
  for (let i = 0; i < spec.startupBudgetSeconds; i++) {
    if (outcome) throw new Error(`Gateway exited: ${JSON.stringify(outcome)}`);
    try {
      const result = await fetch("http://127.0.0.1:18789/startupz");
      const body = await result.json();
      if (result.status === 200 && body.ok === true && body.status === "started") {
        healthy = true;
        break;
      }
    } catch {}
    await delay(1000);
  }
  assert(healthy, "Official entrypoint must migrate and reach startup admission offline");
  for (const command of Object.values(spec.probes)) assert.equal(await execute(command), 0);
  for (const headers of [{}, { Authorization: "Bearer wrong-token" }]) {
    const result = await fetch("http://127.0.0.1:18789/tools/invoke", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify({ tool: "automations", args: { action: "list" } }),
    });
    assert.equal(result.status, 401, "Missing/incorrect token must fail");
  }
  const persisted = JSON.parse(readFileSync(`${home}/openclaw.json`));
  assert.equal(persisted.tools.gatewayPolicy.automations.scope, "caller");
  assert.equal(persisted.tools.gatewayPolicy.automations.allowCommandJobs, false);
  const afterActivation = JSON.parse(cli(["plugins", "inspect", "slack", "--runtime", "--json"]));
  assert.equal(afterActivation.plugin.version, spec.version);
  assert.equal(afterActivation.install.spec, first.install.spec, "Doctor must not unpin Slack");
  const schedules = JSON.parse(cli(["automations", "list", "--all", "--json"]));
  assert.deepEqual(schedules.jobs, [], "Fresh deployment must create no jobs");
  console.log("RUNTIME_TEST_PASS");
} finally {
  gateway.kill("SIGTERM");
  await Promise.race([exited, delay(15000)]);
  if (!outcome) {
    gateway.kill("SIGKILL");
    await exited;
  }
}
