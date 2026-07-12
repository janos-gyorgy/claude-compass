import fs from "node:fs";
import React, { useEffect, useMemo, useState } from "react";
import { Box, Text, useApp, useInput, useStdin, useStdout } from "ink";
import {
  Action,
  CustomRule,
  Group,
  emptyRule,
  parseCustomRules,
  parseGroups,
  saveConfig,
  setValue,
  upsertRule,
} from "./config.js";
import { Entry, parseLine } from "./parse.js";
import { tailFile } from "./tail.js";

type Filter = "all" | "block" | "warn";
type Mode = "monitor" | "config" | "form";

const GROUP_ORDER = [
  "dangerous_tools",
  "git_safety",
  "sycophancy",
  "scope_drift",
  "self_report",
  "custom_rules",
  "other",
];

// The add/edit form for a [[custom_rules]] block, in display order.
type FieldKey = "name" | "on" | "pattern" | "action" | "enabled" | "reason";
const FORM_FIELDS: { key: FieldKey; kind: "text" | "toggle"; options?: string[] }[] = [
  { key: "name", kind: "text" },
  { key: "on", kind: "toggle", options: ["pretool", "stop"] },
  { key: "pattern", kind: "text" },
  { key: "action", kind: "toggle", options: ["warn", "block"] },
  { key: "enabled", kind: "toggle", options: ["true", "false"] },
  { key: "reason", kind: "text" },
];

function shortTs(iso: string): string {
  const t = iso.indexOf("T");
  return t >= 0 ? iso.slice(t + 1) : iso;
}

function Row({ e }: { e: Entry }) {
  // One terminal row per entry, always: the parent Text truncates the whole
  // composed line instead of letting a long reason wrap and shred the columns.
  return (
    <Text wrap="truncate-end">
      <Text dimColor>{shortTs(e.ts)} </Text>
      <Text color={e.action === "BLOCK" ? "red" : "yellow"} bold>
        {e.action.padEnd(5)}
      </Text>
      <Text color="cyan"> {e.on.padEnd(7)}</Text>
      <Text dimColor> {e.group.padEnd(15)}</Text>
      <Text> {e.reason}</Text>
    </Text>
  );
}

function ConfigRow({ g, selected }: { g: Group; selected: boolean }) {
  return (
    <Box>
      <Text color={selected ? "cyan" : undefined} bold={selected}>
        {selected ? "❯ " : "  "}
        {g.name.padEnd(17)}
      </Text>
      <Text color={g.enabled ? "green" : undefined} dimColor={!g.enabled} bold>
        {(g.enabled ? "on" : "off").padEnd(5)}
      </Text>
      {g.action && (
        <Text color={g.action === "block" ? "red" : "yellow"}>{g.action}</Text>
      )}
    </Box>
  );
}

function RuleRow({ r, selected }: { r: CustomRule; selected: boolean }) {
  return (
    <Text wrap="truncate-end">
      <Text color={selected ? "cyan" : undefined} bold={selected}>
        {selected ? "❯ " : "  "}
        {(r.name || "(unnamed)").padEnd(17)}
      </Text>
      <Text color={r.enabled ? "green" : undefined} dimColor={!r.enabled} bold>
        {(r.enabled ? "on" : "off").padEnd(5)}
      </Text>
      <Text color={r.action === "block" ? "red" : "yellow"}>
        {r.action.padEnd(6)}
      </Text>
      <Text color="cyan">{r.on.padEnd(8)}</Text>
      <Text dimColor>/{r.pattern}/</Text>
    </Text>
  );
}

export function App({
  logPath,
  confPath,
}: {
  logPath: string;
  confPath?: string;
}) {
  const { exit } = useApp();
  const { isRawModeSupported } = useStdin();
  const { stdout } = useStdout();
  const [entries, setEntries] = useState<Entry[]>([]);
  const [sinceLaunch, setSinceLaunch] = useState(0);
  const [filter, setFilter] = useState<Filter>("all");
  const [scroll, setScroll] = useState(0); // 0 = pinned to newest
  const [mode, setMode] = useState<Mode>("monitor");
  const [groups, setGroups] = useState<Group[]>([]);
  const [rules, setRules] = useState<CustomRule[]>([]);
  const [cursor, setCursor] = useState(0);
  const [confMsg, setConfMsg] = useState("");
  // form state: which rule is being edited (null = new), draft values, field cursor
  const [formIndex, setFormIndex] = useState<number | null>(null);
  const [draft, setDraft] = useState<CustomRule>(emptyRule(-1));
  const [field, setField] = useState(0);
  const [formMsg, setFormMsg] = useState("");

  // Read fresh from disk each time so we never clobber an outside edit.
  const loadConfig = () => {
    try {
      const text = fs.readFileSync(confPath!, "utf8");
      setGroups(parseGroups(text));
      setRules(parseCustomRules(text));
      return true;
    } catch (err) {
      setConfMsg(`cannot read ${confPath}: ${(err as Error).message}`);
      return false;
    }
  };

  const editConfig = (key: "enabled" | "action", value: string) => {
    try {
      const text = fs.readFileSync(confPath!, "utf8");
      if (cursor < groups.length) {
        const g = groups[cursor];
        if (!g || (key === "action" && g.actionLine < 0)) return;
        saveConfig(confPath!, setValue(text, g.name, key, value));
        setConfMsg(`saved — [${g.name}] ${key} = ${value} (.bak kept)`);
      } else {
        const idx = cursor - groups.length;
        const r = rules[idx];
        if (!r) return;
        const next = { ...r, [key]: key === "enabled" ? value === "true" : value };
        saveConfig(confPath!, upsertRule(text, idx, next as CustomRule));
        setConfMsg(`saved — rule '${r.name}' ${key} = ${value} (.bak kept)`);
      }
    } catch (err) {
      setConfMsg(`NOT saved: ${(err as Error).message}`);
    }
    loadConfig();
  };

  const openForm = (idx: number | null) => {
    setFormIndex(idx);
    setDraft(idx === null ? emptyRule(-1) : { ...rules[idx] });
    setField(0);
    setFormMsg("");
    setMode("form");
  };

  const submitForm = () => {
    if (!draft.name.trim()) return setFormMsg("name must not be empty");
    if (!draft.pattern) return setFormMsg("pattern must not be empty");
    try {
      new RegExp(draft.pattern); // JS-flavored sanity check only
    } catch (err) {
      return setFormMsg(`pattern does not compile: ${(err as Error).message}`);
    }
    try {
      const text = fs.readFileSync(confPath!, "utf8");
      saveConfig(confPath!, upsertRule(text, formIndex, draft));
      setConfMsg(
        `saved — rule '${draft.name}' ${formIndex === null ? "added" : "updated"} (.bak kept)`,
      );
    } catch (err) {
      return setFormMsg(`NOT saved: ${(err as Error).message}`);
    }
    loadConfig();
    setMode("config");
  };

  const formInput = (input: string, key: any) => {
    if (key.escape) return setMode("config");
    if (key.return) return submitForm();
    if (key.upArrow || (key.tab && key.shift)) {
      return setField((f) => (f - 1 + FORM_FIELDS.length) % FORM_FIELDS.length);
    }
    if (key.downArrow || key.tab) {
      return setField((f) => (f + 1) % FORM_FIELDS.length);
    }
    const fd = FORM_FIELDS[field];
    if (fd.kind === "toggle") {
      if (input === " " || key.leftArrow || key.rightArrow) {
        setDraft((d) => {
          const opts = fd.options!;
          const cur = String(d[fd.key]);
          const next = opts[(opts.indexOf(cur) + 1) % opts.length];
          return { ...d, [fd.key]: fd.key === "enabled" ? next === "true" : next };
        });
      }
      return;
    }
    if (key.backspace || key.delete) {
      setDraft((d) => ({ ...d, [fd.key]: String(d[fd.key]).slice(0, -1) }));
      return;
    }
    if (input && !key.ctrl && !key.meta) {
      setDraft((d) => ({ ...d, [fd.key]: String(d[fd.key]) + input }));
    }
  };

  useEffect(() => {
    const handle = tailFile(logPath, (lines, initial) => {
      const parsed = lines
        .map(parseLine)
        .filter((e): e is Entry => e !== null);
      if (!parsed.length) return;
      setEntries((prev) => [...prev, ...parsed]);
      if (!initial) setSinceLaunch((n) => n + parsed.length);
    });
    return () => handle.stop();
  }, [logPath]);

  const rows_total = groups.length + rules.length;
  const selRule = cursor >= groups.length ? rules[cursor - groups.length] : null;

  useInput(
    (input, key) => {
      if (mode === "form") return formInput(input, key);
      if (input === "q") exit();
      else if (mode === "config") {
        if (input === "c" || key.escape) setMode("monitor");
        else if (key.upArrow) setCursor((i) => Math.max(0, i - 1));
        else if (key.downArrow)
          setCursor((i) => Math.min(rows_total - 1, i + 1));
        else if (input === " " || key.return)
          editConfig(
            "enabled",
            (cursor < groups.length ? groups[cursor]?.enabled : selRule?.enabled)
              ? "false"
              : "true",
          );
        else if (input === "b") editConfig("action", "block");
        else if (input === "w") editConfig("action", "warn");
        else if (input === "n") openForm(null);
        else if (input === "e" && selRule) openForm(cursor - groups.length);
        else if (input === "r") loadConfig() && setConfMsg("reloaded");
      } else if (input === "c" && confPath) {
        setConfMsg("");
        if (loadConfig()) setMode("config");
      } else if (input === "b") setFilter("block");
      else if (input === "w") setFilter("warn");
      else if (input === "a") setFilter("all");
      else if (key.upArrow) setScroll((s) => s + 1);
      else if (key.downArrow) setScroll((s) => Math.max(0, s - 1));
    },
    { isActive: isRawModeSupported },
  );

  const filtered = useMemo(
    () =>
      filter === "all"
        ? entries
        : entries.filter((e) => e.action === filter.toUpperCase()),
    [entries, filter],
  );

  const counts = useMemo(() => {
    const byGroup = new Map<string, number>();
    let block = 0;
    let warn = 0;
    for (const e of entries) {
      byGroup.set(e.group, (byGroup.get(e.group) ?? 0) + 1);
      if (e.action === "BLOCK") block++;
      else warn++;
    }
    return { byGroup, block, warn };
  }, [entries]);

  const rows = Math.max(5, (stdout?.rows ?? 24) - 7);
  const maxScroll = Math.max(0, filtered.length - rows);
  const clamped = Math.min(scroll, maxScroll);
  const end = filtered.length - clamped;
  const visible = filtered.slice(Math.max(0, end - rows), end);

  if (mode === "form") {
    return (
      <Box flexDirection="column">
        <Box>
          <Text bold color="cyan">
            compass-tui · custom rule{" "}
            {formIndex === null ? "(new)" : `#${formIndex + 1}`}
          </Text>
          <Text dimColor> — writes a [[custom_rules]] block to {confPath}</Text>
        </Box>
        <Text dimColor>{"─".repeat(Math.min(stdout?.columns ?? 80, 100))}</Text>
        {FORM_FIELDS.map((fd, i) => {
          const sel = i === field;
          const val = String(draft[fd.key]);
          return (
            <Text key={fd.key} wrap="truncate-end">
              <Text color={sel ? "cyan" : undefined} bold={sel}>
                {sel ? "❯ " : "  "}
                {fd.key.padEnd(9)}
              </Text>
              {fd.kind === "toggle" ? (
                <Text>
                  {fd.options!.map((o, j) => (
                    <Text
                      key={o}
                      bold={o === val}
                      color={o === val ? "green" : undefined}
                      dimColor={o !== val}
                    >
                      {j > 0 ? " / " : ""}
                      {o}
                    </Text>
                  ))}
                </Text>
              ) : (
                <Text>
                  {val}
                  {sel ? <Text color="cyan">▌</Text> : null}
                </Text>
              )}
            </Text>
          );
        })}
        {formMsg !== "" && <Text color="red">{formMsg}</Text>}
        <Text dimColor>{"─".repeat(Math.min(stdout?.columns ?? 80, 100))}</Text>
        <Text dimColor>
          ↑/↓/tab field · type/backspace edit · space/←→ toggle · enter save ·
          esc cancel
        </Text>
      </Box>
    );
  }

  if (mode === "config") {
    return (
      <Box flexDirection="column">
        <Box>
          <Text bold color="cyan">
            compass-tui · config
          </Text>
          <Text dimColor> — editing {confPath} (guard reads it live)</Text>
        </Box>
        <Text dimColor>{"─".repeat(Math.min(stdout?.columns ?? 80, 100))}</Text>
        {groups.map((g, i) => (
          <ConfigRow key={g.name} g={g} selected={i === cursor} />
        ))}
        {rules.length > 0 && <Text dimColor>  — custom rules —</Text>}
        {rules.map((r, i) => (
          <RuleRow
            key={`${r.headerLine}-${r.name}`}
            r={r}
            selected={groups.length + i === cursor}
          />
        ))}
        {confMsg !== "" && (
          <Text color={confMsg.startsWith("NOT") ? "red" : "green"}>
            {confMsg}
          </Text>
        )}
        <Text dimColor>{"─".repeat(Math.min(stdout?.columns ?? 80, 100))}</Text>
        <Text dimColor>
          ↑/↓ select · space toggle on/off · b block · w warn · n new rule
          {selRule ? " · e edit rule" : ""} · r reload · c back · q quit
        </Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      <Text wrap="truncate-end">
        <Text bold color="cyan">
          compass-tui
        </Text>
        <Text dimColor> — watching {logPath}</Text>
      </Text>
      <Text wrap="truncate-end">
        <Text color="red" bold>
          BLOCK {counts.block}
        </Text>
        <Text> </Text>
        <Text color="yellow" bold>
          WARN {counts.warn}
        </Text>
        <Text dimColor> · since launch: {sinceLaunch}</Text>
        <Text dimColor>
          {" · "}
          {GROUP_ORDER.filter((g) => counts.byGroup.has(g))
            .map((g) => `${g} ${counts.byGroup.get(g)}`)
            .join(" · ") || "no firings"}
        </Text>
      </Text>
      <Text dimColor>{"─".repeat(Math.min(stdout?.columns ?? 80, 100))}</Text>
      {filtered.length === 0 ? (
        <Box paddingY={1}>
          <Text dimColor>
            {entries.length === 0
              ? "No firings logged — the guard hasn't caught anything. That's the normal, healthy case."
              : `No ${filter.toUpperCase()} entries (filter active — press a for all).`}
          </Text>
        </Box>
      ) : (
        <Box flexDirection="column">
          {visible.map((e, i) => (
            <Row key={`${e.raw}-${i}`} e={e} />
          ))}
        </Box>
      )}
      <Text dimColor>{"─".repeat(Math.min(stdout?.columns ?? 80, 100))}</Text>
      <Text dimColor>
        b blocks · w warns · a all · ↑/↓ scroll
        {clamped > 0 ? ` (${clamped} back)` : ""} · filter: {filter}
        {confPath ? " · c config" : ""} · q quit
      </Text>
    </Box>
  );
}
