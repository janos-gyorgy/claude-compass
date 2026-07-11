import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { parseGroups, saveConfig, setValue } from "./config.js";

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

test("parseGroups finds groups, values and line numbers", () => {
  const g = parseGroups(FIXTURE);
  assert.deepEqual(
    g.map((x) => [x.name, x.enabled, x.action]),
    [
      ["dangerous_tools", true, "block"],
      ["git_safety", false, "block"],
      ["self_report", true, "warn"],
    ],
  );
});

test("setValue flips exactly one line and nothing else", () => {
  const out = setValue(FIXTURE, "git_safety", "enabled", "true");
  const a = FIXTURE.split("\n");
  const b = out.split("\n");
  const changed = a.flatMap((line, i) => (line === b[i] ? [] : [i]));
  assert.deepEqual(changed, [11]);
  assert.equal(b[11], "enabled = true");
});

test("setValue on action preserves alignment and trailing text", () => {
  const out = setValue(FIXTURE, "dangerous_tools", "action", "warn");
  assert.ok(out.includes('action  = "warn"')); // double space kept
  // the header comment's quoted words and block_markers line are untouched
  assert.ok(out.includes('-> deny (mentions "warn" too'));
  assert.ok(out.includes('block_markers = ["risk"]   # escalate'));
});

test("setValue never edits an absent key", () => {
  assert.throws(() => setValue(FIXTURE, "nope", "enabled", "true"));
  const noAction = "[g]\nenabled = true\n";
  assert.throws(() => setValue(noAction, "g", "action", "warn"));
});

test("round-trip: toggle back restores the original byte-for-byte", () => {
  const flipped = setValue(FIXTURE, "self_report", "enabled", "false");
  assert.equal(setValue(flipped, "self_report", "enabled", "true"), FIXTURE);
});

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
