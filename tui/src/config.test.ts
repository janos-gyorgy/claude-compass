import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { saveConfig, setValue } from "./config.js";

// Parsing and editing cases live in tests/conformance/config_vectors.json
// (config.vectors.test.ts), shared with web/compass_web.py.

const FIXTURE = `# claude-compass — header comment stays put.
#     action = "block"  -> deny (mentions "warn" too; must not be edited)

# ── PreToolUse ──────────
[dangerous_tools]
enabled = true
action  = "block"
rm_rf              = true     # rm -rf / -fr
secret_path_globs  = ["*.env", "credentials"]

[git_safety]
enabled = false
action  = "block"
force_push         = true     # --force / -f

[self_report]
enabled = true
action  = "warn"
markers = ["drift", "risk"]
block_markers = ["risk"]   # escalate even when action="warn"
`;

test("saveConfig writes .bak of the previous version and swaps atomically", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "compass-tui-"));
  const file = path.join(dir, "compass.toml");
  fs.writeFileSync(file, FIXTURE);
  const next = setValue(FIXTURE, "git_safety", "enabled", "true");
  saveConfig(file, next);
  assert.equal(fs.readFileSync(file, "utf8"), next);
  assert.equal(fs.readFileSync(`${file}.bak`, "utf8"), FIXTURE);
  assert.deepEqual(
    fs.readdirSync(dir).sort(),
    ["compass.toml", "compass.toml.bak"], // no temp file left behind
  );
  fs.rmSync(dir, { recursive: true, force: true });
});

test("saveConfig keeps the file mode (the hook may be another user)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "compass-tui-"));
  const file = path.join(dir, "compass.toml");
  fs.writeFileSync(file, FIXTURE);
  fs.chmodSync(file, 0o644);
  const old = process.umask(0o077);
  try {
    saveConfig(file, setValue(FIXTURE, "git_safety", "enabled", "true"));
  } finally {
    process.umask(old);
  }
  assert.equal(fs.statSync(file).mode & 0o777, 0o644);
  fs.rmSync(dir, { recursive: true, force: true });
});
