#!/usr/bin/env python3
"""Conformance runner: one contract, three implementations.

Drives every implementation of the compass hook as a real subprocess over the
shared vectors in vectors.json — event JSON on stdin, decision JSON on stdout,
exit 0 always — and asserts all of them make the same decision. This is the
suite that makes the ports *ports* rather than three tools that drift apart.

Usage:
    python3 tests/conformance/run.py            # run all available impls
    python3 tests/conformance/run.py --impl go  # just one (py|go|ts)
"""
import argparse
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

IMPLS = {
    "py": [sys.executable, str(ROOT / "claude_compass.py")],
    "go": [str(ROOT / "go" / "compass")],
    "ts": ["node", str(ROOT / "ts" / "dist" / "compass.mjs")],
}


def transcript_line(role: str, text: str) -> str:
    content = [{"type": "text", "text": text}] if role == "assistant" else text
    return json.dumps({"type": role, "message": {"role": role, "content": content}}) + "\n"


def classify(stdout: str):
    """Map raw stdout to a (kind, searchable-text) pair."""
    if not stdout.strip():
        return "silent", ""
    data = json.loads(stdout)
    hso = data.get("hookSpecificOutput", {})
    if hso.get("permissionDecision") == "deny":
        return "deny", hso.get("permissionDecisionReason", "")
    if data.get("decision") == "block":
        return "block", data.get("reason", "")
    if "systemMessage" in data and "decision" not in data:
        return "warn", data["systemMessage"]
    return f"unrecognized: {stdout[:80]}", stdout


def run_vector(cmd: list, vec: dict, tmpdir: Path) -> str:
    """Run one vector against one implementation; return '' on pass, else the failure."""
    cfg = tmpdir / "compass.toml"
    cfg.write_text(vec["config"])
    event = dict(vec["event"])

    tpath = None
    timer = None
    if "transcript" in vec:
        tpath = tmpdir / "transcript.jsonl"
        tpath.write_text("".join(transcript_line(l["role"], l["text"]) for l in vec["transcript"]))
        event["transcript_path"] = str(tpath)
    if "delayed" in vec:
        d = vec["delayed"]

        def _append():
            with tpath.open("a") as fh:
                fh.write(transcript_line(d["role"], d["text"]))

        timer = threading.Timer(d["delay_ms"] / 1000.0, _append)
        timer.start()

    try:
        p = subprocess.run(
            cmd, input=json.dumps(event), capture_output=True, text=True, timeout=10,
            env={"COMPASS_CONFIG": str(cfg), "COMPASS_LOG": "/dev/null", "PATH": "/usr/bin:/bin"},
        )
    finally:
        if timer:
            timer.cancel()

    if p.returncode != 0:
        return f"exit code {p.returncode} (must fail-open with 0): {p.stderr[:200]}"
    kind, text = classify(p.stdout)
    want = vec["expect"]
    if kind != want["kind"]:
        return f"expected {want['kind']}, got {kind} ({text[:120]})"
    if want.get("contains") and want["contains"] not in text:
        return f"reason missing {want['contains']!r}: {text[:120]}"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", choices=IMPLS, action="append",
                    help="implementation(s) to test; default: all that exist")
    args = ap.parse_args()

    vectors = json.loads((Path(__file__).parent / "vectors.json").read_text())["vectors"]
    names = args.impl or list(IMPLS)

    failures = 0
    for name in names:
        cmd = IMPLS[name]
        if not Path(cmd[-1]).exists():
            hint = "cd go && go build -o compass ." if name == "go" else "cd ts && npm i && npm run build"
            print(f"-- {name}: SKIP (binary missing; build with: {hint})")
            continue
        bad = []
        for vec in vectors:
            with tempfile.TemporaryDirectory() as td:
                err = run_vector(cmd, vec, Path(td))
            if err:
                bad.append((vec["name"], err))
        status = "ok" if not bad else f"{len(bad)} FAILED"
        print(f"-- {name}: {len(vectors) - len(bad)}/{len(vectors)} {status}")
        for vname, err in bad:
            print(f"     FAIL {vname}: {err}")
        failures += len(bad)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
