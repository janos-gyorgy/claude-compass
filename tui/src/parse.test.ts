import { test } from "node:test";
import assert from "node:assert/strict";
import { classify, parseLine } from "./parse.js";

test("parses a real BLOCK line (verbatim from a live log)", () => {
  const e = parseLine(
    "[2026-07-10T10:32:52] BLOCK on=pretool  destructive rm blocked → rm -rf /",
  );
  assert.ok(e);
  assert.equal(e.ts, "2026-07-10T10:32:52");
  assert.equal(e.action, "BLOCK");
  assert.equal(e.on, "pretool");
  assert.equal(e.group, "dangerous_tools");
  assert.equal(e.reason, "destructive rm blocked → rm -rf /");
});

test("parses a WARN line (padded action, stop event)", () => {
  const e = parseLine(
    "[2026-07-10T10:15:52] WARN  on=stop  flattery phrase(s): you're absolutely right",
  );
  assert.ok(e);
  assert.equal(e.action, "WARN");
  assert.equal(e.on, "stop");
  assert.equal(e.group, "sycophancy");
});

test("classifies every rule group by its leading phrase", () => {
  const cases: [string, string][] = [
    ["destructive rm blocked → rm -rf /x", "dangerous_tools"],
    ["edit to secret file blocked → /app/.env", "dangerous_tools"],
    ["matched extra_command_pattern /x/ → git reset", "dangerous_tools"],
    ["force-push blocked → git push -f", "git_safety"],
    ["push to protected branch blocked → git push origin main", "git_safety"],
    ["flattery phrase(s): great question", "sycophancy"],
    ["superlative pile-up (4 in one message)", "sycophancy"],
    ["gushing closer", "sycophancy"],
    ["unrequested scope-expansion language: took the liberty", "scope_drift"],
    ["self-flagged: risky / hard-to-reverse action", "self_report"],
    ["some future phrasing", "other"],
  ];
  for (const [reason, group] of cases) {
    assert.equal(classify(reason), group, reason);
  }
});

test("rejects garbage lines instead of guessing", () => {
  assert.equal(parseLine(""), null);
  assert.equal(parseLine("not a log line"), null);
  assert.equal(parseLine("[ts] NOPE on=x  reason"), null);
});
