"""Tests for web/compass_web.py. Pure stdlib: `python3 -m unittest -v` from repo root."""
import http.client
import importlib.util
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("compass_web", ROOT / "web" / "compass_web.py")
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)
w.Handler.log_message = lambda *a, **k: None  # quiet test output

VECTORS = json.loads((ROOT / "tests" / "conformance" / "config_vectors.json").read_text(encoding="utf-8"))
TOKEN = "t" * 40
FIELDS = ("name", "enabled", "on", "pattern", "action", "reason")

CONFIG = """# comment stays
[git_safety]
enabled = true
action  = "block"   # aligned

[network]
expected = ["api.anthropic.com", "*.fatchilli.com"]

[[custom_rules]]
name    = 'rm-warn'
on      = 'pretool'
pattern = 'rm\\s+-rf'
action  = 'warn'
"""


class TestConfigVectors(unittest.TestCase):
    """The same vectors tui/src/config.vectors.test.ts runs."""

    def test_vectors(self):
        for v in VECTORS["vectors"]:
            with self.subTest(v["name"]):
                text = VECTORS["fixtures"][v["text"][1:]] if v["text"].startswith("@") else v["text"]
                if v.get("op") == "parseGroups":
                    got = [[g["name"], g["enabled"], g["action"]] for g in w.parse_groups(text)]
                    self.assertEqual(got, v["expect"]["groups"])
                    continue
                if v.get("op") == "parseCustomRules":
                    got = [{k: r[k] for k in FIELDS} for r in w.parse_custom_rules(text)]
                    self.assertEqual(got, v["expect"]["rules"])
                    continue

                def run(t=text):
                    for s in v["steps"]:
                        t = (w.set_value(t, *s[1:]) if s[0] == "setValue"
                             else w.upsert_rule(t, s[1], {**w.empty_rule(), **s[2]}))
                    return t

                if v["expect"].get("error"):
                    self.assertRaises(w.EditError, run)
                else:
                    self.assertEqual(run(), v["expect"]["text"])

    def test_save_keeps_mode_and_writes_bak(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "compass.toml"
            f.write_text(CONFIG)
            os.chmod(f, 0o644)
            old = os.umask(0o077)
            try:
                w.save_config(f, CONFIG.replace("true", "false"))
            finally:
                os.umask(old)
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o644)
            self.assertEqual((Path(d) / "compass.toml.bak").read_text(), CONFIG)
            self.assertEqual(sorted(os.listdir(d)), ["compass.toml", "compass.toml.bak"])


class TestPatternCheck(unittest.TestCase):
    def test_accepts_what_both_engines_accept(self):
        for p in (r"kubectl\s+delete", r"(?i)\bshould work\b", r"[(?=]x", r"a{2,3}", r"(?P<n>x)"):
            self.assertEqual(w.pattern_error(p), "", p)

    def test_refuses_python_or_go_only(self):
        for p in ("(", r"(?=x)", r"(?<!git )rm", r"(a)\1", r"a++", r"x\Z", r"(?>a)"):
            self.assertNotEqual(w.pattern_error(p), "", p)


class TestTail(unittest.TestCase):
    def test_partial_lines_wait_and_truncation_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log"
            p.write_text("one\ntw")
            lines, cur, _ = w.read_since(str(p), "")
            self.assertEqual(lines, ["one"])
            with p.open("a") as f:
                f.write("o\n")
            lines, cur, restarted = w.read_since(str(p), cur)
            self.assertEqual((lines, restarted), (["two"], False))
            p.write_text("new\n")  # truncation, same inode
            lines, cur, restarted = w.read_since(str(p), cur)
            self.assertEqual((lines, restarted), (["new"], True))

    def test_rotation_restarts_from_the_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log"
            p.write_text("a\nb\nc\n")
            _, cur, _ = w.read_since(str(p), "")
            os.rename(p, Path(d) / "log.1")
            lines, cur2, _ = w.read_since(str(p), cur)
            self.assertEqual((lines, cur2), ([], cur))  # missing: wait, keep cursor
            p.write_text("fresh\n")
            lines, _, restarted = w.read_since(str(p), cur)
            self.assertEqual((lines, restarted), (["fresh"], True))

    def test_cap_reads_only_the_tail_on_whole_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log"
            p.write_text("".join(f"line{i:04d}\n" for i in range(1000)))
            lines, _, _ = w.read_since(str(p), "", cap=95)
            self.assertTrue(lines and all(re.fullmatch(r"line\d{4}", ln) for ln in lines))
            self.assertEqual(lines[-1], "line0999")

    def test_net_rotation_and_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "net"
            p.write_text('{"ts":"2026-11-20T15:00:00","domain":"Registry.NPMjs.org.","kind":"dns"}\n'
                         'not json\n{"domain":"no-ts.example"}\n')
            agg = w.NetAggregate(str(p))
            agg.refresh()
            self.assertEqual([r["domain"] for r in agg.rows()], ["registry.npmjs.org"])
            os.rename(p, Path(d) / "net.1")
            p.write_text('{"ts":"2026-11-20T16:00:00","domain":"registry.npmjs.org"}\n')
            agg.refresh()
            row = agg.rows()[0]
            self.assertEqual((row["count"], row["first"], row["last"]),
                             (2, "2026-11-20T15:00:00", "2026-11-20T16:00:00"))


class ServerCase(unittest.TestCase):
    token = TOKEN

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.cfg, self.log, self.net = d / "compass.toml", d / "warns.log", d / "net.log"
        self.cfg.write_text(CONFIG)
        self.log.write_text("[2026-11-20T15:00:00] BLOCK on=pretool  destructive rm blocked → "
                            "echo '<img src=x onerror=alert(1)>'\n")
        self.net.write_text('{"ts":"2026-11-20T15:00:00","domain":"api.anthropic.com"}\n'
                            '{"ts":"2026-11-20T15:00:01","domain":"evil.example"}\n')
        env = {"COMPASS_CONFIG": str(self.cfg), "COMPASS_LOG": str(self.log), "COMPASS_NETLOG": str(self.net)}
        if self.token is not None:
            tf = d / "token"
            tf.write_text(self.token + "\n")
            os.chmod(tf, 0o600)
            env["COMPASS_WEB_TOKEN_FILE"] = str(tf)
        with mock.patch.dict(os.environ, env, clear=False):
            if self.token is None:
                os.environ.pop("COMPASS_WEB_TOKEN_FILE", None)
            self.settings = w.Settings("127.0.0.1", 0)
        self.srv = w.make_server(self.settings)
        self.port = self.settings.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"localhost:{self.port}"}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw.decode()
        return r.status, parsed, dict(r.getheaders())

    def auth(self, tok=TOKEN):
        return {"Authorization": f"Bearer {tok}"}


class TestHTTP(ServerCase):
    def test_reads_work_without_token_and_carry_headers(self):
        for path in ("/", "/app.js", "/app.css", "/api/state", "/api/events", "/api/net"):
            st, _, h = self.req("GET", path)
            self.assertEqual(st, 200, path)
            self.assertIn("default-src 'none'", h["Content-Security-Policy"])
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")
            self.assertEqual(h["Cache-Control"], "no-store")
            self.assertNotIn("Access-Control-Allow-Origin", h)
        st, state, _ = self.req("GET", "/api/state")
        self.assertTrue(state["editable"])
        self.assertNotIn(TOKEN, json.dumps(state))

    def test_wrong_host_refused(self):
        for host in ("evil.example:%d", "localhost:1", "127.0.0.1.nip.io:%d"):
            st, _, _ = self.req("GET", "/api/state", host=host.replace("%d", str(self.port)))
            self.assertEqual(st, 403, host)

    def test_write_needs_the_header_token(self):
        body = {"group": "git_safety", "enabled": False}
        self.assertEqual(self.req("POST", "/api/group", body)[0], 401)
        self.assertEqual(self.req("POST", "/api/group", body, self.auth("x" * 40))[0], 401)
        self.assertEqual(self.req("POST", f"/api/group?token={TOKEN}", body)[0], 401)
        self.assertEqual(self.req("POST", "/api/group", body, {"Cookie": f"token={TOKEN}"})[0], 401)
        self.assertIn("enabled = true", self.cfg.read_text())

    def test_good_write_saves_and_logs(self):
        st, r, _ = self.req("POST", "/api/group", {"group": "git_safety", "enabled": False}, self.auth())
        self.assertEqual(st, 200, r)
        text = self.cfg.read_text()
        self.assertEqual(text, CONFIG.replace("enabled = true", "enabled = false", 1))
        self.assertIn("CONFIG on=web  git_safety.enabled: true -> false", self.log.read_text())
        st, r, _ = self.req("POST", "/api/rule", {"index": 0, "current_name": "rm-warn", "rule": {
            "name": "rm-warn", "on": "pretool", "pattern": r"rm\s+-rf", "action": "warn",
            "enabled": False, "reason": ""}}, self.auth())
        self.assertEqual(st, 200, r)
        self.assertEqual(w.parse_custom_rules(self.cfg.read_text())[0]["enabled"], False)
        st, r, _ = self.req("POST", "/api/rule", {"index": None, "rule": {
            "name": "no-lookahead", "on": "stop", "pattern": "(?=x)", "action": "warn",
            "enabled": True, "reason": ""}}, self.auth())
        self.assertEqual(st, 400)
        st, r, _ = self.req("POST", "/api/rule", {"index": None, "rule": {
            "name": "multi", "on": "stop", "pattern": "x", "action": "warn",
            "enabled": True, "reason": "a\n[git_safety]"}}, self.auth())
        self.assertEqual(st, 400)
        self.assertNotIn(TOKEN, self.log.read_text())

    def test_bad_tokens_never_lock_out_the_right_one(self):
        body = {"group": "git_safety", "enabled": False}
        codes = [self.req("POST", "/api/group", body, self.auth("y" * 40))[0] for _ in range(8)]
        self.assertEqual(codes, [401] * 8)
        self.assertEqual(self.req("POST", "/api/group", body, self.auth())[0], 200)
        self.assertIn("AUTH   on=web  write refused: bad token", self.log.read_text())

    def test_body_limits_and_content_type(self):
        st, _, _ = self.req("POST", "/api/group", b"x" * (w.MAX_BODY + 1), self.auth())
        self.assertEqual(st, 413)
        st, _, _ = self.req("POST", "/api/group", b"{}", {**self.auth(), "Content-Type": "text/plain"})
        self.assertEqual(st, 415)

    def test_hostile_reason_returned_unchanged(self):
        _, ev, _ = self.req("GET", "/api/events")
        self.assertEqual(ev["entries"][0]["reason"],
                         "destructive rm blocked → echo '<img src=x onerror=alert(1)>'")

    def test_network_marks_expected(self):
        _, net, _ = self.req("GET", "/api/net")
        marks = {d["domain"]: d["expected"] for d in net["domains"]}
        self.assertEqual(marks, {"api.anthropic.com": True, "evil.example": False})

    def test_page_has_no_html_sinks_or_inline_code(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(sink, w.APP_JS)
        self.assertNotRegex(w.INDEX_HTML, r"<script(?![^>]*\bsrc=)")
        self.assertNotRegex(w.INDEX_HTML, r"\son[a-z]+=|\sstyle=")


class TestViewOnly(ServerCase):
    token = None

    def test_every_write_is_403(self):
        _, state, _ = self.req("GET", "/api/state")
        self.assertFalse(state["editable"])
        for path, body in (("/api/group", {"group": "git_safety", "enabled": False}),
                           ("/api/rule", {"index": None, "rule": {}})):
            self.assertEqual(self.req("POST", path, body, self.auth())[0], 403)
        self.assertEqual(self.cfg.read_text(), CONFIG)


class TestSettings(unittest.TestCase):
    def test_loose_token_file_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "token"
            tf.write_text(TOKEN)
            os.chmod(tf, 0o644)
            with mock.patch.dict(os.environ, {"COMPASS_WEB_TOKEN_FILE": str(tf)}):
                s = w.Settings("127.0.0.1", 8377)
            self.assertIsNone(s.token)
            self.assertTrue(s.errors)

    def test_remote_host_with_token_needs_flag(self):
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "token"
            tf.write_text(TOKEN)
            os.chmod(tf, 0o600)
            with mock.patch.dict(os.environ, {"COMPASS_WEB_TOKEN_FILE": str(tf)}):
                self.assertTrue(w.Settings("0.0.0.0", 8377).errors)
                self.assertFalse(w.Settings("0.0.0.0", 8377, allow_remote_edit=True).errors)


if __name__ == "__main__":
    unittest.main()
