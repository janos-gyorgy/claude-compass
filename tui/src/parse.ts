/**
 * Parser for compass-warns.log lines. The hook (Python and Go impls alike)
 * writes exactly:
 *
 *   [<ISO8601-seconds>] <ACTION> on=<where>  <reason>
 *
 * where ACTION is BLOCK|WARN left-padded to width 5, where is pretool|stop,
 * and reason is free text (often contains a → and a truncated command).
 */

export type Action = "BLOCK" | "WARN";

export interface Entry {
  ts: string; // ISO timestamp as written
  action: Action;
  on: string; // pretool | stop
  group: string; // rule group inferred from the reason's leading phrase
  reason: string;
  raw: string;
}

const LINE = /^\[([^\]]+)\]\s+(BLOCK|WARN)\s+on=(\S+)\s+(.*)$/;

/**
 * The hook doesn't log which rule group fired, but every group's reason
 * strings have stable leading phrases (they're contract-tested), so the
 * group can be inferred. Unknown phrasings fall back to "other".
 */
const GROUP_PHRASES: [string, string][] = [
  ["destructive rm blocked", "dangerous_tools"],
  ["disk-destroying command blocked", "dangerous_tools"],
  ["curl|sh pipe-to-shell blocked", "dangerous_tools"],
  ["chmod 777 blocked", "dangerous_tools"],
  ["fork bomb blocked", "dangerous_tools"],
  ["matched extra_command_pattern", "dangerous_tools"],
  ["edit to secret file blocked", "dangerous_tools"],
  ["force-push blocked", "git_safety"],
  ["push to protected branch blocked", "git_safety"],
  ["flattery phrase(s):", "sycophancy"],
  ["superlative pile-up", "sycophancy"],
  ["gushing closer", "sycophancy"],
  ["unrequested scope-expansion language", "scope_drift"],
  ["self-flagged:", "self_report"],
];

export function classify(reason: string): string {
  for (const [phrase, group] of GROUP_PHRASES) {
    if (reason.startsWith(phrase)) return group;
  }
  return "other";
}

export function parseLine(line: string): Entry | null {
  const m = LINE.exec(line);
  if (!m) return null;
  const [, ts, action, on, reason] = m;
  return {
    ts,
    action: action as Action,
    on,
    group: classify(reason),
    // A blocked command can drag control chars (tabs, CRs, ANSI escapes)
    // into the reason; any of them shreds the row layout. One line, plain.
    reason: reason.replace(/[\x00-\x1f\x7f]+/g, " "),
    raw: line,
  };
}
