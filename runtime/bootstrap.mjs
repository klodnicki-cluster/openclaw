import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const state = process.env.OPENCLAW_STATE_DIR ?? "/home/node/.openclaw";
const configDir = process.env.BOOTSTRAP_CONFIG_DIR ?? "/config";
const plugin = JSON.parse(readFileSync(new URL("./plugin.json", import.meta.url)));
const configPath = process.env.OPENCLAW_CONFIG_PATH ?? join(state, "openclaw.json");
mkdirSync(join(state, "workspace"), { recursive: true });
mkdirSync(join(state, ".cache"), { recursive: true });
process.env.npm_config_cache ??= join(state, ".cache/npm");
process.env.XDG_CACHE_HOME ??= join(state, ".cache");

// Preserve runtime migration bookkeeping, never runtime edits to managed settings.
const managed = JSON.parse(readFileSync(join(configDir, "openclaw.json"), "utf8"));
try {
  const previous = JSON.parse(readFileSync(configPath, "utf8"));
  if (previous?.meta && typeof previous.meta === "object" && !Array.isArray(previous.meta)) {
    managed.meta = { ...previous.meta, ...managed.meta };
  }
} catch (error) {
  if (error.code !== "ENOENT" && !(error instanceof SyntaxError)) throw error;
}
// Only these two files are Git-owned. SQLite state, plugins, jobs, and chats remain.
writeFileSync(`${configPath}.git-tmp`, JSON.stringify(managed, null, 2) + "\n", { mode: 0o600 });
renameSync(`${configPath}.git-tmp`, configPath);
copyFileSync(join(configDir, "AGENTS.md"), join(state, "workspace/AGENTS.md.git-tmp"));
renameSync(join(state, "workspace/AGENTS.md.git-tmp"), join(state, "workspace/AGENTS.md"));

const marker = join(state, ".slack-release.json");
let installed;
try {
  installed = JSON.parse(readFileSync(marker, "utf8"));
} catch {
  // First activation or an interrupted install must complete before the gateway.
}
const spec = `npm:${plugin.name}@${plugin.version}`;
function cli(args, capture = false) {
  const result = spawnSync("node", ["/app/openclaw.mjs", ...args], {
    encoding: "utf8",
    stdio: capture ? ["ignore", "pipe", "inherit"] : "inherit",
    env: process.env,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`OpenClaw ${args[0]} ${args[1]} failed (${result.status})`);
  }
  return result.stdout;
}

// Inspect on every boot; a marker alone must not hide a damaged/missing install.
let healthy = false;
if (installed?.spec === spec) {
  try {
    const info = JSON.parse(cli(["plugins", "inspect", "slack", "--json"], true));
    healthy = info.plugin?.version === plugin.version
      && info.plugin?.status === "loaded"
      && info.plugin?.trustedOfficialInstall === true
      && info.plugin?.dependencyStatus?.requiredInstalled === true
      && info.install?.resolvedVersion === plugin.version;
  } catch {
    healthy = false;
  }
}
if (!healthy) {
  cli(["plugins", "install", spec, "--pin", "--force", "--accept-capabilities"]);
  writeFileSync(`${marker}.tmp`, JSON.stringify({ spec }) + "\n", { mode: 0o600 });
  renameSync(`${marker}.tmp`, marker);
}
cli(["config", "validate"]);
