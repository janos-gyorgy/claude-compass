/**
 * Shared config-editor vectors (tests/conformance/config_vectors.json), the
 * same file tests/test_web.py asserts against web/compass_web.py. Both
 * editors must produce the same bytes.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { emptyRule, parseCustomRules, parseGroups, setValue, upsertRule } from "./config.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const doc = JSON.parse(
  fs.readFileSync(path.resolve(here, "../../tests/conformance/config_vectors.json"), "utf8"),
);
const FIELDS = ["name", "enabled", "on", "pattern", "action", "reason"] as const;

for (const v of doc.vectors) {
  test(`vector: ${v.name}`, () => {
    let text: string = v.text.startsWith("@") ? doc.fixtures[v.text.slice(1)] : v.text;
    if (v.op === "parseGroups") {
      assert.deepEqual(
        parseGroups(text).map((g) => [g.name, g.enabled, g.action]),
        v.expect.groups,
      );
      return;
    }
    if (v.op === "parseCustomRules") {
      assert.deepEqual(
        parseCustomRules(text).map((r) => Object.fromEntries(FIELDS.map((k) => [k, r[k]]))),
        v.expect.rules,
      );
      return;
    }
    const run = () => {
      for (const s of v.steps) {
        text =
          s[0] === "setValue"
            ? setValue(text, s[1], s[2], s[3])
            : upsertRule(text, s[1], { ...emptyRule(-1), ...s[2] });
      }
      return text;
    };
    if (v.expect.error) assert.throws(run);
    else assert.equal(run(), v.expect.text);
  });
}
