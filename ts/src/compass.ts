/**
 * claude-compass — a personal, deterministic principle-guard for Claude Code.
 *
 * TypeScript port of claude_compass.py, same contract: read the hook event
 * JSON on stdin, check it against compass.toml (where every rule ships OFF),
 * and either block (permissionDecision: "deny"), warn (systemMessage), or
 * stay silent. Fail-open: any error → no output, exit 0.
 *
 * The Python promise "zero dependencies, zero network" translates here to
 * "one committed bundle, no network at runtime" — the TOML parser is bundled
 * into dist/compass.mjs at build time, so a fresh clone runs on bare node.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseToml } from "smol-toml";

type Dict = Record<string, unknown>;

// ---------------------------------------------------------------------------
// config
// ---------------------------------------------------------------------------

/** Load compass.toml. Missing/broken config → {} (everything stays off). */
function loadConfig(): Dict {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const cfgPath = process.env.COMPASS_CONFIG || path.join(here, "..", "compass.toml");
  try {
    return parseToml(fs.readFileSync(cfgPath, "utf-8")) as Dict;
  } catch {
    return {};
  }
}

function group(cfg: Dict, name: string): Dict {
  const g = cfg[name];
  return typeof g === "object" && g !== null ? (g as Dict) : {};
}

function getBool(g: Dict, key: string, def: boolean): boolean {
  return typeof g[key] === "boolean" ? (g[key] as boolean) : def;
}

function getInt(g: Dict, key: string, def: number): number {
  const v = g[key];
  if (typeof v === "number") return Math.trunc(v);
  if (typeof v === "bigint") return Number(v);
  return def;
}

function getStr(g: Dict, key: string, def: string): string {
  return typeof g[key] === "string" ? (g[key] as string) : def;
}

function getStrList(g: Dict, key: string): string[] | null {
  const v = g[key];
  if (!Array.isArray(v)) return null;
  return v.filter((x): x is string => typeof x === "string");
}

// ---------------------------------------------------------------------------
// emit helpers (the Claude Code hook output contract)
// ---------------------------------------------------------------------------

function emit(obj: unknown): void {
  process.stdout.write(JSON.stringify(obj));
}

function denyPretool(reason: string): void {
  emit({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: `compass: ${reason}`,
    },
  });
}

/** systemMessage is shown to YOU in the transcript; Claude does not see it. */
function warnUser(reason: string): void {
  emit({ systemMessage: `compass ⚠ ${reason}` });
}

/** On Stop, decision:block feeds the reason back and makes Claude revise. */
function blockStop(reason: string): void {
  emit({ decision: "block", reason: `compass: ${reason}` });
}

/**
 * Append every fired rule to a durable log so warns aren't invisible
 * (systemMessage has no guaranteed rendering on a Stop hook). Only called
 * when a rule fires; fail-open, errors swallowed.
 */
function logFired(action: string, reason: string, on: string): void {
  try {
    const p =
      process.env.COMPASS_LOG || path.join(os.homedir(), ".claude", "compass-warns.log");
    fs.mkdirSync(path.dirname(p), { recursive: true });
    const ts = new Date().toISOString().slice(0, 19);
    fs.appendFileSync(p, `[${ts}] ${action.toUpperCase().padEnd(5)} on=${on}  ${reason}\n`);
  } catch {
    /* never brick the guard */
  }
}

/** Apply a rule's configured action ('block' | 'warn'). */
function act(action: string, reason: string, on: "pretool" | "stop"): void {
  logFired(action, reason, on);
  if (action === "warn") warnUser(reason);
  else if (action === "block") {
    if (on === "pretool") denyPretool(reason);
    else blockStop(reason);
  }
}

// ---------------------------------------------------------------------------
// rule checks (each returns a human reason string on a hit, else "")
// ---------------------------------------------------------------------------

const RM = /\brm\s+(?:-\S*[rf]\S*|--recursive|--force)/;
const DISK = /\bdd\b[^\n]*\bof=\/dev\/|\bmkfs\b|>\s*\/dev\/sd|>\s*\/dev\/nvme/;
const CURL_SH = /\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|d)?sh\b/;
const CHMOD777 = /\bchmod\s+(?:-R\s+)?0?777\b/;
const FORKBOMB = /:\(\)\s*\{\s*:\|:&\s*\}\s*;:/;

function truncate(s: string, n: number): string {
  return s.trim().slice(0, n);
}

/**
 * fnmatch-style glob → RegExp, matching Python fnmatch semantics:
 * * matches everything (including /), ? one char, [seq] sets.
 */
function fnmatchTranslate(pat: string): RegExp | null {
  let out = "^";
  for (let i = 0; i < pat.length; i++) {
    const c = pat[i];
    if (c === "*") out += ".*";
    else if (c === "?") out += ".";
    else if (c === "[") {
      let j = i + 1;
      if (j < pat.length && (pat[j] === "!" || pat[j] === "^")) j++;
      if (j < pat.length && pat[j] === "]") j++;
      while (j < pat.length && pat[j] !== "]") j++;
      if (j >= pat.length) out += "\\[";
      else {
        let inner = pat.slice(i + 1, j);
        if (inner.startsWith("!")) inner = "^" + inner.slice(1);
        out += `[${inner}]`;
        i = j;
      }
    } else out += c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }
  try {
    return new RegExp(out + "$");
  } catch {
    return null;
  }
}

function fnmatch(name: string, pat: string): boolean {
  const re = fnmatchTranslate(pat);
  return re !== null && re.test(name);
}

/** Dangerous shell commands + edits to secret files. */
function checkDangerous(tool: string, tinput: Dict, g: Dict): string {
  if (tool === "Bash") {
    const cmd = String(tinput["command"] ?? "");
    if (getBool(g, "rm_rf", true) && RM.test(cmd))
      return `destructive rm blocked → ${truncate(cmd, 120)}`;
    if (getBool(g, "disk_destroyers", true) && DISK.test(cmd))
      return `disk-destroying command blocked → ${truncate(cmd, 120)}`;
    if (getBool(g, "curl_pipe_shell", true) && CURL_SH.test(cmd))
      return `curl|sh pipe-to-shell blocked → ${truncate(cmd, 120)}`;
    if (getBool(g, "chmod_777", true) && CHMOD777.test(cmd))
      return `chmod 777 blocked → ${truncate(cmd, 120)}`;
    if (FORKBOMB.test(cmd)) return "fork bomb blocked";
    for (const pat of getStrList(g, "extra_command_patterns") ?? []) {
      try {
        if (new RegExp(pat).test(cmd))
          return `matched extra_command_pattern /${pat}/ → ${truncate(cmd, 100)}`;
      } catch {
        continue;
      }
    }
    return "";
  }

  // edits to secret files
  if (["Edit", "Write", "MultiEdit"].includes(tool) && getBool(g, "secret_file_edits", true)) {
    const fp = String(tinput["file_path"] ?? tinput["path"] ?? "");
    if (fp) {
      const name = path.basename(fp);
      for (const pat of getStrList(g, "secret_path_globs") ?? []) {
        if (fnmatch(fp, pat) || fnmatch(name, pat))
          return `edit to secret file blocked → ${fp}`;
      }
    }
  }
  return "";
}

const FORCE_PUSH = /--force(?:-with-lease)?\b|\s-f\b/;

/** git push to a protected branch / force-push. */
function checkGit(cmd: string, g: Dict): string {
  if (!cmd.includes("git push")) return "";
  if (getBool(g, "force_push", true) && FORCE_PUSH.test(cmd))
    return `force-push blocked → ${truncate(cmd, 120)}`;
  if (getBool(g, "push_to_protected", true)) {
    const protectedBranches = getStrList(g, "protected_branches") ?? ["main", "master"];
    const alt = protectedBranches
      .map((b) => b.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
      .join("|");
    if (alt && new RegExp(`git push\\s+\\S+\\s+(?:${alt})\\b`).test(cmd))
      return `push to protected branch blocked → ${truncate(cmd, 120)}`;
  }
  return "";
}

const SUPER =
  /\b(amazing|incredible|fantastic|excellent|perfect|brilliant|wonderful|awesome|superb|stellar|exceptional|flawless|phenomenal)\b/gi;
const DEFAULT_SYC = [
  "great question",
  "you're absolutely right",
  "you are absolutely right",
  "i'm thrilled",
  "i am thrilled",
  "happy to help",
  "what a great",
  "excellent question",
  "that's a fantastic",
];
const CLOSER =
  /(happy to help|always here|let me know if you|feel free to|great work|you've got this|excited to|i'm here to help)/i;

function checkSycophancy(text: string, g: Dict): string {
  const low = text.toLowerCase();
  const phrases = getStrList(g, "phrases") ?? DEFAULT_SYC;
  const found = phrases.filter((p) => low.includes(p.toLowerCase()));
  if (found.length) return "flattery phrase(s): " + found.slice(0, 3).join(", ");
  if (getBool(g, "flag_superlative_pileups", true)) {
    const n = (text.match(SUPER) ?? []).length;
    if (n >= getInt(g, "superlative_threshold", 3))
      return `superlative pile-up (${n} in one message)`;
  }
  if (getBool(g, "flag_gushing_closers", true)) {
    const lines = text.trim().split("\n");
    const tail = text.trim() ? lines[lines.length - 1] : "";
    if (CLOSER.test(tail)) return "gushing closer";
  }
  return "";
}

const DEFAULT_EXPANSION = [
  "while i was at it",
  "went ahead and also",
  "took the liberty",
  "as a bonus",
  "i also added",
  "also refactored",
  "additionally, i",
  "i also went ahead",
  "for good measure",
];

/**
 * Deterministic *proxy* for unrequested scope expansion — NOT true intent
 * drift. Flags language that signals the agent did more than asked.
 */
function checkScopeDrift(text: string, g: Dict): string {
  const low = text.toLowerCase();
  const found = (getStrList(g, "expansion_phrases") ?? DEFAULT_EXPANSION).filter((p) =>
    low.includes(p),
  );
  if (found.length)
    return "unrequested scope-expansion language: " + found.slice(0, 3).join(", ");
  return "";
}

// Self-report: the model flags itself via <<compass:CODE>> markers (see
// CLAUDE.snippet.md). The hook just greps for the token — still 100% local
// and deterministic, but the judgment is the running model's.
const MARKER = /<<\s*compass\s*:\s*(\w+)\s*>>/gi;
const MARKER_MEANING: Record<string, string> = {
  drift: "self-flagged: going beyond / away from what was asked",
  scope: "self-flagged: adding unrequested scope",
  unsure: "self-flagged: guessing / low confidence / unverified",
  assume: "self-flagged: proceeding on an unconfirmed assumption",
  flattery: "self-flagged: being sycophantic",
  risk: "self-flagged: risky / hard-to-reverse action",
};
const DEFAULT_MARKERS = Object.keys(MARKER_MEANING);

/** Returns [reason, escalateToBlock] for enabled self-report markers. */
function checkSelfReport(text: string, g: Dict): [string, boolean] {
  const enabled = new Set(
    (getStrList(g, "markers") ?? DEFAULT_MARKERS).map((m) => m.toLowerCase()),
  );
  const blockMarkers = new Set(
    (getStrList(g, "block_markers") ?? []).map((m) => m.toLowerCase()),
  );
  const hits: string[] = [];
  let escalate = false;
  for (const m of text.matchAll(MARKER)) {
    const code = m[1].toLowerCase();
    if (!enabled.has(code)) continue;
    hits.push(MARKER_MEANING[code] ?? `self-flagged: ${code}`);
    if (blockMarkers.has(code)) escalate = true;
  }
  if (!hits.length) return ["", false];
  const uniq = [...new Set(hits)];
  return [uniq.slice(0, 4).join(" | "), escalate];
}

// ---------------------------------------------------------------------------
// transcript reading (for Stop)
// ---------------------------------------------------------------------------

function blockText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts: string[] = [];
    for (const b of content) {
      if (typeof b === "object" && b !== null && (b as Dict)["type"] === "text")
        parts.push(String((b as Dict)["text"] ?? ""));
      else if (typeof b === "string") parts.push(b);
    }
    return parts.join("\n");
  }
  return "";
}

/** Walk the JSONL transcript calling fn(role, text) per message. */
function scanTranscript(p: string, fn: (role: string, text: string) => void): boolean {
  let data: string;
  try {
    data = fs.readFileSync(p, "utf-8");
  } catch {
    return false;
  }
  for (let line of data.split("\n")) {
    line = line.trim();
    if (!line) continue;
    let obj: Dict;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    const msg =
      typeof obj["message"] === "object" && obj["message"] !== null
        ? (obj["message"] as Dict)
        : obj;
    const role = String(msg["role"] ?? obj["type"] ?? "");
    fn(role, blockText(msg["content"]));
  }
  return true;
}

function lastAssistantText(p: string): string {
  if (!p) return "";
  let last = "";
  scanTranscript(p, (role, text) => {
    if (role === "assistant" && text.trim()) last = text;
  });
  return last;
}

/**
 * All assistant text in the final turn (reset on each user message).
 * Self-report markers may be emitted mid-turn, not just in the last reply.
 */
function assistantTextSinceLastUser(p: string): string {
  if (!p) return "";
  let buf: string[] = [];
  scanTranscript(p, (role, text) => {
    if (role === "user") buf = []; // new turn
    else if (role === "assistant" && text.trim()) buf.push(text);
  });
  return buf.join("\n");
}

/**
 * True once the just-finished assistant turn has been flushed to the
 * transcript, i.e. the last text-bearing message is an assistant one.
 * Claude Code fires the Stop hook slightly before the final turn is written,
 * so a single read sees a one-message-stale file.
 */
function finalTurnPresent(p: string): boolean {
  let last = "";
  const ok = scanTranscript(p, (role, text) => {
    if (role === "user") last = "user";
    else if (role === "assistant" && text.trim()) last = "assistant";
  });
  return ok && last === "assistant";
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Give the transcript writer a moment to flush the turn that triggered Stop.
 * Fail-open: on timeout we just scan whatever is there.
 */
async function awaitFinalTurn(p: string): Promise<void> {
  if (!p) return;
  for (let i = 0; i < 6; i++) {
    if (finalTurnPresent(p)) return;
    await sleep(30);
  }
}

// ---------------------------------------------------------------------------
// event handlers
// ---------------------------------------------------------------------------

function handlePretool(ev: Dict, cfg: Dict): void {
  const tool = String(ev["tool_name"] ?? "");
  const tinput =
    typeof ev["tool_input"] === "object" && ev["tool_input"] !== null
      ? (ev["tool_input"] as Dict)
      : {};

  let g = group(cfg, "dangerous_tools");
  if (getBool(g, "enabled", false)) {
    const hit = checkDangerous(tool, tinput, g);
    if (hit) return act(getStr(g, "action", "block"), hit, "pretool");
  }

  g = group(cfg, "git_safety");
  if (getBool(g, "enabled", false) && tool === "Bash") {
    const hit = checkGit(String(tinput["command"] ?? ""), g);
    if (hit) return act(getStr(g, "action", "block"), hit, "pretool");
  }
}

async function handleStop(ev: Dict, cfg: Dict): Promise<void> {
  const syc = group(cfg, "sycophancy");
  const drift = group(cfg, "scope_drift");
  const selfrep = group(cfg, "self_report");
  if (
    !(
      getBool(syc, "enabled", false) ||
      getBool(drift, "enabled", false) ||
      getBool(selfrep, "enabled", false)
    )
  )
    return;
  const p = String(ev["transcript_path"] ?? "");

  // The Stop hook fires before the final turn is flushed; wait for it to land.
  await awaitFinalTurn(p);

  // Self-report scans the whole final turn (markers may be emitted mid-turn).
  if (getBool(selfrep, "enabled", false)) {
    const turn = assistantTextSinceLastUser(p);
    if (turn) {
      const [reason, escalate] = checkSelfReport(turn, selfrep);
      if (reason) {
        const action = escalate ? "block" : getStr(selfrep, "action", "warn");
        return act(action, reason, "stop");
      }
    }
  }

  // Pattern checks look at the final reply.
  const text = lastAssistantText(p);
  if (!text) return;
  if (getBool(syc, "enabled", false)) {
    const hit = checkSycophancy(text, syc);
    if (hit) return act(getStr(syc, "action", "warn"), hit, "stop");
  }
  if (getBool(drift, "enabled", false)) {
    const hit = checkScopeDrift(text, drift);
    if (hit) return act(getStr(drift, "action", "warn"), hit, "stop");
  }
}

async function main(): Promise<void> {
  let raw = "";
  if (!process.stdin.isTTY) {
    for await (const chunk of process.stdin) raw += chunk;
  }
  let ev: Dict;
  try {
    ev = raw.trim() ? JSON.parse(raw) : {};
  } catch {
    return;
  }
  const cfg = loadConfig();
  if (!Object.keys(cfg).length || typeof ev !== "object" || ev === null) return;
  const name = ev["hook_event_name"];
  if (name === "PreToolUse") handlePretool(ev, cfg);
  else if (name === "Stop" || name === "SubagentStop") await handleStop(ev, cfg);
}

// fail-open: never break the session, always exit 0
main()
  .catch(() => {})
  .finally(() => process.exit(0));
