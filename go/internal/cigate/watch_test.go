package cigate

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// ---- test doubles -------------------------------------------------------

// scriptedRunner returns pre-baked commandResult values keyed by command
// shape (the first 3 args: `gh pr checks`, `gh run view`, `gh pr ready`).
type scriptedRunner struct {
	calls        [][]string
	checksQueue  []commandResult
	runViewQueue []commandResult
	readyQueue   []commandResult
	onCall       func() // optional side effect (e.g. advance the fake clock)
}

func (s *scriptedRunner) run(_ context.Context, cmd []string, _ string) commandResult {
	if s.onCall != nil {
		s.onCall()
	}
	dup := append([]string(nil), cmd...)
	s.calls = append(s.calls, dup)
	kind := prefix3(cmd)
	switch kind {
	case "gh pr checks":
		if len(s.checksQueue) == 0 {
			panic("ran out of scripted gh pr checks replies")
		}
		r := s.checksQueue[0]
		s.checksQueue = s.checksQueue[1:]
		return r
	case "gh run view":
		if len(s.runViewQueue) > 0 {
			r := s.runViewQueue[0]
			s.runViewQueue = s.runViewQueue[1:]
			return r
		}
		return commandResult{Stdout: "(no log captured)\n"}
	case "gh pr ready":
		if len(s.readyQueue) > 0 {
			r := s.readyQueue[0]
			s.readyQueue = s.readyQueue[1:]
			return r
		}
		return commandResult{}
	}
	panic(fmt.Sprintf("unexpected command in test: %v", cmd))
}

func prefix3(cmd []string) string {
	if len(cmd) >= 3 {
		return strings.Join(cmd[:3], " ")
	}
	return strings.Join(cmd, " ")
}

// install wires the scripted runner + a fake clock + no-op sleep into the
// package seams and returns a restore func.
func install(t *testing.T, r *scriptedRunner, clock *float64) func() {
	t.Helper()
	origExec, origSleep, origNow := execCommand, sleepFn, nowFn
	execCommand = r.run
	sleepFn = func(_ context.Context, _ int) {}
	nowFn = func() float64 { return *clock }
	return func() {
		execCommand, sleepFn, nowFn = origExec, origSleep, origNow
	}
}

func completed(stdout, stderr string, rc int) commandResult {
	return commandResult{Stdout: stdout, Stderr: stderr, ReturnCode: rc}
}

func mkCheck(bucket, name string) map[string]any {
	if name == "" {
		name = "Tests"
	}
	return map[string]any{
		"bucket":   bucket,
		"state":    strings.ToUpper(bucket),
		"name":     name,
		"workflow": "CI",
		"link":     "https://github.com/o/r/actions/runs/12345/job/1",
	}
}

func mkCheckSHA(bucket, name, headSHA string) map[string]any {
	c := mkCheck(bucket, name)
	c["headSha"] = headSHA
	return c
}

func jsonArr(checks ...map[string]any) string {
	b, _ := json.Marshal(checks)
	return string(b)
}

// ---- pure-helper tests --------------------------------------------------

func TestParseChecks(t *testing.T) {
	if got, err := parseChecks(""); err != nil || len(got) != 0 {
		t.Fatalf("empty: got %v err %v", got, err)
	}
	if got, err := parseChecks("   \n"); err != nil || len(got) != 0 {
		t.Fatalf("whitespace: got %v err %v", got, err)
	}
	got, err := parseChecks(`[{"bucket":"pass","name":"a"}]`)
	if err != nil || len(got) != 1 || got[0]["name"] != "a" {
		t.Fatalf("array: got %v err %v", got, err)
	}
	if _, err := parseChecks(`{"not":"array"}`); err == nil {
		t.Fatal("non-array JSON should error")
	}
}

func TestIsConclusive(t *testing.T) {
	if !isConclusive([]map[string]any{mkCheck("pass", ""), mkCheck("fail", "")}) {
		t.Fatal("pass+fail should be conclusive")
	}
	if isConclusive([]map[string]any{mkCheck("pass", ""), mkCheck("pending", "")}) {
		t.Fatal("pending should not be conclusive")
	}
	if isConclusive([]map[string]any{mkCheck("queued", "")}) {
		t.Fatal("queued should not be conclusive")
	}
	if !isConclusive([]map[string]any{mkCheck("skip", "")}) {
		t.Fatal("skip should be conclusive")
	}
}

func TestClassify(t *testing.T) {
	if classify([]map[string]any{mkCheck("pass", ""), mkCheck("skip", "")}) != "passed" {
		t.Fatal("pass+skip should be passed")
	}
	if classify([]map[string]any{mkCheck("pass", ""), mkCheck("fail", "")}) != "failed" {
		t.Fatal("any fail should be failed")
	}
	if classify([]map[string]any{mkCheck("cancel", "")}) != "failed" {
		t.Fatal("cancel should be failed")
	}
}

func TestExtractRunID(t *testing.T) {
	if got := extractRunID("https://github.com/o/r/actions/runs/12345/job/678"); got != "12345" {
		t.Fatalf("got %q", got)
	}
	if got := extractRunID(""); got != "" {
		t.Fatalf("empty: got %q", got)
	}
	if got := extractRunID("not a url"); got != "" {
		t.Fatalf("nonmatch: got %q", got)
	}
}

func TestTail(t *testing.T) {
	out := tail(strings.Repeat("x", 5000), 100)
	if !strings.HasPrefix(out, "…[truncated]…") {
		t.Fatalf("missing prefix: %q", out[:20])
	}
	if got := len([]rune(strings.TrimRight(out, "\n\r "))); got != len([]rune("…[truncated]…\n"))+100 {
		t.Fatalf("wrong length: %d", got)
	}
	if tail("short", logTailChars) != "short" {
		t.Fatal("short should pass through")
	}
}

// ---- watch loop tests (contract) ---------------------------------------

// Contract: conclusive-green on first poll => passed, no failed checks, one poll.
func TestPassesWhenFirstPollAllGreen(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue, completed(jsonArr(mkCheck("pass", "Tests"), mkCheck("pass", "Lint")), "", 0))
	clock := 0.0
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 42, 600, 10, "")
	if res.Status != "passed" || res.PRNumber != 42 || len(res.FailedChecks) != 0 {
		t.Fatalf("got %+v", res)
	}
	if len(r.calls) != 1 {
		t.Fatalf("expected 1 poll, got %d", len(r.calls))
	}
}

// Contract: any failing check => failed, with excerpt fetched via gh run view.
func TestFailsAndCollectsFailedLogs(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue, completed(jsonArr(mkCheck("pass", "Lint"), mkCheck("fail", "Tests")), "", 0))
	r.runViewQueue = append(r.runViewQueue, completed("E   AssertionError: foo != bar\n", "", 0))
	clock := 0.0
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 7, 600, 10, "")
	if res.Status != "failed" || len(res.FailedChecks) != 1 {
		t.Fatalf("got %+v", res)
	}
	fc := res.FailedChecks[0]
	if fc.Name != "Tests" || !strings.Contains(fc.LogsExcerpt, "AssertionError") {
		t.Fatalf("bad failed check %+v", fc)
	}
	var checksCalls, viewCalls int
	for _, c := range r.calls {
		switch prefix3(c) {
		case "gh pr checks":
			checksCalls++
		case "gh run view":
			viewCalls++
		}
	}
	if checksCalls != 1 || viewCalls != 1 {
		t.Fatalf("calls: checks=%d view=%d", checksCalls, viewCalls)
	}
}

// Contract: watcher polls (and sleeps) until conclusive.
func TestPollsUntilConclusive(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue,
		completed(jsonArr(mkCheck("pending", "Tests")), "", 0),
		completed(jsonArr(mkCheck("pending", "Tests")), "", 0),
		completed(jsonArr(mkCheck("pass", "Tests")), "", 0),
	)
	clock := 0.0
	r.onCall = func() { clock += 5.0 }
	var sleeps []int
	defer install(t, r, &clock)()
	sleepFn = func(_ context.Context, s int) { sleeps = append(sleeps, s) }

	res := WatchPRChecks(context.Background(), "/tmp/repo", 1, 600, 10, "")
	if res.Status != "passed" {
		t.Fatalf("got %+v", res)
	}
	if len(r.calls) != 3 {
		t.Fatalf("expected 3 polls, got %d", len(r.calls))
	}
	if len(sleeps) != 2 || sleeps[0] != 10 || sleeps[1] != 10 {
		t.Fatalf("sleeps = %v", sleeps)
	}
}

// Contract: checks never settle before wait cap => timed_out.
func TestTimesOutWhenChecksNeverSettle(t *testing.T) {
	r := &scriptedRunner{}
	for i := 0; i < 20; i++ {
		r.checksQueue = append(r.checksQueue, completed(jsonArr(mkCheck("pending", "Tests")), "", 0))
	}
	clock := 0.0
	r.onCall = func() { clock += 100.0 }
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 99, 300, 50, "")
	if res.Status != "timed_out" {
		t.Fatalf("got %+v", res)
	}
	if res.ElapsedSeconds < 300 {
		t.Fatalf("elapsed %d < 300", res.ElapsedSeconds)
	}
}

// Contract: PR with no CI => no_checks.
func TestNoChecksWhenPRHasNoCI(t *testing.T) {
	r := &scriptedRunner{}
	for i := 0; i < 5; i++ {
		r.checksQueue = append(r.checksQueue, completed("[]", "", 0))
	}
	clock := 0.0
	r.onCall = func() { clock += 200.0 }
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 5, 300, 50, "")
	if res.Status != "no_checks" {
		t.Fatalf("got %+v", res)
	}
}

// Contract: `gh pr checks` on a PR whose repo has NO CI workflows exits
// NON-ZERO with an EMPTY stdout and a "no checks reported on the '<branch>'
// branch" stderr — the real gh behavior (verified against gh 2.76.2), NOT an
// empty JSON array with a zero exit. The watcher must treat that as the
// no_checks signal after the wait cap, not status="error".
//
// Regression test: the pre-fix watcher returned status="error" here (it aborted
// on any non-zero exit with an empty body), so a workflow-less repo could never
// reach the no_checks verdict.
func TestNoChecksWhenGhReportsNoChecksReported(t *testing.T) {
	r := &scriptedRunner{}
	for i := 0; i < 5; i++ {
		r.checksQueue = append(r.checksQueue, completed(
			"", "no checks reported on the 'feature/foo' branch", 1))
	}
	clock := 0.0
	r.onCall = func() { clock += 200.0 }
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 7, 300, 50, "")
	if res.Status != "no_checks" {
		t.Fatalf("expected no_checks for a repo with no CI, got %+v", res)
	}
}

// Contract: a GENUINE `gh pr checks` failure (non-zero exit, empty body, and a
// stderr that is NOT the benign "no checks reported" message) must still surface
// as status="error" — the benign-case handling must not swallow real failures.
func TestGenuineGhErrorStillReported(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue, completed(
		"", "could not determine base repo; authentication failed", 1))
	clock := 0.0
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 9, 600, 10, "")
	if res.Status != "error" {
		t.Fatalf("expected error for a genuine gh failure, got %+v", res)
	}
}

// Contract: non-zero exit but valid JSON body => body is truth (failed).
func TestFailedChecksWithNonzeroExitStillParsed(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue, completed(
		jsonArr(mkCheck("pass", "Lint"), mkCheck("fail", "Tests")),
		"some checks failing", 8))
	r.runViewQueue = append(r.runViewQueue, completed("boom", "", 0))
	clock := 0.0
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 11, 600, 10, "")
	if res.Status != "failed" || len(res.FailedChecks) != 1 {
		t.Fatalf("got %+v", res)
	}
}

// Contract: gh fails with no payload => error status carrying stderr.
func TestRealErrorWhenGhFailsWithNoPayload(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue, completed("", "gh: not authenticated", 1))
	clock := 0.0
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 3, 600, 10, "")
	if res.Status != "error" || !strings.Contains(res.Summary, "not authenticated") {
		t.Fatalf("got %+v", res)
	}
}

// ---- SHA anchoring (contract) ------------------------------------------

// Contract: stale (previous-HEAD) passed checks must not yield a verdict
// until a check for the requested SHA appears.
func TestStalePassedChecksIgnoredUntilNewSHA(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue,
		completed(jsonArr(mkCheckSHA("pass", "Old Lint", "oldsha111")), "", 0),
		completed(jsonArr(mkCheckSHA("pass", "Old Lint", "oldsha111")), "", 0),
		completed(jsonArr(
			mkCheckSHA("pass", "Old Lint", "oldsha111"),
			mkCheckSHA("pass", "Tests", "newsha222"),
		), "", 0),
	)
	clock := 0.0
	r.onCall = func() { clock += 5.0 }
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 42, 600, 10, "newsha222")
	if res.Status != "passed" {
		t.Fatalf("got %+v", res)
	}
	if len(r.calls) != 3 {
		t.Fatalf("SHA anchor should force 3 polls, got %d", len(r.calls))
	}
}

// Contract: previous-HEAD FAILED checks must not poison the new SHA's verdict.
func TestStaleFailedChecksDontShortCircuit(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue,
		completed(jsonArr(mkCheckSHA("fail", "Old Tests", "oldsha111")), "", 0),
		completed(jsonArr(
			mkCheckSHA("fail", "Old Tests", "oldsha111"),
			mkCheckSHA("pass", "Tests", "newsha222"),
		), "", 0),
	)
	clock := 0.0
	r.onCall = func() { clock += 5.0 }
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 42, 600, 10, "newsha222")
	if res.Status != "passed" {
		t.Fatalf("got %+v", res)
	}
}

// Contract: wait cap fires and no check for the SHA was ever seen => no_checks
// with a SHA-specific summary.
func TestNoChecksWhenOnlyOtherSHASeen(t *testing.T) {
	r := &scriptedRunner{}
	for i := 0; i < 5; i++ {
		r.checksQueue = append(r.checksQueue, completed(jsonArr(mkCheckSHA("pass", "Old", "oldsha111")), "", 0))
	}
	clock := 0.0
	r.onCall = func() { clock += 100.0 }
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 42, 300, 50, "newsha222")
	if res.Status != "no_checks" || !strings.Contains(res.Summary, "newsha222") {
		t.Fatalf("got %+v", res)
	}
}

// Contract: older gh without headSha field => degrade gracefully, verdict
// eligible.
func TestMissingHeadSHAFieldDoesNotBlockVerdict(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue, completed(jsonArr(mkCheck("pass", "Tests")), "", 0))
	clock := 0.0
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 42, 600, 10, "newsha222")
	if res.Status != "passed" {
		t.Fatalf("got %+v", res)
	}
}

// Contract: without head_sha, behavior is unchanged (short-circuit on poll 1).
func TestNoAnchorPreservesBehavior(t *testing.T) {
	r := &scriptedRunner{}
	r.checksQueue = append(r.checksQueue, completed(jsonArr(mkCheckSHA("pass", "Tests", "anysha")), "", 0))
	clock := 0.0
	defer install(t, r, &clock)()

	res := WatchPRChecks(context.Background(), "/tmp/repo", 42, 600, 10, "")
	if res.Status != "passed" || len(r.calls) != 1 {
		t.Fatalf("got %+v calls=%d", res, len(r.calls))
	}
}

// ---- ctx cancellation (contract) ---------------------------------------

// Contract: a cancelled context stops polling promptly with an error verdict.
func TestCtxCancellationStopsPolling(t *testing.T) {
	r := &scriptedRunner{}
	// Always pending — the loop would spin forever without cancellation.
	for i := 0; i < 50; i++ {
		r.checksQueue = append(r.checksQueue, completed(jsonArr(mkCheck("pending", "Tests")), "", 0))
	}
	clock := 0.0
	defer install(t, r, &clock)()

	ctx, cancel := context.WithCancel(context.Background())
	// Cancel after the first poll so we prove the loop honors ctx and does
	// not exhaust the queue / hit the wait cap.
	r.onCall = func() { cancel() }

	res := WatchPRChecks(ctx, "/tmp/repo", 42, 100000, 10, "")
	if res.Status != "error" || !strings.Contains(res.Summary, "cancelled") {
		t.Fatalf("got %+v", res)
	}
	if len(r.calls) > 2 {
		t.Fatalf("cancellation should stop promptly, got %d polls", len(r.calls))
	}
}

// ---- mark ready ---------------------------------------------------------

func TestMarkPRReady(t *testing.T) {
	r := &scriptedRunner{}
	clock := 0.0
	defer install(t, r, &clock)()

	r.readyQueue = append(r.readyQueue, completed("", "", 0))
	ok, msg := MarkPRReady(context.Background(), "/tmp/r", 42)
	if !ok || !strings.Contains(msg, "#42") {
		t.Fatalf("success: ok=%v msg=%q", ok, msg)
	}
	last := r.calls[len(r.calls)-1]
	if prefix3(last) != "gh pr ready" || last[3] != "42" {
		t.Fatalf("wrong command %v", last)
	}

	r.readyQueue = append(r.readyQueue, completed("", "not a draft", 1))
	ok, msg = MarkPRReady(context.Background(), "/tmp/r", 42)
	if ok || !strings.Contains(msg, "not a draft") {
		t.Fatalf("failure: ok=%v msg=%q", ok, msg)
	}
}
