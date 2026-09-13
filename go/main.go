// claude-compass — a personal, deterministic principle-guard for Claude Code.
//
// Go port of claude_compass.py, same contract: read the hook event JSON on
// stdin, check it against compass.toml (where every rule ships OFF), and
// either block (permissionDecision: "deny"), warn (systemMessage), or stay
// silent. Fail-open: any error → no output, exit 0.
//
// The Python promise "zero dependencies, zero network" translates here to
// "single static binary, no network at runtime" — the one compile-time dep
// (TOML parsing) is linked in.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/BurntSushi/toml"
)

// --------------------------------------------------------------------------
// config
// --------------------------------------------------------------------------

// loadConfig reads compass.toml. Missing/broken config → nil (everything off).
func loadConfig() map[string]any {
	path := os.Getenv("COMPASS_CONFIG")
	if path == "" {
		exe, err := os.Executable()
		if err != nil {
			return nil
		}
		path = filepath.Join(filepath.Dir(exe), "compass.toml")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var cfg map[string]any
	if err := toml.Unmarshal(data, &cfg); err != nil {
		return nil
	}
	return cfg
}

// config helpers: TOML groups decode to map[string]any; defaults mirror the
// Python g.get(key, default) semantics.
func group(cfg map[string]any, name string) map[string]any {
	if g, ok := cfg[name].(map[string]any); ok {
		return g
	}
	return nil
}

func getBool(g map[string]any, key string, def bool) bool {
	if v, ok := g[key].(bool); ok {
		return v
	}
	return def
}

func getInt(g map[string]any, key string, def int) int {
	switch v := g[key].(type) {
	case int64:
		return int(v)
	case int:
		return v
	}
	return def
}

func getStr(g map[string]any, key, def string) string {
	if v, ok := g[key].(string); ok {
		return v
	}
	return def
}

func getStrSlice(g map[string]any, key string) []string {
	raw, ok := g[key].([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// --------------------------------------------------------------------------
// emit helpers (the Claude Code hook output contract)
// --------------------------------------------------------------------------

func emit(obj any) {
	b, err := json.Marshal(obj)
	if err != nil {
		return
	}
	os.Stdout.Write(b)
}

func denyPretool(reason string) {
	emit(map[string]any{
		"hookSpecificOutput": map[string]any{
			"hookEventName":            "PreToolUse",
			"permissionDecision":       "deny",
			"permissionDecisionReason": "compass: " + reason,
		},
	})
}

// warnUser: systemMessage is shown to YOU in the transcript; Claude does not see it.
func warnUser(reason string) {
	emit(map[string]any{"systemMessage": "compass ⚠ " + reason})
}

// blockStop: on Stop, decision:block feeds the reason back and makes Claude
// continue (i.e. revise). Used only when a soft rule's action is "block".
func blockStop(reason string) {
	emit(map[string]any{"decision": "block", "reason": "compass: " + reason})
}

// logFired appends every fired rule to a durable log so warns aren't invisible
// (systemMessage has no guaranteed rendering on a Stop hook). Only called when
// a rule fires; fail-open, errors swallowed.
func logFired(action, reason, on string) {
	defer func() { recover() }()
	path := os.Getenv("COMPASS_LOG")
	if path == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return
		}
		path = filepath.Join(home, ".claude", "compass-warns.log")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	ts := time.Now().Format("2006-01-02T15:04:05")
	fmt.Fprintf(f, "[%s] %-5s on=%s  %s\n", ts, strings.ToUpper(action), on, reason)
}

// act applies a rule's configured action ("block" | "warn").
func act(action, reason, on string) {
	logFired(action, reason, on)
	switch action {
	case "warn":
		warnUser(reason)
	case "block":
		if on == "pretool" {
			denyPretool(reason)
		} else if on == "stop" {
			blockStop(reason)
		}
	}
}

// --------------------------------------------------------------------------
// rule checks (each returns a human reason string on a hit, else "")
// --------------------------------------------------------------------------

var (
	// Mirrors Python's `(?<!git\s)\brm\s+…` — `git rm` (staged, reversible) is
	// exempt. RE2 has no lookbehind, so rmHit re-checks each match's prefix.
	reRM       = regexp.MustCompile(`\brm\s+(?:-\S*[rf]\S*|--recursive|--force)`)
	reGitPre   = regexp.MustCompile(`git\s$`)
	reDisk     = regexp.MustCompile(`\bdd\b[^\n]*\bof=/dev/|\bmkfs\b|>\s*/dev/sd|>\s*/dev/nvme`)
	reCurlSh   = regexp.MustCompile(`\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|d)?sh\b`)
	reChmod777 = regexp.MustCompile(`\bchmod\s+(?:-R\s+)?0?777\b`)
	reForkbomb = regexp.MustCompile(`:\(\)\s*\{\s*:\|:&\s*\}\s*;:`)
)

// rmHit emulates the fixed-width negative lookbehind (?<!git\s): a destructive
// rm counts only when the match is not immediately preceded by `git `.
func rmHit(cmd string) bool {
	for _, loc := range reRM.FindAllStringIndex(cmd, -1) {
		if !reGitPre.MatchString(cmd[:loc[0]]) {
			return true
		}
	}
	return false
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// fnmatchTranslate converts an fnmatch-style glob to a regexp, matching Python
// fnmatch semantics: * matches everything (including /), ? one char, [seq] sets.
func fnmatchTranslate(pat string) *regexp.Regexp {
	var b strings.Builder
	b.WriteString(`^`)
	runes := []rune(pat)
	for i := 0; i < len(runes); i++ {
		c := runes[i]
		switch c {
		case '*':
			b.WriteString(`.*`)
		case '?':
			b.WriteString(`.`)
		case '[':
			j := i + 1
			if j < len(runes) && (runes[j] == '!' || runes[j] == '^') {
				j++
			}
			if j < len(runes) && runes[j] == ']' {
				j++
			}
			for j < len(runes) && runes[j] != ']' {
				j++
			}
			if j >= len(runes) {
				b.WriteString(`\[`)
			} else {
				inner := string(runes[i+1 : j])
				if strings.HasPrefix(inner, "!") {
					inner = "^" + inner[1:]
				}
				b.WriteString("[" + inner + "]")
				i = j
			}
		default:
			b.WriteString(regexp.QuoteMeta(string(c)))
		}
	}
	b.WriteString(`$`)
	re, err := regexp.Compile(b.String())
	if err != nil {
		return nil
	}
	return re
}

func fnmatch(name, pat string) bool {
	re := fnmatchTranslate(pat)
	return re != nil && re.MatchString(name)
}

// checkDangerous: dangerous shell commands + edits to secret files.
func checkDangerous(tool string, tinput map[string]any, g map[string]any) string {
	if tool == "Bash" {
		cmd, _ := tinput["command"].(string)
		if getBool(g, "rm_rf", true) && rmHit(cmd) {
			return "destructive rm blocked → " + truncate(cmd, 120)
		}
		if getBool(g, "disk_destroyers", true) && reDisk.MatchString(cmd) {
			return "disk-destroying command blocked → " + truncate(cmd, 120)
		}
		if getBool(g, "curl_pipe_shell", true) && reCurlSh.MatchString(cmd) {
			return "curl|sh pipe-to-shell blocked → " + truncate(cmd, 120)
		}
		if getBool(g, "chmod_777", true) && reChmod777.MatchString(cmd) {
			return "chmod 777 blocked → " + truncate(cmd, 120)
		}
		if reForkbomb.MatchString(cmd) {
			return "fork bomb blocked"
		}
		for _, pat := range getStrSlice(g, "extra_command_patterns") {
			re, err := regexp.Compile(pat)
			if err != nil {
				continue
			}
			if re.MatchString(cmd) {
				return "matched extra_command_pattern /" + pat + "/ → " + truncate(cmd, 100)
			}
		}
		return ""
	}

	// edits to secret files
	if (tool == "Edit" || tool == "Write" || tool == "MultiEdit") && getBool(g, "secret_file_edits", true) {
		fp, _ := tinput["file_path"].(string)
		if fp == "" {
			fp, _ = tinput["path"].(string)
		}
		if fp != "" {
			name := filepath.Base(fp)
			for _, pat := range getStrSlice(g, "secret_path_globs") {
				if fnmatch(fp, pat) || fnmatch(name, pat) {
					return "edit to secret file blocked → " + fp
				}
			}
		}
	}
	return ""
}

// git's global options that take a SEPARATE value token. These are why substring
// matching failed: `git -C repo push` and `git -c k=v push --force` are ordinary
// git, contain no literal "git push", and walked past the check until 2026-09-13.
var gitGlobalValueOpts = map[string]bool{
	"-C": true, "-c": true, "--git-dir": true, "--work-tree": true,
	"--namespace": true, "--exec-path": true, "--super-prefix": true, "--config-env": true,
}

var shellBins = map[string]bool{"sh": true, "bash": true, "zsh": true, "dash": true, "ksh": true}

// shellSplit: a minimal POSIX-ish tokenizer — enough to read argv out of a
// command line. Handles single/double quotes and backslash escapes; on anything
// it cannot parse it falls back to whitespace splitting rather than failing open.
func shellSplit(s string) []string {
	var out []string
	var cur strings.Builder
	var quote rune
	esc, has := false, false
	for _, r := range s {
		switch {
		case esc:
			cur.WriteRune(r)
			esc, has = false, true
		case r == '\\' && quote != '\'':
			esc = true
		case quote != 0:
			if r == quote {
				quote = 0
			} else {
				cur.WriteRune(r)
			}
			has = true
		case r == '\'' || r == '"':
			quote, has = r, true
		case r == ' ' || r == '\t' || r == '\n' || r == '\r':
			if has {
				out = append(out, cur.String())
				cur.Reset()
				has = false
			}
		default:
			cur.WriteRune(r)
			has = true
		}
	}
	if quote != 0 {
		return strings.Fields(s)
	}
	if has {
		out = append(out, cur.String())
	}
	return out
}

// pushArgv returns the args AFTER the subcommand for every `git push` in the
// command line. Reads argv rather than text, so global options, env prefixes,
// shell chaining and `sh -c "..."` cannot hide the invocation.
func pushArgv(cmd string, depth int) [][]string {
	if depth > 2 {
		return nil
	}
	var segs [][]string
	var cur []string
	for _, t := range shellSplit(cmd) {
		switch t {
		case "&&", "||", ";", "|", "&":
			segs = append(segs, cur)
			cur = nil
		default:
			cur = append(cur, t)
		}
	}
	segs = append(segs, cur)

	var found [][]string
	for _, seg := range segs {
		i := 0
		for i < len(seg) && strings.Contains(seg[i], "=") && !strings.HasPrefix(seg[i], "-") {
			i++ // env assignments: FOO=bar git push ...
		}
		if i >= len(seg) {
			continue
		}
		bin := filepath.Base(seg[i])
		if shellBins[bin] { // `sh -c "git push ..."` hides it all in one token
			for j := i + 1; j < len(seg)-1; j++ {
				if seg[j] == "-c" {
					found = append(found, pushArgv(seg[j+1], depth+1)...)
				}
			}
			continue
		}
		if bin != "git" {
			continue
		}
		i++
		for i < len(seg) { // step over git's global options
			t := seg[i]
			if gitGlobalValueOpts[t] {
				i += 2
			} else if strings.HasPrefix(t, "-") {
				i++
			} else {
				break
			}
		}
		if i < len(seg) && seg[i] == "push" {
			found = append(found, seg[i+1:])
		}
	}
	return found
}

// refNames: branch names a push argument can mean — `main`, `HEAD:main`,
// `refs/heads/main`, `+refs/heads/main:refs/heads/main`.
func refNames(arg string) []string {
	var out []string
	for _, part := range strings.Split(strings.TrimPrefix(arg, "+"), ":") {
		if part == "" {
			continue
		}
		if k := strings.LastIndex(part, "/"); k >= 0 {
			part = part[k+1:]
		}
		out = append(out, part)
	}
	return out
}

// checkGit: git push to a protected branch / force-push. Matches on ARGV, not on
// the command text — the old `strings.Contains(cmd, "git push")` form read as
// enforcing and was not.
func checkGit(cmd string, g map[string]any) string {
	for _, args := range pushArgv(cmd, 0) {
		if getBool(g, "force_push", true) {
			for _, a := range args {
				if a == "-f" || a == "--force" || strings.HasPrefix(a, "--force-with-lease") ||
					(strings.HasPrefix(a, "-") && !strings.HasPrefix(a, "--") && strings.Contains(a[1:], "f")) {
					return "force-push blocked → " + truncate(cmd, 120)
				}
			}
		}
		if getBool(g, "push_to_protected", true) {
			protected := getStrSlice(g, "protected_branches")
			if protected == nil {
				protected = []string{"main", "master"}
			}
			set := make(map[string]bool, len(protected))
			for _, b := range protected {
				set[b] = true
			}
			for _, a := range args {
				if strings.HasPrefix(a, "-") {
					continue
				}
				for _, n := range refNames(a) {
					if set[n] {
						return "push to protected branch blocked → " + truncate(cmd, 120)
					}
				}
			}
		}
	}
	return ""
}

var (
	reSuper = regexp.MustCompile(`(?i)\b(amazing|incredible|fantastic|excellent|perfect|brilliant|wonderful|` +
		`awesome|superb|stellar|exceptional|flawless|phenomenal)\b`)
	reCloser = regexp.MustCompile(`(?i)(happy to help|always here|let me know if you|feel free to|great work|` +
		`you've got this|excited to|i'm here to help)`)
)

var defaultSyc = []string{
	"great question",
	"you're absolutely right",
	"you are absolutely right",
	"i'm thrilled",
	"i am thrilled",
	"happy to help",
	"what a great",
	"excellent question",
	"that's a fantastic",
}

func checkSycophancy(text string, g map[string]any) string {
	low := strings.ToLower(text)
	phrases := getStrSlice(g, "phrases")
	if phrases == nil {
		phrases = defaultSyc
	}
	var found []string
	for _, p := range phrases {
		if strings.Contains(low, strings.ToLower(p)) {
			found = append(found, p)
		}
	}
	if len(found) > 0 {
		if len(found) > 3 {
			found = found[:3]
		}
		return "flattery phrase(s): " + strings.Join(found, ", ")
	}
	if getBool(g, "flag_superlative_pileups", true) {
		n := len(reSuper.FindAllString(text, -1))
		if n >= getInt(g, "superlative_threshold", 3) {
			return fmt.Sprintf("superlative pile-up (%d in one message)", n)
		}
	}
	if getBool(g, "flag_gushing_closers", true) {
		trimmed := strings.TrimSpace(text)
		tail := ""
		if trimmed != "" {
			lines := strings.Split(trimmed, "\n")
			tail = lines[len(lines)-1]
		}
		if reCloser.MatchString(tail) {
			return "gushing closer"
		}
	}
	return ""
}

var defaultExpansion = []string{
	"while i was at it",
	"went ahead and also",
	"took the liberty",
	"as a bonus",
	"i also added",
	"also refactored",
	"additionally, i",
	"i also went ahead",
	"for good measure",
}

// checkScopeDrift is a deterministic *proxy* for unrequested scope expansion —
// NOT true intent drift. Flags language that signals the agent did more than asked.
func checkScopeDrift(text string, g map[string]any) string {
	low := strings.ToLower(text)
	expansion := getStrSlice(g, "expansion_phrases")
	if expansion == nil {
		expansion = defaultExpansion
	}
	var found []string
	for _, p := range expansion {
		if strings.Contains(low, p) {
			found = append(found, p)
		}
	}
	if len(found) > 0 {
		if len(found) > 3 {
			found = found[:3]
		}
		return "unrequested scope-expansion language: " + strings.Join(found, ", ")
	}
	return ""
}

// Self-report: the model flags itself via <<compass:CODE>> markers (see
// CLAUDE.snippet.md). The hook just greps for the token — still 100% local and
// deterministic, but the judgment is the running model's.
var reMarker = regexp.MustCompile(`(?i)<<\s*compass\s*:\s*(\w+)\s*>>`)

var markerMeaning = map[string]string{
	"drift":    "self-flagged: going beyond / away from what was asked",
	"scope":    "self-flagged: adding unrequested scope",
	"unsure":   "self-flagged: guessing / low confidence / unverified",
	"assume":   "self-flagged: proceeding on an unconfirmed assumption",
	"flattery": "self-flagged: being sycophantic",
	"risk":     "self-flagged: risky / hard-to-reverse action",
}

var defaultMarkers = []string{"drift", "scope", "unsure", "assume", "flattery", "risk"}

// checkSelfReport returns (reason, escalateToBlock) for enabled self-report markers.
func checkSelfReport(text string, g map[string]any) (string, bool) {
	markers := getStrSlice(g, "markers")
	if markers == nil {
		markers = defaultMarkers
	}
	enabled := make(map[string]bool, len(markers))
	for _, m := range markers {
		enabled[strings.ToLower(m)] = true
	}
	blockMarkers := make(map[string]bool)
	for _, m := range getStrSlice(g, "block_markers") {
		blockMarkers[strings.ToLower(m)] = true
	}
	var hits []string
	escalate := false
	for _, m := range reMarker.FindAllStringSubmatch(text, -1) {
		code := strings.ToLower(m[1])
		if !enabled[code] {
			continue
		}
		meaning, ok := markerMeaning[code]
		if !ok {
			meaning = "self-flagged: " + code
		}
		hits = append(hits, meaning)
		if blockMarkers[code] {
			escalate = true
		}
	}
	if len(hits) == 0 {
		return "", false
	}
	seen := map[string]bool{}
	var uniq []string
	for _, h := range hits {
		if !seen[h] {
			seen[h] = true
			uniq = append(uniq, h)
		}
	}
	if len(uniq) > 4 {
		uniq = uniq[:4]
	}
	return strings.Join(uniq, " | "), escalate
}

// --------------------------------------------------------------------------
// custom rules ([[custom_rules]] in compass.toml)
// --------------------------------------------------------------------------

// customRules returns enabled custom rules for one surface ("pretool"|"stop").
// A rule with no `enabled` key counts as on — writing the rule is opting in.
// BurntSushi decodes [[x]] as []map[string]any, but tolerate []any too.
func customRules(cfg map[string]any, on string) []map[string]any {
	var raw []map[string]any
	switch v := cfg["custom_rules"].(type) {
	case []map[string]any:
		raw = v
	case []any:
		for _, item := range v {
			if m, ok := item.(map[string]any); ok {
				raw = append(raw, m)
			}
		}
	}
	var out []map[string]any
	for _, r := range raw {
		if getBool(r, "enabled", true) && getStr(r, "on", "pretool") == on {
			out = append(out, r)
		}
	}
	return out
}

// checkCustom returns the reason string when the rule's pattern matches text,
// else "". Invalid or missing patterns never fire (fail-open).
func checkCustom(rule map[string]any, text string) string {
	pat := getStr(rule, "pattern", "")
	if pat == "" {
		return ""
	}
	re, err := regexp.Compile(pat)
	if err != nil || !re.MatchString(text) {
		return ""
	}
	// mirror Python's `rule.get("name") or pat`: empty string falls back too
	name := getStr(rule, "name", "")
	if name == "" {
		name = pat
	}
	reason := getStr(rule, "reason", "")
	if reason == "" {
		reason = "matched /" + pat + "/"
	}
	return "custom '" + name + "': " + reason
}

// --------------------------------------------------------------------------
// transcript reading (for Stop)
// --------------------------------------------------------------------------

func blockText(content any) string {
	switch c := content.(type) {
	case string:
		return c
	case []any:
		var parts []string
		for _, b := range c {
			switch bb := b.(type) {
			case map[string]any:
				if bb["type"] == "text" {
					if t, ok := bb["text"].(string); ok {
						parts = append(parts, t)
					}
				}
			case string:
				parts = append(parts, bb)
			}
		}
		return strings.Join(parts, "\n")
	}
	return ""
}

// scanTranscript walks the JSONL transcript calling fn(role, text) per message.
func scanTranscript(path string, fn func(role, text string)) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var obj map[string]any
		if err := json.Unmarshal([]byte(line), &obj); err != nil {
			continue
		}
		msg, ok := obj["message"].(map[string]any)
		if !ok {
			msg = obj
		}
		role, _ := msg["role"].(string)
		if role == "" {
			role, _ = obj["type"].(string)
		}
		fn(role, blockText(msg["content"]))
	}
	return true
}

func lastAssistantText(path string) string {
	if path == "" {
		return ""
	}
	last := ""
	scanTranscript(path, func(role, text string) {
		if role == "assistant" && strings.TrimSpace(text) != "" {
			last = text
		}
	})
	return last
}

// assistantTextSinceLastUser: all assistant text in the final turn (reset on
// each user message). Self-report markers may be emitted mid-turn.
func assistantTextSinceLastUser(path string) string {
	if path == "" {
		return ""
	}
	var buf []string
	scanTranscript(path, func(role, text string) {
		if role == "user" {
			buf = nil // new turn
		} else if role == "assistant" && strings.TrimSpace(text) != "" {
			buf = append(buf, text)
		}
	})
	return strings.Join(buf, "\n")
}

// finalTurnPresent is true once the just-finished assistant turn has been
// flushed to the transcript, i.e. the last text-bearing message is assistant.
// Claude Code fires the Stop hook slightly before the final turn is written,
// so a single read sees a one-message-stale file.
func finalTurnPresent(path string) bool {
	last := ""
	if !scanTranscript(path, func(role, text string) {
		if role == "user" {
			last = "user"
		} else if role == "assistant" && strings.TrimSpace(text) != "" {
			last = "assistant"
		}
	}) {
		return false
	}
	return last == "assistant"
}

// awaitFinalTurn gives the transcript writer a moment to flush the turn that
// triggered Stop. Fail-open: on timeout we scan whatever is there.
func awaitFinalTurn(path string) {
	if path == "" {
		return
	}
	for i := 0; i < 6; i++ {
		if finalTurnPresent(path) {
			return
		}
		time.Sleep(30 * time.Millisecond)
	}
}

// --------------------------------------------------------------------------
// event handlers
// --------------------------------------------------------------------------

func handlePretool(ev, cfg map[string]any) {
	tool, _ := ev["tool_name"].(string)
	tinput, _ := ev["tool_input"].(map[string]any)

	if g := group(cfg, "dangerous_tools"); getBool(g, "enabled", false) {
		if hit := checkDangerous(tool, tinput, g); hit != "" {
			act(getStr(g, "action", "block"), hit, "pretool")
			return
		}
	}
	if g := group(cfg, "git_safety"); getBool(g, "enabled", false) && tool == "Bash" {
		cmd, _ := tinput["command"].(string)
		if hit := checkGit(cmd, g); hit != "" {
			act(getStr(g, "action", "block"), hit, "pretool")
			return
		}
	}

	// Custom pretool rules match against Bash commands only (v1).
	if tool == "Bash" {
		cmd, _ := tinput["command"].(string)
		for _, rule := range customRules(cfg, "pretool") {
			if hit := checkCustom(rule, cmd); hit != "" {
				act(getStr(rule, "action", "warn"), hit+" → "+truncate(cmd, 100), "pretool")
				return
			}
		}
	}
}

func handleStop(ev, cfg map[string]any) {
	syc := group(cfg, "sycophancy")
	drift := group(cfg, "scope_drift")
	selfrep := group(cfg, "self_report")
	customStop := customRules(cfg, "stop")
	if !(getBool(syc, "enabled", false) || getBool(drift, "enabled", false) ||
		getBool(selfrep, "enabled", false) || len(customStop) > 0) {
		return
	}
	path, _ := ev["transcript_path"].(string)

	// The Stop hook fires before the final turn is flushed; wait for it to land.
	awaitFinalTurn(path)

	// Self-report scans the whole final turn (markers may be emitted mid-turn).
	if getBool(selfrep, "enabled", false) {
		if turn := assistantTextSinceLastUser(path); turn != "" {
			if reason, escalate := checkSelfReport(turn, selfrep); reason != "" {
				action := getStr(selfrep, "action", "warn")
				if escalate {
					action = "block"
				}
				act(action, reason, "stop")
				return
			}
		}
	}

	// Pattern checks look at the final reply.
	text := lastAssistantText(path)
	if text == "" {
		return
	}
	if getBool(syc, "enabled", false) {
		if hit := checkSycophancy(text, syc); hit != "" {
			act(getStr(syc, "action", "warn"), hit, "stop")
			return
		}
	}
	if getBool(drift, "enabled", false) {
		if hit := checkScopeDrift(text, drift); hit != "" {
			act(getStr(drift, "action", "warn"), hit, "stop")
			return
		}
	}
	for _, rule := range customStop {
		if hit := checkCustom(rule, text); hit != "" {
			act(getStr(rule, "action", "warn"), hit, "stop")
			return
		}
	}
}

func main() {
	// fail-open: never break the session, always exit 0
	defer func() { recover() }()

	raw, err := readStdin()
	if err != nil {
		return
	}
	var ev map[string]any
	if strings.TrimSpace(raw) == "" || json.Unmarshal([]byte(raw), &ev) != nil {
		return
	}
	cfg := loadConfig()
	if len(cfg) == 0 || ev == nil {
		return
	}
	switch ev["hook_event_name"] {
	case "PreToolUse":
		handlePretool(ev, cfg)
	case "Stop", "SubagentStop":
		handleStop(ev, cfg)
	}
}

func readStdin() (string, error) {
	st, err := os.Stdin.Stat()
	if err == nil && st.Mode()&os.ModeCharDevice != 0 {
		return "", nil // interactive terminal, nothing piped
	}
	b, err := io.ReadAll(os.Stdin)
	if err != nil {
		return "", err
	}
	return string(b), nil
}
