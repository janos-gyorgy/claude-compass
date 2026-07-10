/**
 * Follow the compass log: read what's there, then poll for appended lines.
 *
 * fs.watchFile (stat polling) rather than fs.watch: it survives the file not
 * existing yet (the healthy case — the guard hasn't fired), editors replacing
 * the inode, and truncation. A monitor for a low-volume log doesn't need
 * inotify latency; 300ms polling is plenty and never misses.
 */
import * as fs from "node:fs";

export interface TailHandle {
  stop(): void;
}

export function tailFile(
  path: string,
  onLines: (lines: string[], initial: boolean) => void,
): TailHandle {
  let offset = 0;
  let leftover = "";

  const readFrom = (start: number, initial: boolean) => {
    let data: string;
    try {
      const fd = fs.openSync(path, "r");
      try {
        const size = fs.fstatSync(fd).size;
        if (size < start) {
          // truncated/rotated: start over
          start = 0;
          leftover = "";
        }
        const buf = Buffer.alloc(size - start);
        fs.readSync(fd, buf, 0, buf.length, start);
        offset = size;
        data = buf.toString("utf-8");
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      return; // file missing or unreadable — keep waiting
    }
    const chunk = leftover + data;
    const parts = chunk.split("\n");
    leftover = parts.pop() ?? ""; // last element is a partial line (or "")
    const lines = parts.filter((l) => l.trim() !== "");
    if (lines.length) onLines(lines, initial);
  };

  readFrom(0, true);

  const listener = () => readFrom(offset, false);
  fs.watchFile(path, { interval: 300 }, listener);

  return {
    stop() {
      fs.unwatchFile(path, listener);
    },
  };
}
