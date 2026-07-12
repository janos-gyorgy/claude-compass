/**
 * Line-surgical editor for the armed compass.toml (v2 config pane).
 *
 * Deliberately NOT a TOML round-tripper: it only ever rewrites the value
 * token on a group's `enabled` / `action` line, so comments, alignment and
 * every other key are untouchable by construction. Saves are atomic
 * (temp file + rename) with a `.bak` of the previous version.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export type Action = "block" | "warn";

export interface Group {
  name: string;
  enabled: boolean;
  action: Action | null;
  enabledLine: number;
  actionLine: number; // -1 when the group has no action key
}

export function configPath(): string {
  return (
    process.env.COMPASS_CONFIG ||
    path.join(os.homedir(), ".claude", "compass.toml")
  );
}

const HEADER = /^\s*\[([A-Za-z0-9_]+)\]\s*(?:#.*)?$/;
const ARRAY_HEADER = /^\s*\[\[([A-Za-z0-9_]+)\]\]\s*(?:#.*)?$/;
const ENABLED = /^\s*enabled\s*=\s*(true|false)\b/;
const ACTION = /^\s*action\s*=\s*['"](block|warn)['"]/;

/** Groups that carry an `enabled` key, in file order. */
export function parseGroups(text: string): Group[] {
  const groups: Group[] = [];
  let current: Group | null = null;
  text.split("\n").forEach((line, i) => {
    if (ARRAY_HEADER.test(line)) {
      // a [[custom_rules]] block: its enabled/action lines belong to the rule,
      // never to the preceding group
      current = null;
      return;
    }
    const h = line.match(HEADER);
    if (h) {
      current = {
        name: h[1],
        enabled: false,
        action: null,
        enabledLine: -1,
        actionLine: -1,
      };
      return;
    }
    if (!current) return;
    const e = line.match(ENABLED);
    if (e && current.enabledLine < 0) {
      current.enabled = e[1] === "true";
      current.enabledLine = i;
      if (!groups.includes(current)) groups.push(current);
      return;
    }
    const a = line.match(ACTION);
    if (a && current.actionLine < 0) {
      current.action = a[1] as Action;
      current.actionLine = i;
      if (!groups.includes(current)) groups.push(current);
    }
  });
  return groups.filter((g) => g.enabledLine >= 0);
}

/**
 * Return new file text with exactly one value token changed. Throws if the
 * group/key isn't present — never falls back to appending or rewriting.
 */
export function setValue(
  text: string,
  group: string,
  key: "enabled" | "action",
  value: string,
): string {
  const g = parseGroups(text).find((x) => x.name === group);
  if (!g) throw new Error(`no group [${group}] with an enabled key`);
  const lineNo = key === "enabled" ? g.enabledLine : g.actionLine;
  if (lineNo < 0) throw new Error(`[${group}] has no ${key} key`);
  const lines = text.split("\n");
  const before = lines[lineNo];
  const after =
    key === "enabled"
      ? before.replace(/(=\s*)(true|false)\b/, `$1${value}`)
      : before.replace(/(=\s*")(block|warn)(")/, `$1${value}$3`);
  if (after === before && !before.includes(value)) {
    throw new Error(`could not rewrite ${key} on line ${lineNo + 1}`);
  }
  lines[lineNo] = after;
  return lines.join("\n");
}

// ── [[custom_rules]] blocks ─────────────────────────────────────────────────

export type Surface = "pretool" | "stop";

export interface CustomRule {
  name: string;
  enabled: boolean; // key absent = true (writing a rule is opting in)
  on: Surface;
  pattern: string;
  action: Action;
  reason: string;
  headerLine: number;
  /** line number per key, -1 when the key isn't written */
  lines: Record<RuleKey, number>;
}

export type RuleKey = "name" | "enabled" | "on" | "pattern" | "action" | "reason";
const RULE_KEYS: RuleKey[] = ["name", "enabled", "on", "pattern", "action", "reason"];

// `key = 'literal'` or `key = "basic"` — lazy close-quote match; good for
// TUI-authored (always literal-quoted) and typical hand-authored lines.
const STR_KEY = (k: string) => new RegExp(`^\\s*${k}\\s*=\\s*(['"])(.*?)\\1`);

export function emptyRule(headerLine: number): CustomRule {
  return {
    name: "",
    enabled: true,
    on: "pretool",
    pattern: "",
    action: "warn",
    reason: "",
    headerLine,
    lines: { name: -1, enabled: -1, on: -1, pattern: -1, action: -1, reason: -1 },
  };
}

/** All [[custom_rules]] blocks, in file order. */
export function parseCustomRules(text: string): CustomRule[] {
  const rules: CustomRule[] = [];
  let current: CustomRule | null = null;
  text.split("\n").forEach((line, i) => {
    const ah = line.match(ARRAY_HEADER);
    if (ah || HEADER.test(line)) {
      current = ah && ah[1] === "custom_rules" ? emptyRule(i) : null;
      if (current) rules.push(current);
      return;
    }
    if (!current) return;
    const e = line.match(ENABLED);
    if (e && current.lines.enabled < 0) {
      current.enabled = e[1] === "true";
      current.lines.enabled = i;
      return;
    }
    for (const k of ["name", "on", "pattern", "action", "reason"] as const) {
      const m = line.match(STR_KEY(k));
      if (m && current.lines[k] < 0) {
        (current as any)[k] = m[2];
        current.lines[k] = i;
        return;
      }
    }
  });
  return rules;
}

/** TOML-quote a string: literal single quotes unless the value contains one. */
function tomlString(v: string): string {
  return v.includes("'") ? JSON.stringify(v) : `'${v}'`;
}

function ruleLine(key: RuleKey, rule: CustomRule): string {
  const pad = key.padEnd(7);
  if (key === "enabled") return `${pad} = ${rule.enabled}`;
  return `${pad} = ${tomlString(String(rule[key]))}`;
}

/**
 * Insert a new rule (index null) or update rule `index` in place. Updates are
 * line-surgical per key: existing key lines get their value token rewritten
 * (leading whitespace + trailing comment kept), missing keys are inserted
 * right after the [[custom_rules]] header. New rules append a block at EOF.
 */
export function upsertRule(
  text: string,
  index: number | null,
  rule: CustomRule,
): string {
  if (index === null) {
    const block = ["[[custom_rules]]", ...RULE_KEYS
      .filter((k) => k !== "reason" || rule.reason !== "")
      .map((k) => ruleLine(k, rule))].join("\n");
    const sep = text.endsWith("\n") ? "\n" : "\n\n";
    return text + sep + block + "\n";
  }
  const existing = parseCustomRules(text)[index];
  if (!existing) throw new Error(`no custom rule #${index + 1}`);
  const lines = text.split("\n");
  const inserts: string[] = [];
  for (const k of RULE_KEYS) {
    const lineNo = existing.lines[k];
    if (lineNo < 0) {
      if (k === "reason" && rule.reason === "") continue;
      inserts.push(ruleLine(k, rule));
      continue;
    }
    const before = lines[lineNo];
    const value = k === "enabled" ? String(rule.enabled) : tomlString(String(rule[k]));
    // function replacement: a `$` inside a regex pattern value must not be
    // interpreted as a replacement-string directive
    const after =
      k === "enabled"
        ? before.replace(/(=\s*)(true|false)\b/, (_m, p) => p + value)
        : before.replace(/(=\s*)(['"]).*?\2/, (_m, p) => p + value);
    if (after === before && !before.includes(value)) {
      throw new Error(`could not rewrite ${k} on line ${lineNo + 1}`);
    }
    lines[lineNo] = after;
  }
  lines.splice(existing.headerLine + 1, 0, ...inserts);
  return lines.join("\n");
}

/** Atomic save: copy the current file to <file>.bak, then temp + rename. */
export function saveConfig(file: string, newText: string): void {
  fs.copyFileSync(file, `${file}.bak`);
  const tmp = path.join(
    path.dirname(file),
    `.${path.basename(file)}.tmp-${process.pid}`,
  );
  fs.writeFileSync(tmp, newText);
  fs.renameSync(tmp, file);
}
