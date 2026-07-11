import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import React from "react";
import { render } from "ink-testing-library";
import { App } from "./app.js";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test("renders counts, colours rows, and picks up live appends", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ctui-app-"));
  const p = path.join(dir, "w.log");
  fs.writeFileSync(
    p,
    "[2026-07-10T10:32:52] BLOCK on=pretool  destructive rm blocked → rm -rf /\n" +
      "[2026-07-10T10:15:52] WARN  on=stop  flattery phrase(s): you're absolutely right\n",
  );
  const { lastFrame, unmount } = render(<App logPath={p} />);
  try {
    await sleep(50);
    let frame = lastFrame() ?? "";
    assert.match(frame, /BLOCK 1/);
    assert.match(frame, /WARN 1/);
    assert.match(frame, /dangerous_tools 1/);
    assert.match(frame, /sycophancy 1/);
    assert.match(frame, /destructive rm blocked/);
    assert.match(frame, /since launch: 0/);

    fs.appendFileSync(
      p,
      "[2026-07-10T10:40:00] BLOCK on=stop  self-flagged: risky / hard-to-reverse action\n",
    );
    await sleep(700);
    frame = lastFrame() ?? "";
    assert.match(frame, /BLOCK 2/);
    assert.match(frame, /since launch: 1/);
    assert.match(frame, /self_report 1/);
  } finally {
    unmount();
  }
});

test("a long reason stays on one row instead of wrapping into the columns", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ctui-wrap-"));
  const p = path.join(dir, "w.log");
  const longCmd = "cd ~/git && python3 - << PYEOF import re p = x ".repeat(8);
  fs.writeFileSync(
    p,
    `[2026-07-11T14:19:41] BLOCK on=pretool  destructive rm blocked → ${longCmd}\n` +
      "[2026-07-11T14:20:00] WARN  on=stop  flattery phrase(s): ok\n",
  );
  const { lastFrame, unmount } = render(<App logPath={p} />);
  try {
    await sleep(50);
    const lines = (lastFrame() ?? "").split("\n");
    // exactly one line carries each entry's timestamp — no spill-over rows
    assert.equal(lines.filter((l) => l.includes("14:19:41")).length, 1);
    assert.equal(lines.filter((l) => l.includes("14:20:00")).length, 1);
    // the long entry's line is truncated, not wrapped: no line consists of
    // reason-continuation without a leading timestamp column
    const cont = lines.filter((l) => l.includes("PYEOF") && !l.includes("14:19:41"));
    assert.equal(cont.length, 0);
  } finally {
    unmount();
  }
});

test("empty state explains itself", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ctui-empty-"));
  const { lastFrame, unmount } = render(
    <App logPath={path.join(dir, "none.log")} />,
  );
  try {
    await sleep(50);
    assert.match(lastFrame() ?? "", /healthy case/);
  } finally {
    unmount();
  }
});
