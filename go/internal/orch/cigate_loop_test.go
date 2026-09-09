package orch

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
)

// --- scripted watcher/fixer harness --------------------------------------

type ciCall struct {
	target string
	input  map[string]any
}

// scriptCIApp returns a mockApp that pops pre-baked run_ci_watcher /
// run_ci_fixer results (wrapped in a succeeded envelope so Deps.Call unwraps
// them) and records every call for assertions.
func scriptCIApp(watchQ, fixQ []map[string]any) (*mockApp, *[]ciCall) {
	calls := &[]ciCall{}
	wi, fi := 0, 0
	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		*calls = append(*calls, ciCall{target: target, input: input})
		var out map[string]any
		switch {
		case strings.HasSuffix(target, "run_ci_watcher"):
			out = watchQ[wi]
			wi++
		case strings.HasSuffix(target, "run_ci_fixer"):
			out = fixQ[fi]
			fi++
		}
		return map[string]any{"status": "succeeded", "result": out}, nil
	}}
	return app, calls
}

func ciCfg() *config.BuildConfig {
	return &config.BuildConfig{
		Runtime:               "claude_code",
		PermissionMode:        "",
		MaxCIFixCycles:        2,
		CIWaitSeconds:         1500,
		CIPollSeconds:         30,
		CIStartupGraceSeconds: 30,
	}
}

func ciReq(app *mockApp, resolved map[string]string) CIGateRequest {
	return CIGateRequest{
		Deps:              &Deps{App: app, NodeID: "swe-planner"},
		Cfg:               ciCfg(),
		Resolved:          resolved,
		RepoPath:          "/tmp/repo",
		PRNumber:          42,
		PRURL:             "https://github.com/o/r/pull/42",
		IntegrationBranch: "integration/x",
		BaseBranch:        "main",
		Goal:              "do the thing",
		CompletedIssues:   []map[string]any{{"name": "i1"}},
	}
}

func countCalls(calls []ciCall, suffix string) int {
	n := 0
	for _, c := range calls {
		if strings.HasSuffix(c.target, suffix) {
			n++
		}
	}
	return n
}

func watcherCalls(calls []ciCall) []ciCall {
	var out []ciCall
	for _, c := range calls {
		if strings.HasSuffix(c.target, "run_ci_watcher") {
			out = append(out, c)
		}
	}
	return out
}

func fixerCalls(calls []ciCall) []ciCall {
	var out []ciCall
	for _, c := range calls {
		if strings.HasSuffix(c.target, "run_ci_fixer") {
			out = append(out, c)
		}
	}
	return out
}

// Contract: "passed" short-circuits — no fixer, final_status=passed, no attempts.
func TestCIGatePassedShortCircuits(t *testing.T) {
	app, calls := scriptCIApp(
		[]map[string]any{{"status": "passed"}},
		nil,
	)
	req := ciReq(app, map[string]string{"ci_fixer_model": "opus"})
	req.HeadSHA = "" // build path: no grace
	out, err := RunCIGate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if out["final_status"] != "passed" {
		t.Fatalf("final_status=%v want passed", out["final_status"])
	}
	if n := len(out["fix_attempts"].([]map[string]any)); n != 0 {
		t.Fatalf("fix_attempts=%d want 0", n)
	}
	if countCalls(*calls, "run_ci_fixer") != 0 {
		t.Fatal("fixer must not run when checks pass")
	}
	if countCalls(*calls, "run_ci_watcher") != 1 {
		t.Fatal("expected exactly one watch")
	}
}

// Contract: "no_checks" short-circuits with final_status=no_checks.
func TestCIGateNoChecksShortCircuits(t *testing.T) {
	app, calls := scriptCIApp([]map[string]any{{"status": "no_checks"}}, nil)
	out, err := RunCIGate(context.Background(), ciReqNoGrace(app))
	if err != nil {
		t.Fatal(err)
	}
	if out["final_status"] != "no_checks" {
		t.Fatalf("final_status=%v want no_checks", out["final_status"])
	}
	if countCalls(*calls, "run_ci_fixer") != 0 {
		t.Fatal("fixer must not run for no_checks")
	}
}

// Contract: timed_out / error pass through unchanged, no fixer.
func TestCIGateWatcherTimedOutAndErrorPassThrough(t *testing.T) {
	for _, status := range []string{"timed_out", "error"} {
		app, calls := scriptCIApp([]map[string]any{{"status": status, "summary": "boom"}}, nil)
		out, err := RunCIGate(context.Background(), ciReqNoGrace(app))
		if err != nil {
			t.Fatal(err)
		}
		if out["final_status"] != status {
			t.Fatalf("final_status=%v want %s", out["final_status"], status)
		}
		if countCalls(*calls, "run_ci_fixer") != 0 {
			t.Fatalf("%s: fixer must not run", status)
		}
	}
}

// Contract (Python parity, app.py:266): failed -> fixer pushes -> rewatch
// passes; every watch keeps the ORIGINAL head_sha anchor. Python passes
// head_sha=head_sha on each cycle and never re-anchors to the fixer's commit.
func TestCIGateFailedFixRewatchPassesAndReanchors(t *testing.T) {
	app, calls := scriptCIApp(
		[]map[string]any{
			{"status": "failed", "failed_checks": []any{map[string]any{"name": "Tests"}}},
			{"status": "passed"},
		},
		[]map[string]any{
			{"pushed": true, "commit_sha": "newsha222", "summary": "fixed it"},
		},
	)
	req := ciReq(app, map[string]string{"ci_fixer_model": "opus"})
	req.HeadSHA = "origsha111"
	out, err := RunCIGate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if out["final_status"] != "passed" {
		t.Fatalf("final_status=%v want passed", out["final_status"])
	}
	attempts := out["fix_attempts"].([]map[string]any)
	if len(attempts) != 1 {
		t.Fatalf("fix_attempts=%d want 1", len(attempts))
	}
	wc := watcherCalls(*calls)
	if len(wc) != 2 {
		t.Fatalf("watch calls=%d want 2", len(wc))
	}
	if got := wc[0].input["head_sha"]; got != "origsha111" {
		t.Fatalf("first watch head_sha=%v want origsha111", got)
	}
	if got := wc[1].input["head_sha"]; got != "origsha111" {
		t.Fatalf("second watch head_sha=%v want original anchor origsha111 (no re-anchoring, Python parity)", got)
	}
}

// Contract: fixer returning pushed=false -> fixer_gave_up (fixed=false too).
func TestCIGateFixerGaveUp(t *testing.T) {
	app, _ := scriptCIApp(
		[]map[string]any{{"status": "failed", "failed_checks": []any{}}},
		[]map[string]any{{"fixed": false, "pushed": false, "summary": "cannot fix"}},
	)
	out, err := RunCIGate(context.Background(), ciReqNoGrace(app))
	if err != nil {
		t.Fatal(err)
	}
	if out["final_status"] != "fixer_gave_up" {
		t.Fatalf("final_status=%v want fixer_gave_up", out["final_status"])
	}
	if len(out["fix_attempts"].([]map[string]any)) != 1 {
		t.Fatal("expected exactly one recorded attempt")
	}
}

// Contract: persistent failure bounded by max_ci_fix_cycles -> failed_exhausted,
// with exactly max_ci_fix_cycles fixer invocations.
func TestCIGateFailedExhausted(t *testing.T) {
	app, calls := scriptCIApp(
		[]map[string]any{
			{"status": "failed", "failed_checks": []any{}},
			{"status": "failed", "failed_checks": []any{}},
			{"status": "failed", "failed_checks": []any{}},
		},
		[]map[string]any{
			{"pushed": true, "commit_sha": "s1"},
			{"pushed": true, "commit_sha": "s2"},
		},
	)
	out, err := RunCIGate(context.Background(), ciReqNoGrace(app))
	if err != nil {
		t.Fatal(err)
	}
	if out["final_status"] != "failed_exhausted" {
		t.Fatalf("final_status=%v want failed_exhausted", out["final_status"])
	}
	if got := countCalls(*calls, "run_ci_fixer"); got != 2 {
		t.Fatalf("fixer invocations=%d want 2 (max_ci_fix_cycles)", got)
	}
	if got := countCalls(*calls, "run_ci_watcher"); got != 3 {
		t.Fatalf("watch invocations=%d want 3 (max+1)", got)
	}
	if n := len(out["fix_attempts"].([]map[string]any)); n != 2 {
		t.Fatalf("fix_attempts=%d want 2", n)
	}
}

// Contract: watcher kwargs are passed verbatim.
func TestCIGateWatcherKwargsVerbatim(t *testing.T) {
	app, calls := scriptCIApp([]map[string]any{{"status": "passed"}}, nil)
	_, err := RunCIGate(context.Background(), ciReqNoGrace(app))
	if err != nil {
		t.Fatal(err)
	}
	in := watcherCalls(*calls)[0].input
	if in["repo_path"] != "/tmp/repo" || asInt(in["pr_number"]) != 42 {
		t.Fatalf("watcher repo/pr wrong: %v", in)
	}
	if asInt(in["wait_seconds"]) != 1500 || asInt(in["poll_seconds"]) != 30 {
		t.Fatalf("watcher wait/poll wrong: %v", in)
	}
	if _, ok := in["head_sha"]; !ok {
		t.Fatal("watcher must receive head_sha")
	}
}

// Contract (app.py:326): model = resolved["ci_fixer_model"] when present, else
// resolved["coder_model"].
func TestCIGateFixerModelResolution(t *testing.T) {
	// ci_fixer_model present -> used.
	app, calls := scriptCIApp(
		[]map[string]any{{"status": "failed", "failed_checks": []any{}}},
		[]map[string]any{{"pushed": false, "summary": "x"}},
	)
	req := ciReq(app, map[string]string{"ci_fixer_model": "opus", "coder_model": "sonnet"})
	req.HeadSHA = ""
	req.Cfg.CIStartupGraceSeconds = 0
	if _, err := RunCIGate(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	if got := fixerCalls(*calls)[0].input["model"]; got != "opus" {
		t.Fatalf("fixer model=%v want opus (ci_fixer_model)", got)
	}

	// ci_fixer_model absent -> falls back to coder_model.
	app2, calls2 := scriptCIApp(
		[]map[string]any{{"status": "failed", "failed_checks": []any{}}},
		[]map[string]any{{"pushed": false, "summary": "x"}},
	)
	req2 := ciReq(app2, map[string]string{"coder_model": "sonnet"})
	req2.HeadSHA = ""
	req2.Cfg.CIStartupGraceSeconds = 0
	if _, err := RunCIGate(context.Background(), req2); err != nil {
		t.Fatal(err)
	}
	if got := fixerCalls(*calls2)[0].input["model"]; got != "sonnet" {
		t.Fatalf("fixer model=%v want sonnet (coder_model fallback)", got)
	}
}

// Contract (Python parity, app.py:1887 vs :224-352): the startup-grace sleep
// belongs to resolve()'s call site, NOT to _run_ci_gate. RunCIGate must never
// sleep the grace itself, regardless of HeadSHA or configuration.
func TestCIGateStartupGrace(t *testing.T) {
	prev := sleepFn
	defer func() { sleepFn = prev }()
	var sleepCalls int
	sleepFn = func(_ context.Context, _ time.Duration) { sleepCalls++ }

	for _, headSHA := range []string{"sha1", ""} {
		sleepCalls = 0
		app, _ := scriptCIApp([]map[string]any{{"status": "passed"}}, nil)
		req := ciReq(app, nil)
		req.HeadSHA = headSHA
		req.Cfg.CIStartupGraceSeconds = 30
		if _, err := RunCIGate(context.Background(), req); err != nil {
			t.Fatal(err)
		}
		if sleepCalls != 0 {
			t.Fatalf("RunCIGate must not sleep the grace (headSHA=%q), got %d sleeps", headSHA, sleepCalls)
		}
	}
}

// ciReqNoGrace builds a request with no head SHA and grace disabled, isolating
// the loop behavior from the startup-grace sleep.
func ciReqNoGrace(app *mockApp) CIGateRequest {
	req := ciReq(app, map[string]string{"ci_fixer_model": "opus", "coder_model": "sonnet"})
	req.HeadSHA = ""
	req.Cfg.CIStartupGraceSeconds = 0
	return req
}
