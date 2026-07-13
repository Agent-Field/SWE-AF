// Package cigate holds deterministic helpers for the post-PR CI gate.
//
// The CI gate watches GitHub Actions checks on a PR after SWE-AF pushes its
// work, using the `gh` CLI. Polling and log retrieval live here so they can
// be unit-tested without a GitHub remote or an LLM in the loop.
//
// PRs are opened ready for review (no draft phase). MarkPRReady remains in
// this package as a backwards-compatible utility for callers that still
// invoke it, but the build pipeline no longer calls it.
//
// Ported verbatim from swe_af/execution/ci_gate.py.
package cigate

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// pendingBuckets are `gh pr checks --json bucket` values that mean "still
// running". Anything outside this set is conclusive (pass/fail/cancel/skip).
var pendingBuckets = map[string]bool{"pending": true, "queued": true}

// failureBuckets are the conclusive buckets that count as a failing check.
var failureBuckets = map[string]bool{"fail": true, "cancel": true}

// logTailChars is the per-failure log tail size. Big enough to surface the
// actual error, small enough to keep the CI-fixer prompt under control across
// multi-failure runs.
const logTailChars = 3000

// runIDRe pulls the run id out of the details_url returned by `gh pr checks`.
// Expected shape: https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
var runIDRe = regexp.MustCompile(`/actions/runs/(\d+)(?:/|$)`)

// commandResult mirrors subprocess.CompletedProcess for the fields we read.
type commandResult struct {
	Stdout     string
	Stderr     string
	ReturnCode int
}

// CommandRunner runs a `gh` invocation and returns its captured result. It is
// the injectable seam that lets tests fake the gh binary.
type CommandRunner func(ctx context.Context, cmd []string, cwd string) commandResult

// execCommand is the package-level seam. Tests replace it with a scripted
// runner; production uses defaultRunner (exec.CommandContext).
var execCommand CommandRunner = defaultRunner

// sleepFn is the pollable, context-aware sleep. Tests replace it to avoid
// real wall-clock waits.
var sleepFn = defaultSleep

// nowFn returns a monotonic-ish seconds clock. Tests replace it with a fake
// clock so the polling loop's elapsed-time accounting is deterministic.
var nowFn = defaultNow

var baseTime = time.Now()

func defaultNow() float64 { return time.Since(baseTime).Seconds() }

func defaultRunner(ctx context.Context, cmd []string, cwd string) commandResult {
	if len(cmd) == 0 {
		return commandResult{ReturnCode: 1}
	}
	c := exec.CommandContext(ctx, cmd[0], cmd[1:]...)
	if cwd != "" {
		c.Dir = cwd
	}
	var out, errBuf bytes.Buffer
	c.Stdout = &out
	c.Stderr = &errBuf
	rc := 0
	if err := c.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			rc = ee.ExitCode()
		} else {
			rc = 1
		}
	}
	return commandResult{Stdout: out.String(), Stderr: errBuf.String(), ReturnCode: rc}
}

// defaultSleep sleeps for `seconds`, returning early if ctx is cancelled.
func defaultSleep(ctx context.Context, seconds int) {
	t := time.NewTimer(time.Duration(seconds) * time.Second)
	defer t.Stop()
	select {
	case <-ctx.Done():
	case <-t.C:
	}
}

// mapStr returns the string value at key, coercing non-strings to "".
func mapStr(m map[string]any, key string) string {
	v, ok := m[key]
	if !ok {
		return ""
	}
	s, _ := v.(string)
	return s
}

// headSHAOf returns the normalized (trimmed) headSha of a check.
func headSHAOf(c map[string]any) string {
	return strings.TrimSpace(mapStr(c, "headSha"))
}

// parseChecks parses `gh pr checks --json` output into a list of dicts.
//
// Returns an empty list when no checks are configured for the PR yet. Returns
// an error if the payload is non-empty but not a parseable JSON array.
func parseChecks(payload string) ([]map[string]any, error) {
	text := strings.TrimSpace(payload)
	if text == "" {
		return []map[string]any{}, nil
	}
	var data []map[string]any
	if err := json.Unmarshal([]byte(text), &data); err != nil {
		// Mirror Python: distinguish "not an array" from parse errors with a
		// clearer message when the top-level JSON is a valid non-array value.
		var probe any
		if json.Unmarshal([]byte(text), &probe) == nil {
			return nil, fmt.Errorf("Expected JSON array from gh pr checks, got %T", probe)
		}
		return nil, err
	}
	return data, nil
}

// isConclusive reports whether all listed checks have settled (no
// pending/queued).
func isConclusive(checks []map[string]any) bool {
	for _, c := range checks {
		if pendingBuckets[mapStr(c, "bucket")] {
			return false
		}
	}
	return true
}

// classify classifies a conclusive set of checks. Returns "failed" if any
// required check failed/was cancelled, "passed" otherwise. "skip"/"pass"
// buckets count as passing.
func classify(checks []map[string]any) string {
	for _, c := range checks {
		if failureBuckets[mapStr(c, "bucket")] {
			return "failed"
		}
	}
	return "passed"
}

func extractRunID(detailsURL string) string {
	if detailsURL == "" {
		return ""
	}
	m := runIDRe.FindStringSubmatch(detailsURL)
	if m == nil {
		return ""
	}
	return m[1]
}

func tail(text string, maxChars int) string {
	r := []rune(text)
	if len(r) <= maxChars {
		return text
	}
	return "…[truncated]…\n" + string(r[len(r)-maxChars:])
}

// fetchFailedLogs runs `gh run view <id> --log-failed` and returns a tail of
// the output.
func fetchFailedLogs(ctx context.Context, repoPath, runID string) string {
	if runID == "" {
		return ""
	}
	proc := execCommand(ctx, []string{"gh", "run", "view", runID, "--log-failed"}, repoPath)
	body := proc.Stdout
	if proc.ReturnCode != 0 && body == "" {
		body = proc.Stderr
	}
	return tail(body, logTailChars)
}

func buildFailedChecks(ctx context.Context, rawChecks []map[string]any, repoPath string) []schemas.CIFailedCheck {
	failures := []schemas.CIFailedCheck{}
	for _, c := range rawChecks {
		if !failureBuckets[mapStr(c, "bucket")] {
			continue
		}
		detailsURL := mapStr(c, "link")
		if detailsURL == "" {
			detailsURL = mapStr(c, "detailsUrl")
		}
		runID := extractRunID(detailsURL)
		logsExcerpt := fetchFailedLogs(ctx, repoPath, runID)

		name := "?"
		if _, ok := c["name"]; ok {
			name = mapStr(c, "name")
		}
		conclusion := mapStr(c, "state")
		if _, ok := c["state"]; !ok {
			conclusion = mapStr(c, "bucket")
		}
		failures = append(failures, schemas.CIFailedCheck{
			Name:        name,
			Workflow:    mapStr(c, "workflow"),
			Conclusion:  conclusion,
			DetailsURL:  detailsURL,
			LogsExcerpt: logsExcerpt,
		})
	}
	return failures
}

// WatchPRChecks polls `gh pr checks <pr>` until conclusive, the wait cap is
// hit, or no checks exist. Returns a CIWatchResult describing the outcome.
// Failed checks include a truncated log tail fetched via
// `gh run view --log-failed` so downstream callers (the CI fixer) get
// actionable context without re-querying.
//
// When headSHA is provided, the watcher refuses to declare a verdict until
// `gh pr checks` reports a HEAD SHA matching headSHA. Without that anchor,
// immediately after a push the watcher can briefly see the PREVIOUS HEAD's
// still-cached check states, hit isConclusive, and return passed/failed
// verdicts that don't actually reflect the new commit. With it, mismatched
// checks are treated as "not yet seen for this commit" and polling continues.
//
// The poll loop honors ctx cancellation and returns promptly with an "error"
// status when ctx is done.
func WatchPRChecks(ctx context.Context, repoPath string, prNumber int, waitSeconds, pollSeconds int, headSHA string) schemas.CIWatchResult {
	start := nowFn()
	expectedSHA := strings.ToLower(strings.TrimSpace(headSHA))
	elapsed := func() int { return int(nowFn() - start) }

	var lastChecks []map[string]any
	sawAnyCheck := false
	sawAnyForSHA := false // only meaningful when expectedSHA != ""

	// Field set for classifying checks. `gh pr checks --json` does NOT expose a
	// per-check commit SHA (its schema is bucket,completedAt,description,event,
	// link,name,startedAt,state,workflow) — requesting an unknown field like
	// `headSha` makes `gh` reject the WHOLE call ("Unknown JSON field"), which
	// previously turned every watch into status="error". We therefore request
	// only valid fields; headSHAOf() consequently returns "" for every check, so
	// SHA anchoring degrades to PR-level verdicts via the shaUnsupported path
	// below (the intended fallback). Per-check SHA anchoring is not achievable
	// through `gh pr checks`.
	const fields = "bucket,state,name,workflow,link"

	for {
		// Honor cancellation promptly.
		select {
		case <-ctx.Done():
			return schemas.CIWatchResult{
				Status:         "error",
				PRNumber:       prNumber,
				ElapsedSeconds: elapsed(),
				Summary:        fmt.Sprintf("CI watch cancelled: %v", ctx.Err()),
			}
		default:
		}

		proc := execCommand(ctx, []string{
			"gh", "pr", "checks", strconv.Itoa(prNumber), "--json", fields,
		}, repoPath)

		if proc.ReturnCode != 0 {
			stderr := strings.TrimSpace(proc.Stderr)
			// `gh pr checks` exits non-zero when there ARE failed checks but
			// also when the call itself errors out. Distinguish via stdout
			// presence: a parseable JSON body means we got real check data
			// alongside the non-zero exit, so keep going.
			parsed, err := parseChecks(proc.Stdout)
			if err != nil {
				lastChecks = nil
			} else {
				lastChecks = parsed
			}
			if len(lastChecks) == 0 {
				// `gh pr checks` exits non-zero both for genuine errors AND for
				// the benign "no checks reported on the '<branch>' branch" case —
				// a PR whose repo has no CI workflows configured. That benign case
				// is precisely the no_checks signal, NOT an error: treat it as
				// "no checks seen this poll" and keep polling so the no_checks
				// verdict fires after the wait cap. Only a real error (auth,
				// network, unknown flag) aborts the watch.
				if !isNoChecksReported(stderr) {
					return schemas.CIWatchResult{
						Status:         "error",
						PRNumber:       prNumber,
						ElapsedSeconds: elapsed(),
						Summary:        "`gh pr checks` failed: " + firstN(stderr, 300),
					}
				}
				lastChecks = nil // benign: fall through to the no_checks/wait logic
			}
		} else {
			parsed, err := parseChecks(proc.Stdout)
			if err != nil {
				return schemas.CIWatchResult{
					Status:         "error",
					PRNumber:       prNumber,
					ElapsedSeconds: elapsed(),
					Summary:        fmt.Sprintf("Could not parse gh pr checks output: %v", err),
				}
			}
			lastChecks = parsed
		}

		// When a headSHA is supplied, restrict the conclusive-verdict logic
		// to checks that actually belong to that commit. Checks that report a
		// non-empty headSha are filtered; checks that report no headSha at all
		// are kept (treated as "unknown — could be ours").
		var checksForVerdict []map[string]any
		shaUnsupported := false
		if expectedSHA != "" {
			shaMatched := []map[string]any{}
			for _, c := range lastChecks {
				hs := strings.ToLower(headSHAOf(c))
				if hs == "" || hs == expectedSHA {
					shaMatched = append(shaMatched, c)
				}
			}
			checksForVerdict = shaMatched
			for _, c := range lastChecks {
				if strings.ToLower(headSHAOf(c)) == expectedSHA {
					sawAnyForSHA = true
					break
				}
			}
			// Older `gh` versions don't populate headSha at all. If we have
			// checks but none of them carry a headSha, we can't anchor — fall
			// back to the old PR-level behavior so we don't hang waiting for a
			// field that will never appear.
			if len(lastChecks) > 0 {
				anyHasSHA := false
				for _, c := range lastChecks {
					if headSHAOf(c) != "" {
						anyHasSHA = true
						break
					}
				}
				if !anyHasSHA {
					shaUnsupported = true
				}
			}
		} else {
			checksForVerdict = lastChecks
		}

		if len(lastChecks) > 0 {
			sawAnyCheck = true
		}

		// When anchored to a SHA, require that we've actually seen at least
		// one check for that SHA before declaring a verdict. Exception: when
		// the gh CLI clearly doesn't expose headSha, degrade to the old
		// behavior — every check is verdict-eligible.
		var verdictEligible bool
		if expectedSHA != "" && shaUnsupported {
			verdictEligible = len(checksForVerdict) > 0
		} else if expectedSHA != "" {
			verdictEligible = len(checksForVerdict) > 0 && sawAnyForSHA
		} else {
			verdictEligible = len(checksForVerdict) > 0
		}

		if verdictEligible && isConclusive(checksForVerdict) {
			if classify(checksForVerdict) == "passed" {
				return schemas.CIWatchResult{
					Status:         "passed",
					PRNumber:       prNumber,
					ElapsedSeconds: elapsed(),
					Summary:        fmt.Sprintf("All %d check(s) passed", len(checksForVerdict)),
				}
			}
			failures := buildFailedChecks(ctx, checksForVerdict, repoPath)
			return schemas.CIWatchResult{
				Status:         "failed",
				PRNumber:       prNumber,
				ElapsedSeconds: elapsed(),
				FailedChecks:   failures,
				Summary:        fmt.Sprintf("%d of %d check(s) failing", len(failures), len(checksForVerdict)),
			}
		}

		if elapsed() >= waitSeconds {
			if !sawAnyCheck || (expectedSHA != "" && !sawAnyForSHA) {
				extra := ""
				if expectedSHA != "" && !sawAnyForSHA {
					extra = fmt.Sprintf(" for %s", firstN(expectedSHA, 10))
				}
				return schemas.CIWatchResult{
					Status:         "no_checks",
					PRNumber:       prNumber,
					ElapsedSeconds: elapsed(),
					Summary: fmt.Sprintf(
						"No checks reported in %ds — PR has no CI configured or checks not yet started%s",
						waitSeconds, extra,
					),
				}
			}
			return schemas.CIWatchResult{
				Status:         "timed_out",
				PRNumber:       prNumber,
				ElapsedSeconds: elapsed(),
				Summary: fmt.Sprintf(
					"Checks still pending after %ds (%d reporting)",
					waitSeconds, len(checksForVerdict),
				),
			}
		}

		sleepFn(ctx, pollSeconds)
	}
}

// isNoChecksReported reports whether a `gh pr checks` stderr is the benign
// "no checks reported on the '<branch>' branch" message (a PR with no CI
// configured), as opposed to a genuine failure. gh emits this with a non-zero
// exit code, so callers must special-case it to reach the no_checks verdict.
func isNoChecksReported(stderr string) bool {
	return strings.Contains(strings.ToLower(stderr), "no checks reported")
}

// firstN returns the first n runes of s (mirrors Python's s[:n] semantics).
func firstN(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// MarkPRReady promotes a draft PR to ready-for-review via `gh pr ready <num>`.
//
// Kept for backwards compatibility and as a manually-callable utility. The
// build pipeline no longer invokes this — PRs are now opened ready for review
// from the start (no draft phase) so promotion is unnecessary.
//
// Returns (success, message). message carries the gh stderr on failure
// (truncated), or a short confirmation on success.
func MarkPRReady(ctx context.Context, repoPath string, prNumber int) (bool, string) {
	proc := execCommand(ctx, []string{"gh", "pr", "ready", strconv.Itoa(prNumber)}, repoPath)
	if proc.ReturnCode == 0 {
		return true, fmt.Sprintf("PR #%d marked ready for review", prNumber)
	}
	msg := firstN(strings.TrimSpace(proc.Stderr), 300)
	if msg == "" {
		msg = "gh pr ready failed"
	}
	return false, msg
}
