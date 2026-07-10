import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { tailFile } from "./tail.js";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const L1 = "[2026-07-10T10:00:00] WARN  on=stop  gushing closer";
const L2 = "[2026-07-10T10:00:01] BLOCK on=pretool  fork bomb blocked";

function tmpLog(): string {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), "ctui-")), "w.log");
}

test("reads existing content as initial, appended lines as live", async () => {
  const p = tmpLog();
  fs.writeFileSync(p, L1 + "\n");
  const got: Array<{ lines: string[]; initial: boolean }> = [];
  const h = tailFile(p, (lines, initial) => got.push({ lines, initial }));
  try {
    assert.deepEqual(got, [{ lines: [L1], initial: true }]);
    fs.appendFileSync(p, L2 + "\n");
    await sleep(700); // > poll interval
    assert.deepEqual(got[1], { lines: [L2], initial: false });
  } finally {
    h.stop();
  }
});

test("missing file is fine; lines arrive once it appears", async () => {
  const p = tmpLog(); // never created until below
  const got: string[][] = [];
  const h = tailFile(p, (lines) => got.push(lines));
  try {
    assert.equal(got.length, 0);
    fs.writeFileSync(p, L1 + "\n");
    await sleep(700);
    assert.deepEqual(got, [[L1]]);
  } finally {
    h.stop();
  }
});

test("truncation resets instead of erroring", async () => {
  const p = tmpLog();
  fs.writeFileSync(p, L1 + "\n" + L2 + "\n");
  const got: string[][] = [];
  const h = tailFile(p, (lines) => got.push(lines));
  try {
    fs.writeFileSync(p, L1 + "\n"); // shrink: rotation/truncate
    await sleep(700);
    assert.deepEqual(got.at(-1), [L1]); // re-read from 0, no crash
  } finally {
    h.stop();
  }
});
