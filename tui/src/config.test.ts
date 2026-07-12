import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import {
  emptyRule,
  parseCustomRules,
  parseGroups,
  saveConfig,
  setValue,
  upsertRule,
} from "./config.js";

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

const RULES_FIXTURE = `${FIXTURE}
# your own regexes
[[custom_rules]]
name    = 'kubectl-delete-namespace'
enabled = false
on      = 'pretool'
pattern = 'kubectl\\s+delete\\s+(ns|namespace)\\b'
action  = 'block'
reason  = 'Tier-1 — ask first'

[[custom_rules]]
name    = "should-work-tell"
on      = "stop"
pattern = "(?i)\\bshould work\\b"
action  = "warn"
`;

test("parseCustomRules reads both blocks; missing enabled defaults true", () => {
  const r = parseCustomRules(RULES_FIXTURE);
  assert.deepEqual(
    r.map((x) => [x.name, x.enabled, x.on, x.action]),
    [
      ["kubectl-delete-namespace", false, "pretool", "block"],
      ["should-work-tell", true, "stop", "warn"],
    ],
  );
  assert.equal(r[0].pattern, "kubectl\\s+delete\\s+(ns|namespace)\\b");
  assert.equal(r[1].lines.enabled, -1);
});

test("custom rule enabled lines never leak into the preceding group", () => {
  const g = parseGroups(RULES_FIXTURE);
  assert.deepEqual(
    g.map((x) => [x.name, x.enabled]),
    [
      ["dangerous_tools", true],
      ["git_safety", false],
      ["self_report", true],
    ],
  );
});

test("upsertRule toggles enabled in place, one line changed", () => {
  const r = parseCustomRules(RULES_FIXTURE)[0];
  const out = upsertRule(RULES_FIXTURE, 0, { ...r, enabled: true });
  const a = RULES_FIXTURE.split("\n");
  const b = out.split("\n");
  const changed = a.flatMap((line, i) => (line === b[i] ? [] : [i]));
  assert.deepEqual(changed, [r.lines.enabled]);
  assert.ok(b[r.lines.enabled].includes("enabled = true"));
});

test("upsertRule inserts a missing enabled line after the header", () => {
  const r = parseCustomRules(RULES_FIXTURE)[1];
  const out = upsertRule(RULES_FIXTURE, 1, { ...r, enabled: false });
  const rules = parseCustomRules(out);
  assert.equal(rules[1].enabled, false);
  assert.ok(rules[1].lines.enabled >= 0);
  // first rule untouched
  assert.equal(rules[0].enabled, false);
});

test("upsertRule appends a new block; engine-parsable; $ in pattern survives", () => {
  const rule = {
    ...emptyRule(-1),
    name: "no-prod-kubeconfig",
    on: "stop" as const,
    pattern: "KUBECONFIG=\\S*prod\\S*$",
    action: "block" as const,
  };
  const out = upsertRule(RULES_FIXTURE, null, rule);
  const rules = parseCustomRules(out);
  assert.equal(rules.length, 3);
  assert.equal(rules[2].name, "no-prod-kubeconfig");
  assert.equal(rules[2].pattern, "KUBECONFIG=\\S*prod\\S*$");
  assert.equal(rules[2].enabled, true);
  // update the appended rule: $ must not be mangled by replacement semantics
  const out2 = upsertRule(out, 2, { ...rules[2], action: "warn" as const });
  assert.equal(parseCustomRules(out2)[2].pattern, "KUBECONFIG=\\S*prod\\S*$");
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
