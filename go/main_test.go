package main

import "testing"

// The conformance suite (tests/conformance/) covers the full hook contract
// end-to-end; these native tests pin the one piece hand-rolled for the port —
// Python-fnmatch-equivalent glob matching — plus a sample of each matcher.

func TestFnmatchPythonSemantics(t *testing.T) {
	cases := []struct {
		name, pat string
		want      bool
	}{
		{"deploy/secrets/db.yaml", "**/secrets/**", true}, // * crosses / like fnmatch
		{".env", "*.env", true},
		{"/app/.env", "*.env", true}, // full path: * matches leading dirs
		{"main.py", "*.env", false},
		{"id_rsa.pub", "id_rsa*", true},
		{"config.yaml", "config.y?ml", true},
		{"config.toml", "config.[ty]?ml", true},
		{"broken[", "broken[", true}, // unterminated set matches literally
	}
	for _, c := range cases {
		if got := fnmatch(c.name, c.pat); got != c.want {
			t.Errorf("fnmatch(%q, %q) = %v, want %v", c.name, c.pat, got, c.want)
		}
	}
}

func TestCheckGit(t *testing.T) {
	g := map[string]any{"enabled": true}
	if checkGit("git push origin main", g) == "" {
		t.Error("push to main should be flagged")
	}
	if checkGit("git push -f origin x", g) == "" {
		t.Error("force push should be flagged")
	}
	if checkGit("git push origin my-feature", g) != "" {
		t.Error("feature branch push should pass")
	}
	if checkGit("git status", g) != "" {
		t.Error("non-push should be ignored")
	}
}

func TestCheckSycophancy(t *testing.T) {
	g := map[string]any{"enabled": true}
	if checkSycophancy("You're absolutely right, my mistake.", g) == "" {
		t.Error("flattery phrase should be flagged")
	}
	if checkSycophancy("The bug was a missing semicolon on line 4.", g) != "" {
		t.Error("plain answer should pass")
	}
}

func TestCheckSelfReport(t *testing.T) {
	g := map[string]any{"block_markers": []any{"risk"}}
	reason, escalate := checkSelfReport("about to <<compass:risk>>", g)
	if reason == "" || !escalate {
		t.Errorf("risk marker should flag and escalate, got (%q, %v)", reason, escalate)
	}
	reason, escalate = checkSelfReport("a perfectly normal answer", g)
	if reason != "" || escalate {
		t.Error("clean text should not flag")
	}
}
