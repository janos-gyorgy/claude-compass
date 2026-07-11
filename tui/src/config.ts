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
const ENABLED = /^\s*enabled\s*=\s*(true|false)\b/;
const ACTION = /^\s*action\s*=\s*"(block|warn)"/;

/** Groups that carry an `enabled` key, in file order. */
export function parseGroups(text: string): Group[] {
  const groups: Group[] = [];
  let current: Group | null = null;
  text.split("\n").forEach((line, i) => {
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
