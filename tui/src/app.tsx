import fs from "node:fs";
import React, { useEffect, useMemo, useState } from "react";
import { Box, Text, useApp, useInput, useStdin, useStdout } from "ink";
import { Group, parseGroups, saveConfig, setValue } from "./config.js";
import { Entry, parseLine } from "./parse.js";
import { tailFile } from "./tail.js";

type Filter = "all" | "block" | "warn";
type Mode = "monitor" | "config";

const GROUP_ORDER = [
  "dangerous_tools",
  "git_safety",
  "sycophancy",
  "scope_drift",
  "self_report",
  "other",
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
  const [cursor, setCursor] = useState(0);
  const [confMsg, setConfMsg] = useState("");

  // Read fresh from disk each time so we never clobber an outside edit.
  const loadConfig = () => {
    try {
      setGroups(parseGroups(fs.readFileSync(confPath!, "utf8")));
      return true;
    } catch (err) {
      setConfMsg(`cannot read ${confPath}: ${(err as Error).message}`);
      return false;
    }
  };

  const editConfig = (key: "enabled" | "action", value: string) => {
    const g = groups[cursor];
    if (!g || (key === "action" && g.actionLine < 0)) return;
    try {
      const text = fs.readFileSync(confPath!, "utf8");
      saveConfig(confPath!, setValue(text, g.name, key, value));
      setConfMsg(`saved — [${g.name}] ${key} = ${value} (.bak kept)`);
    } catch (err) {
      setConfMsg(`NOT saved: ${(err as Error).message}`);
    }
    loadConfig();
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

  useInput(
    (input, key) => {
      if (input === "q") exit();
      else if (mode === "config") {
        if (input === "c" || key.escape) setMode("monitor");
        else if (key.upArrow) setCursor((i) => Math.max(0, i - 1));
        else if (key.downArrow)
          setCursor((i) => Math.min(groups.length - 1, i + 1));
        else if (input === " " || key.return)
          editConfig("enabled", groups[cursor]?.enabled ? "false" : "true");
        else if (input === "b") editConfig("action", "block");
        else if (input === "w") editConfig("action", "warn");
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
        {confMsg !== "" && (
          <Text color={confMsg.startsWith("NOT") ? "red" : "green"}>
            {confMsg}
          </Text>
        )}
        <Text dimColor>{"─".repeat(Math.min(stdout?.columns ?? 80, 100))}</Text>
        <Text dimColor>
          ↑/↓ select · space toggle on/off · b block · w warn · r reload · c
          back · q quit
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
