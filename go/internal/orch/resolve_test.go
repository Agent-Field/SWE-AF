package orch

import (
	"context"
	"strings"
	"testing"
	"time"
)

// --- seam helpers ---------------------------------------------------------

// gitCall / ghCall record a single subprocess invocation for assertions.
type recCall struct {
	dir  string
	args []string
}

// installGitGH overrides the runGit/runGH seams with routers and returns the
// captured-call slices plus a restore func. gitFn/ghFn map args→cmdResult.
func installGitGH(
	gitFn func(dir string, args []string) cmdResult,
	ghFn func(dir string, args []string) cmdResult,
) (git *[]recCall, gh *[]recCall, restore func()) {
	prevGit, prevGH := runGit, runGH
	gitCalls := []recCall{}
	ghCalls := []recCall{}
	runGit = func(_ context.Context, dir string, args ...string) cmdResult {
		gitCalls = append(gitCalls, recCall{dir: dir, args: args})
		return gitFn(dir, args)
	}
	runGH = func(_ context.Context, dir string, args ...string) cmdResult {
		ghCalls = append(ghCalls, recCall{dir: dir, args: args})
		return ghFn(dir, args)
	}
	return &gitCalls, &ghCalls, func() { runGit, runGH = prevGit, prevGH }
}

// installSleep captures sleepFn durations (no real sleeping).
func installSleep() (slept *[]time.Duration, restore func()) {
	prev := sleepFn
	got := []time.Duration{}
	sleepFn = func(_ context.Context, d time.Duration) { got = append(got, d) }
	return &got, func() { sleepFn = prev }
}

// ---------------------------------------------------------------------------
// attemptBaseMerge — merge-state classification (maps to TestAttemptBaseMerge).
// ---------------------------------------------------------------------------

func TestAttemptBaseMergeSkippedWhenFetchFails(t *testing.T) {
	git, _, restore := installGitGH(
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 1, Stderr: "no remote"} },
		func(_ string, _ []string) cmdResult { return cmdResult{} },
	)
	defer restore()

	state, conflicts := attemptBaseMerge(context.Background(), "/tmp/x", "main")
	if state != "skipped" {
		t.Fatalf("state = %q, want skipped", state)
	}
	if len(conflicts) != 0 {
		t.Fatalf("conflicts = %v, want empty", conflicts)
	}
	// Only the fetch call was made — no merge attempt after a failed fetch.
	if len(*git) != 1 {
		t.Fatalf("expected 1 git call (fetch), got %d: %v", len(*git), *git)
	}
}

func TestAttemptBaseMergeCleanWhenAncestor(t *testing.T) {
	_, _, restore := installGitGH(
		func(_ string, args []string) cmdResult {
			switch args[0] {
			case "fetch":
				return cmdResult{ExitCode: 0}
			case "merge-base":
				return cmdResult{ExitCode: 0} // 0 == is-ancestor
			}
			return cmdResult{ExitCode: 0}
		},
		func(_ string, _ []string) cmdResult { return cmdResult{} },
	)
	defer restore()

	state, conflicts := attemptBaseMerge(context.Background(), "/tmp/x", "main")
	if state != "clean" || len(conflicts) != 0 {
		t.Fatalf("got (%q,%v), want (clean,[])", state, conflicts)
	}
}

func TestAttemptBaseMergeMergedWhenMergeSucceeds(t *testing.T) {
	_, _, restore := installGitGH(
		func(_ string, args []string) cmdResult {
			switch args[0] {
			case "fetch":
				return cmdResult{ExitCode: 0}
			case "merge-base":
				return cmdResult{ExitCode: 1} // 1 == not-ancestor
			case "merge":
				return cmdResult{ExitCode: 0}
			}
			return cmdResult{ExitCode: 0}
		},
		func(_ string, _ []string) cmdResult { return cmdResult{} },
	)
	defer restore()

	state, conflicts := attemptBaseMerge(context.Background(), "/tmp/x", "main")
	if state != "merged" || len(conflicts) != 0 {
		t.Fatalf("got (%q,%v), want (merged,[])", state, conflicts)
	}
}

func TestAttemptBaseMergeConflictListsUnmergedFiles(t *testing.T) {
	_, _, restore := installGitGH(
		func(_ string, args []string) cmdResult {
			switch args[0] {
			case "fetch":
				return cmdResult{ExitCode: 0}
			case "merge-base":
				return cmdResult{ExitCode: 1}
			case "merge":
				return cmdResult{ExitCode: 1, Stderr: "CONFLICT"}
			case "diff":
				return cmdResult{ExitCode: 0, Stdout: "src/a.py\nsrc/b.py\n\n"}
			}
			return cmdResult{ExitCode: 0}
		},
		func(_ string, _ []string) cmdResult { return cmdResult{} },
	)
	defer restore()

	state, conflicts := attemptBaseMerge(context.Background(), "/tmp/x", "main")
	if state != "conflict" {
		t.Fatalf("state = %q, want conflict", state)
	}
	if len(conflicts) != 2 || conflicts[0] != "src/a.py" || conflicts[1] != "src/b.py" {
		t.Fatalf("conflicts = %v, want [src/a.py src/b.py]", conflicts)
	}
}

// ---------------------------------------------------------------------------
// resolve() input validation (maps to TestResolveInputValidation).
// ---------------------------------------------------------------------------

func TestResolveMissingRequiredArgsRaises(t *testing.T) {
	deps := &Deps{App: &mockApp{handler: func(context.Context, string, map[string]any) (map[string]any, error) {
		return map[string]any{}, nil
	}}, NodeID: "swe-planner"}

	cases := []map[string]any{
		{"pr_url": "", "pr_number": 1, "repo_url": "https://github.com/o/r.git", "head_branch": "feature/x"},
		{"pr_url": "https://github.com/o/r/pull/1", "pr_number": 0, "repo_url": "https://github.com/o/r.git", "head_branch": "feature/x"},
		{"pr_url": "https://github.com/o/r/pull/1", "pr_number": 1, "repo_url": "https://github.com/o/r.git", "head_branch": ""},
		{"pr_url": "https://github.com/o/r/pull/1", "pr_number": 1, "repo_url": "", "head_branch": "feature/x"},
	}
	for i, in := range cases {
		_, err := ResolveHandler(context.Background(), deps, in)
		if err == nil || !strings.Contains(err.Error(), "non-empty pr_url") {
			t.Fatalf("case %d: expected non-empty pr_url error, got %v", i, err)
		}
	}
}

// ---------------------------------------------------------------------------
// resolve() orchestration shape (maps to TestResolveOrchestration).
// ---------------------------------------------------------------------------

func resolverHappyPayload() map[string]any {
	return map[string]any{
		"fixed":          true,
		"merge_resolved": true,
		"files_changed":  []any{"src/a.py"},
		"commit_shas":    []any{"abc123"},
		"pushed":         true,
		"addressed_comments": []any{
			map[string]any{"comment_id": float64(11), "thread_id": "PRRT_aaa", "addressed": true, "note": "Renamed foo to bar"},
			map[string]any{"comment_id": float64(22), "thread_id": "PRRT_bbb", "addressed": false, "note": "Not actionable"},
		},
		"summary": "resolver summary",
	}
}

func TestResolveCallsResolverAndPostsThreads(t *testing.T) {
	defer withExecCtx("run-r", "exec-r")()

	git, gh, restore := installGitGH(
		func(_ string, args []string) cmdResult {
			switch args[0] {
			case "fetch":
				return cmdResult{ExitCode: 0}
			case "merge-base":
				return cmdResult{ExitCode: 1} // not ancestor → proceed to merge
			case "merge":
				return cmdResult{ExitCode: 0} // merged
			case "rev-parse":
				return cmdResult{ExitCode: 0, Stdout: "newsha-abc\n"}
			}
			return cmdResult{ExitCode: 0}
		},
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
	)
	defer restore()

	slept, restoreSleep := installSleep()
	defer restoreSleep()

	// Capture all reasoner calls.
	captured := []struct {
		target string
		input  map[string]any
	}{}
	app := &mockApp{handler: func(_ context.Context, target string, input map[string]any) (map[string]any, error) {
		captured = append(captured, struct {
			target string
			input  map[string]any
		}{target, input})
		if strings.Contains(target, "run_pr_resolver") {
			return resolverHappyPayload(), nil
		}
		return map[string]any{}, nil
	}}

	// CI gate seam captures its request.
	var gateReq CIGateRequest
	gateCalled := false
	deps := &Deps{
		App:    app,
		NodeID: "swe-planner",
		CIGate: func(_ context.Context, req CIGateRequest) (map[string]any, error) {
			gateCalled = true
			gateReq = req
			return map[string]any{"final_status": "passed", "fix_attempts": []any{}, "watch": map[string]any{}}, nil
		},
	}

	out, err := ResolveHandler(context.Background(), deps, map[string]any{
		"pr_url":      "https://github.com/o/r/pull/7",
		"pr_number":   7,
		"repo_url":    "https://github.com/o/r.git",
		"head_branch": "feature/x",
		"base_branch": "main",
		"ci_failures": []any{map[string]any{"name": "tests", "logs_excerpt": "AssertionError"}},
		"review_comments": []any{
			map[string]any{"comment_id": 11, "thread_id": "PRRT_aaa", "path": "src/a.py", "line": 5, "author": "alice", "body": "please rename this"},
			map[string]any{"comment_id": 22, "thread_id": "PRRT_bbb", "path": "src/b.py", "line": 10, "author": "bob", "body": "what about this?"},
		},
	})
	if err != nil {
		t.Fatalf("resolve errored: %v", err)
	}
	res := out.(map[string]any)

	// --- resolver called once with the right kwargs ---
	resolverCalls := 0
	var kw map[string]any
	for _, c := range captured {
		if strings.Contains(c.target, "run_pr_resolver") {
			resolverCalls++
			kw = c.input
		}
	}
	if resolverCalls != 1 {
		t.Fatalf("expected 1 run_pr_resolver call, got %d", resolverCalls)
	}
	if asInt(kw["pr_number"]) != 7 || mapStr(kw, "head_branch", "") != "feature/x" ||
		mapStr(kw, "base_branch", "") != "main" || mapStr(kw, "merge_state", "") != "merged" {
		t.Fatalf("resolver kwargs mismatch: %v", kw)
	}
	if cf, ok := kw["conflicted_files"].([]string); !ok || len(cf) != 0 {
		t.Fatalf("conflicted_files = %v, want empty []string", kw["conflicted_files"])
	}
	if len(maps0(kw["failed_checks"])) != 1 {
		t.Fatalf("failed_checks len = %d, want 1", len(maps0(kw["failed_checks"])))
	}
	if len(maps0(kw["review_comments"])) != 2 {
		t.Fatalf("review_comments len = %d, want 2", len(maps0(kw["review_comments"])))
	}

	// --- PR creation must NOT be triggered ---
	for _, c := range captured {
		if strings.Contains(c.target, "run_github_pr") {
			t.Fatalf("resolve must never create a PR; saw %s", c.target)
		}
	}
	for _, g := range *git {
		if len(g.args) >= 2 && g.args[0] == "pr" && g.args[1] == "create" {
			t.Fatalf("unexpected git pr create: %v", g.args)
		}
	}

	// --- CI gate ran with PR number + head as integration + SHA anchor ---
	if !gateCalled {
		t.Fatal("CI gate should have run")
	}
	if gateReq.PRNumber != 7 || gateReq.IntegrationBranch != "feature/x" ||
		gateReq.BaseBranch != "main" || gateReq.HeadSHA != "newsha-abc" {
		t.Fatalf("CI gate request mismatch: %+v", gateReq)
	}

	// --- startup grace fired before the gate (>=30s) ---
	found := false
	for _, d := range *slept {
		if d >= 30*time.Second {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected a >=30s grace sleep; got %v", *slept)
	}

	// --- thread replies posted only for addressed=true ---
	replies := asMapList(res["thread_replies"])
	if len(replies) != 1 {
		t.Fatalf("expected 1 thread reply, got %d", len(replies))
	}
	r0 := replies[0]
	if asInt(r0["comment_id"]) != 11 || mapStr(r0, "thread_id", "") != "PRRT_aaa" ||
		!asBool(r0["replied"]) || !asBool(r0["resolved"]) {
		t.Fatalf("thread reply mismatch: %v", r0)
	}

	// --- gh saw the inline reply POST + GraphQL mutation (skip not-addressed) ---
	if len(*gh) != 2 {
		t.Fatalf("expected 2 gh api calls, got %d: %v", len(*gh), *gh)
	}
	var replyCmd, graphqlCmd []string
	for _, g := range *gh {
		joined := strings.Join(g.args, " ")
		if strings.Contains(joined, "replies") {
			replyCmd = g.args
		}
		if strings.Contains(joined, "graphql") {
			graphqlCmd = g.args
		}
	}
	if replyCmd == nil || !strings.Contains(strings.Join(replyCmd, " "), "/comments/11/replies") {
		t.Fatalf("reply command missing /comments/11/replies: %v", replyCmd)
	}
	if graphqlCmd == nil || !containsArg(graphqlCmd, "id=PRRT_aaa") {
		t.Fatalf("graphql command missing id=PRRT_aaa: %v", graphqlCmd)
	}

	// --- top-level shape ---
	if asInt(res["pr_number"]) != 7 || mapStr(res, "head_branch", "") != "feature/x" ||
		mapStr(res, "merge_state", "") != "merged" || !asBool(res["success"]) {
		t.Fatalf("top-level shape mismatch: %v", res)
	}
	rr := res["resolve_result"].(map[string]any)
	if !asBool(rr["pushed"]) {
		t.Fatal("resolve_result.pushed should be true")
	}
	if mapStr(res["ci_gate"].(map[string]any), "final_status", "") != "passed" {
		t.Fatalf("ci_gate.final_status mismatch: %v", res["ci_gate"])
	}
	// summary parity.
	if got := mapStr(res, "summary", ""); got != "PR #7: merge=merged, 1 file(s) changed, 1/2 comment(s) addressed, CI=passed" {
		t.Fatalf("summary = %q", got)
	}
}

func containsArg(args []string, want string) bool {
	for _, a := range args {
		if a == want {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// resolve() push fallback (maps to TestResolvePushFallback).
// ---------------------------------------------------------------------------

func TestResolvePushesWhenAgentCommittedButDidntPush(t *testing.T) {
	defer withExecCtx("run-p", "exec-p")()

	pushInvocations := [][]string{}
	git, _, restore := installGitGH(
		func(_ string, args []string) cmdResult {
			switch args[0] {
			case "fetch":
				return cmdResult{ExitCode: 0}
			case "merge-base":
				return cmdResult{ExitCode: 0} // is ancestor → clean
			case "push":
				pushInvocations = append(pushInvocations, args)
				return cmdResult{ExitCode: 0}
			case "rev-parse":
				return cmdResult{ExitCode: 0, Stdout: "sha\n"}
			}
			return cmdResult{ExitCode: 0}
		},
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
	)
	defer restore()
	_ = git

	_, restoreSleep := installSleep()
	defer restoreSleep()

	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		if strings.Contains(target, "run_pr_resolver") {
			return map[string]any{
				"fixed":       true,
				"commit_shas": []any{"abc"},
				"pushed":      false,
			}, nil
		}
		return map[string]any{}, nil
	}}
	deps := &Deps{
		App:    app,
		NodeID: "swe-planner",
		CIGate: func(context.Context, CIGateRequest) (map[string]any, error) {
			return map[string]any{"final_status": "passed"}, nil
		},
	}

	out, err := ResolveHandler(context.Background(), deps, map[string]any{
		"pr_url":      "https://github.com/o/r/pull/9",
		"pr_number":   9,
		"repo_url":    "https://github.com/o/r.git",
		"head_branch": "feature/y",
		"base_branch": "main",
	})
	if err != nil {
		t.Fatalf("resolve errored: %v", err)
	}

	pushed := false
	for _, cmd := range pushInvocations {
		if len(cmd) >= 3 && cmd[0] == "push" && cmd[1] == "origin" && cmd[2] == "feature/y" {
			pushed = true
		}
	}
	if !pushed {
		t.Fatalf("orchestrator must push on the agent's behalf; got %v", pushInvocations)
	}
	rr := out.(map[string]any)["resolve_result"].(map[string]any)
	if !asBool(rr["pushed"]) {
		t.Fatal("resolve_result.pushed should be set true after fallback push")
	}
}

// ---------------------------------------------------------------------------
// Env-derived committer identity (validation contract).
// ---------------------------------------------------------------------------

func TestResolveCommitterIdentityFromEnv(t *testing.T) {
	defer withExecCtx("run-c", "exec-c")()

	t.Setenv("SWE_AF_GIT_EMAIL", "bot@example.com")
	t.Setenv("SWE_AF_GIT_NAME", "CustomBot")

	git, _, restore := installGitGH(
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
	)
	defer restore()
	_, restoreSleep := installSleep()
	defer restoreSleep()

	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		if strings.Contains(target, "run_pr_resolver") {
			return map[string]any{"fixed": true, "pushed": true}, nil
		}
		return map[string]any{}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}

	_, err := ResolveHandler(context.Background(), deps, map[string]any{
		"pr_url":      "https://github.com/o/r/pull/3",
		"pr_number":   3,
		"repo_url":    "https://github.com/o/r.git",
		"head_branch": "feature/z",
	})
	if err != nil {
		t.Fatalf("resolve errored: %v", err)
	}

	gotEmail, gotName := configuredIdentity(*git)
	if gotEmail != "bot@example.com" || gotName != "CustomBot" {
		t.Fatalf("committer identity = (%q,%q), want (bot@example.com,CustomBot)", gotEmail, gotName)
	}
}

func TestResolveCommitterIdentityDefaults(t *testing.T) {
	defer withExecCtx("run-d", "exec-d")()

	// Ensure env is unset so defaults apply.
	t.Setenv("SWE_AF_GIT_EMAIL", "")
	t.Setenv("SWE_AF_GIT_NAME", "")

	git, _, restore := installGitGH(
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
	)
	defer restore()
	_, restoreSleep := installSleep()
	defer restoreSleep()

	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		if strings.Contains(target, "run_pr_resolver") {
			return map[string]any{"fixed": true, "pushed": true}, nil
		}
		return map[string]any{}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}

	_, err := ResolveHandler(context.Background(), deps, map[string]any{
		"pr_url":      "https://github.com/o/r/pull/4",
		"pr_number":   4,
		"repo_url":    "https://github.com/o/r.git",
		"head_branch": "feature/w",
	})
	if err != nil {
		t.Fatalf("resolve errored: %v", err)
	}

	gotEmail, gotName := configuredIdentity(*git)
	if gotEmail != "swe-af@users.noreply.github.com" || gotName != "SWE-AF" {
		t.Fatalf("default identity = (%q,%q)", gotEmail, gotName)
	}
}

// configuredIdentity extracts the values from `git config user.email/user.name`.
func configuredIdentity(calls []recCall) (email, name string) {
	for _, c := range calls {
		if len(c.args) == 3 && c.args[0] == "config" {
			switch c.args[1] {
			case "user.email":
				email = c.args[2]
			case "user.name":
				name = c.args[2]
			}
		}
	}
	return email, name
}

// ---------------------------------------------------------------------------
// CI gate skipped when check_ci is false (validation contract).
// ---------------------------------------------------------------------------

func TestResolveSkipsCIGateWhenCheckCIFalse(t *testing.T) {
	defer withExecCtx("run-n", "exec-n")()

	_, _, restore := installGitGH(
		func(_ string, args []string) cmdResult {
			if args[0] == "rev-parse" {
				return cmdResult{ExitCode: 0, Stdout: "sha\n"}
			}
			return cmdResult{ExitCode: 0}
		},
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
	)
	defer restore()
	slept, restoreSleep := installSleep()
	defer restoreSleep()

	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		if strings.Contains(target, "run_pr_resolver") {
			return map[string]any{"fixed": true, "pushed": true}, nil
		}
		return map[string]any{}, nil
	}}
	gateCalled := false
	deps := &Deps{
		App:    app,
		NodeID: "swe-planner",
		CIGate: func(context.Context, CIGateRequest) (map[string]any, error) {
			gateCalled = true
			return map[string]any{"final_status": "passed"}, nil
		},
	}

	out, err := ResolveHandler(context.Background(), deps, map[string]any{
		"pr_url":      "https://github.com/o/r/pull/5",
		"pr_number":   5,
		"repo_url":    "https://github.com/o/r.git",
		"head_branch": "feature/q",
		"config":      map[string]any{"check_ci": false},
	})
	if err != nil {
		t.Fatalf("resolve errored: %v", err)
	}
	if gateCalled {
		t.Fatal("CI gate must be skipped when check_ci is false")
	}
	if len(*slept) != 0 {
		t.Fatalf("no grace sleep expected when gate skipped; got %v", *slept)
	}
	if res := out.(map[string]any); res["ci_gate"] != nil {
		t.Fatalf("ci_gate should be nil when skipped, got %v", res["ci_gate"])
	}
}

// ---------------------------------------------------------------------------
// Failure surfaces success=false with a summary (validation contract).
// ---------------------------------------------------------------------------

func TestResolveFailureSuccessFalse(t *testing.T) {
	defer withExecCtx("run-f", "exec-f")()

	_, _, restore := installGitGH(
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
		func(_ string, _ []string) cmdResult { return cmdResult{ExitCode: 0} },
	)
	defer restore()
	_, restoreSleep := installSleep()
	defer restoreSleep()

	// Agent did not fix and did not push → success must be false.
	app := &mockApp{handler: func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		if strings.Contains(target, "run_pr_resolver") {
			return map[string]any{"fixed": false, "pushed": false, "files_changed": []any{}, "addressed_comments": []any{}}, nil
		}
		return map[string]any{}, nil
	}}
	deps := &Deps{App: app, NodeID: "swe-planner"}

	out, err := ResolveHandler(context.Background(), deps, map[string]any{
		"pr_url":      "https://github.com/o/r/pull/6",
		"pr_number":   6,
		"repo_url":    "https://github.com/o/r.git",
		"head_branch": "feature/u",
	})
	if err != nil {
		t.Fatalf("resolve errored: %v", err)
	}
	res := out.(map[string]any)
	if asBool(res["success"]) {
		t.Fatal("success should be false when agent did not fix/push")
	}
	if mapStr(res, "summary", "") == "" {
		t.Fatal("summary must be present even on failure")
	}
}
