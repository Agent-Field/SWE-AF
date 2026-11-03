package coding

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

func resumeRun(t *testing.T, root, name string, budget, round int, callFn CallFn) schemas.IssueResult {
	t.Helper()
	state := makeDAGState(root)
	state.BuildID = "build"
	result, err := RunCodingLoop(context.Background(), map[string]any{"name": name, "branch_name": "test/resume"},
		state, callFn, "test", makeConfig(t, map[string]any{"max_coding_iterations": budget, "enable_issue_advisor": false}), nil, nil, round)
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func resumeNoCalls(t *testing.T) CallFn {
	return func(context.Context, string, map[string]any) (map[string]any, error) {
		t.Fatal("An exhausted window must use history without agent calls")
		return nil, nil
	}
}

func resumeCheckpoint(root string, history []map[string]any, files []string) {
	saveIterationState(root, "resume", map[string]any{
		"iteration": 1, "feedback": "recorded feedback", "files_changed": files, "iteration_history": history,
	}, "build")
}

func TestResumeFinishedOneIterationWindow(t *testing.T) {
	for _, round := range []int{0, 1} {
		for _, approved := range []bool{true, false} {
			t.Run(fmt.Sprintf("round%d/approved%v", round, approved), func(t *testing.T) {
				root := t.TempDir()
				calls := 0
				callFn := func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
					calls++
					if lastSeg(target) == "run_coder" {
						return map[string]any{"files_changed": []string{"app.py"}, "repo_name": "api"}, nil
					}
					return map[string]any{"approved": approved, "blocking": false, "summary": "Verified feedback",
						"debt_items": []map[string]any{{"severity": "should_fix", "title": "Decode errors"}}}, nil
				}
				first := resumeRun(t, root, "resume", 1, round, callFn)
				resumed := resumeRun(t, root, "resume", 1, round, resumeNoCalls(t))
				expected := schemas.IssueOutcomeCompleted
				if !approved {
					expected = schemas.IssueOutcomeCompletedWithDebt
				}
				if calls != 2 || first.Outcome != expected || resumed.Outcome != expected || resumed.Attempts != 1 || first.ResultSummary != resumed.ResultSummary ||
					!reflect.DeepEqual(first.FilesChanged, resumed.FilesChanged) || first.BranchName != resumed.BranchName {
					t.Fatalf("calls=%d first=%+v resumed=%+v", calls, first, resumed)
				}
				if approved && resumed.RepoName != "api" {
					t.Fatal("lost recorded repo_name")
				}
				if !approved && (len(resumed.DebtItems) == 0 || !reflect.DeepEqual(first.DebtItems, resumed.DebtItems)) {
					t.Fatalf("lost review debt: %+v", resumed)
				}
				data, err := os.ReadFile(resumeExpectedPath(root, "resume", round))
				if err != nil {
					t.Fatal(err)
				}
				var state map[string]any
				if err := json.Unmarshal(data, &state); err != nil {
					t.Fatal(err)
				}
				if state["result"] != nil || state["issue"] != nil {
					t.Fatal("whole-result replay/ownership must not be persisted")
				}
			})
		}
	}
}

func TestResumeLegacyHistoryRequiresPositiveEvidence(t *testing.T) {
	tests := []struct {
		name    string
		entry   map[string]any
		files   []string
		outcome schemas.IssueOutcome
	}{
		{"approved", map[string]any{"iteration": 1, "action": "approve", "review_approved": true, "review_blocking": false, "summary": "Approved"}, nil, schemas.IssueOutcomeCompleted},
		{"synthesizer_approved", map[string]any{"iteration": 1, "action": "approve", "review_approved": false, "review_blocking": false, "path": "flagged"}, []string{"app.py"}, schemas.IssueOutcomeCompleted},
		{"debt", map[string]any{"iteration": 1, "action": "fix", "review_approved": false, "review_blocking": false, "summary": "Polish"}, []string{"app.py"}, schemas.IssueOutcomeCompletedWithDebt},
		{"blocking", map[string]any{"iteration": 1, "action": "fix", "review_blocking": true}, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
		{"empty", nil, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
		{"missing_review", map[string]any{"iteration": 1, "action": "fix"}, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
		{"missing_review_approval", map[string]any{"iteration": 1, "action": "approve"}, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
		{"missing_approval", map[string]any{"iteration": 1, "action": "approve", "review_blocking": false}, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
		{"no_changes", map[string]any{"iteration": 1, "action": "fix", "review_blocking": false}, nil, schemas.IssueOutcomeFailedUnrecoverable},
		{"blocked_action", map[string]any{"iteration": 1, "action": "block", "review_blocking": false}, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
		{"unknown_action", map[string]any{"iteration": 1, "review_blocking": false}, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
		{"missing_iteration", map[string]any{"action": "fix", "review_blocking": false}, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
		{"null_iteration", map[string]any{"iteration": nil, "action": "fix", "review_blocking": false}, []string{"app.py"}, schemas.IssueOutcomeFailedUnrecoverable},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			history := []map[string]any{}
			if tc.entry != nil {
				history = append(history, tc.entry)
			}
			resumeCheckpoint(root, history, tc.files)
			result := resumeRun(t, root, "resume", 1, 0, resumeNoCalls(t))
			if result.Outcome != tc.outcome {
				t.Fatalf("result=%+v want %s", result, tc.outcome)
			}
			if tc.outcome == schemas.IssueOutcomeCompletedWithDebt && (len(result.DebtItems) == 0 || result.DebtItems[0]["description"] != "Polish") {
				t.Fatalf("missing legacy summary debt: %+v", result)
			}
		})
	}
}

func TestResumeRemainingBudgetRunsNextIterationAfterApproval(t *testing.T) {
	root := t.TempDir()
	resumeCheckpoint(root, []map[string]any{{"iteration": 1, "action": "approve", "review_approved": true, "review_blocking": false}}, []string{"app.py"})
	iterations := []int{}
	callFn := func(_ context.Context, target string, kw map[string]any) (map[string]any, error) {
		if lastSeg(target) == "run_coder" {
			iterations = append(iterations, toInt(kw["iteration"]))
			return map[string]any{"files_changed": []string{"restored.py"}}, nil
		}
		return map[string]any{"approved": true, "blocking": false}, nil
	}
	result := resumeRun(t, root, "resume", 3, 0, callFn)
	if !reflect.DeepEqual(iterations, []int{2}) || result.Outcome != schemas.IssueOutcomeCompleted || !contains(result.FilesChanged, "restored.py") {
		t.Fatalf("iterations=%v result=%+v", iterations, result)
	}
}

func TestResumeNullHistoryIsUnestablished(t *testing.T) {
	root := t.TempDir()
	resumeCheckpoint(root, nil, []string{"app.py"})
	result := resumeRun(t, root, "resume", 1, 0, resumeNoCalls(t))
	if result.Outcome != schemas.IssueOutcomeFailedUnrecoverable {
		t.Fatal(result)
	}
}

func TestResumeAdvisorRoundAndLiteralIssueNeverShareState(t *testing.T) {
	for _, reverse := range []bool{false, true} {
		t.Run(fmt.Sprint(reverse), func(t *testing.T) {
			root := t.TempDir()
			windows := []struct {
				name  string
				round int
			}{{"parent", 1}, {"parent.r1", 0}}
			if reverse {
				windows[0], windows[1] = windows[1], windows[0]
			}
			paths := []string{}
			for _, w := range windows {
				paths = append(paths, resumeExpectedPath(root, w.name, w.round))
				calls := 0
				callFn := func(_ context.Context, target string, kw map[string]any) (map[string]any, error) {
					calls++
					if lastSeg(target) == "run_coder" {
						if toInt(kw["iteration"]) != 1 {
							t.Fatal("resumed another issue")
						}
						return map[string]any{"files_changed": []string{w.name + ".py"}}, nil
					}
					return map[string]any{"approved": true, "blocking": false, "summary": w.name}, nil
				}
				result := resumeRun(t, root, w.name, 1, w.round, callFn)
				if calls != 2 || !reflect.DeepEqual(result.FilesChanged, []string{w.name + ".py"}) {
					t.Fatalf("calls=%d result=%+v", calls, result)
				}
			}
			if paths[0] == paths[1] {
				t.Fatal("round checkpoint aliases literal issue")
			}
			for i, w := range windows {
				if _, err := os.Stat(paths[i]); err != nil {
					t.Fatal(err)
				}
				result := resumeRun(t, root, w.name, 1, w.round, resumeNoCalls(t))
				if result.ResultSummary != w.name || !reflect.DeepEqual(result.FilesChanged, []string{w.name + ".py"}) {
					t.Fatal(result)
				}
			}
		})
	}
}

func TestResumeRoundZeroPathMatchesBase(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"parent", "parent.r1", "parent%2Er1", "advisor-rounds", ".", "..", "", "advisor-rounds/r1/victim", "../../victim", `a\b`, "C:drive", strings.Repeat("a", 300)} {
		for _, buildID := range []string{"", "build"} {
			want := filepath.Join(root, "execution", "iterations", buildID, name+".json")
			if iterationStateKey(name, 0) != name || iterationStatePath(root, iterationStateKey(name, 0), buildID) != want {
				t.Fatal(name, buildID)
			}
		}
	}
}

// Expected paths are independent of production helpers so restoring HEAD source
// still compiles and demonstrates behavioral failures.
func resumeExpectedPath(root, name string, round int) string {
	if round != 0 {
		return filepath.Join(root, "execution", "iteration-rounds", "build", fmt.Sprintf("r%d", round), name+".json")
	}
	return filepath.Join(root, "execution", "iterations", "build", name+".json")
}

func TestResumeRoundPathIsBelowSeparateDirectory(t *testing.T) {
	for _, tc := range []struct{ name, encoded string }{{"parent", "parent"}, {"parent.r1", "parent.r1"}, {"parent%2Er1", "parent%252Er1"}, {"advisor-rounds", "advisor-rounds"}, {".", "."}, {"..", ".."}} {
		name := tc.name
		root := t.TempDir()
		fn := func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
			if lastSeg(target) == "run_coder" {
				return map[string]any{"files_changed": []string{"app.py"}}, nil
			}
			return map[string]any{"approved": true, "blocking": false}, nil
		}
		resumeRun(t, root, name, 1, 1, fn)
		if _, err := os.Stat(resumeExpectedPath(root, tc.encoded, 1)); err != nil {
			t.Fatal(err)
		}
	}
}

func TestResumeReviewOutagePreservesBlockerAndStuckFingerprint(t *testing.T) {
	for _, budget := range []int{2, 5} {
		t.Run(fmt.Sprint(budget), func(t *testing.T) {
			reviews := 0
			feedback := []string{}
			callFn := func(_ context.Context, target string, kw map[string]any) (map[string]any, error) {
				if lastSeg(target) == "run_coder" {
					feedback = append(feedback, mapGetStr(kw, "feedback", ""))
					return map[string]any{"files_changed": []string{"query.py"}}, nil
				}
				reviews++
				if reviews > 1 {
					return nil, errors.New("Reviewer unavailable")
				}
				return map[string]any{"approved": false, "blocking": true, "summary": "Unsafe query", "debt_items": []map[string]any{{"severity": "blocking", "title": "SQL injection", "description": "Parameterize input", "file_path": "query.py"}}}, nil
			}
			result := resumeRun(t, t.TempDir(), "resume", budget, 0, callFn)
			attempts := budget
			if budget > 3 {
				attempts = 3
			}
			if result.Outcome != schemas.IssueOutcomeFailedUnrecoverable || result.Attempts != attempts || reviews != attempts || !strings.Contains(feedback[1], "Parameterize input") {
				t.Fatalf("reviews=%d result=%+v", reviews, result)
			}
			for _, entry := range result.IterationHistory {
				if entry["action"] != "fix" || entry["review_blocking"] != true || entry["review_findings"] != "sql injection" {
					t.Fatal(entry)
				}
			}
			if attempts == 3 && !strings.Contains(result.ErrorMessage, "Stuck loop") {
				t.Fatal(result)
			}
		})
	}
}

func TestResumeReviewOutagePreservesCheckpointBlocker(t *testing.T) {
	for _, legacy := range []bool{false, true} {
		t.Run(fmt.Sprint(legacy), func(t *testing.T) {
			root := t.TempDir()
			entry := map[string]any{"iteration": 1, "action": "fix", "review_approved": false, "review_blocking": true, "review_findings": "sql injection", "summary": "Unsafe query"}
			if !legacy {
				entry["review_debt_items"] = []map[string]any{{"severity": "blocking", "title": "SQL injection", "description": "Parameterize input"}}
			}
			resumeCheckpoint(root, []map[string]any{entry}, []string{"app.py"})
			callFn := func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
				if lastSeg(target) == "run_coder" {
					return map[string]any{"files_changed": []string{"app.py"}}, nil
				}
				return nil, errors.New("Reviewer unavailable")
			}
			result := resumeRun(t, root, "resume", 5, 0, callFn)
			if result.Outcome != schemas.IssueOutcomeFailedUnrecoverable || result.Attempts != 3 {
				t.Fatal(result)
			}
			for _, entry := range result.IterationHistory {
				if entry["review_findings"] != "sql injection" {
					t.Fatal(entry)
				}
			}
		})
	}
}

func TestResumeReviewOutageWithoutUnresolvedBlockerKeepsFailOpen(t *testing.T) {
	for _, cleared := range []bool{false, true} {
		t.Run(fmt.Sprint(cleared), func(t *testing.T) {
			reviews := 0
			callFn := func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
				if lastSeg(target) == "run_coder" {
					return map[string]any{"files_changed": []string{"app.py"}}, nil
				}
				reviews++
				if cleared && reviews <= 2 {
					return map[string]any{"approved": false, "blocking": reviews == 1, "summary": "Known feedback"}, nil
				}
				return nil, errors.New("Reviewer unavailable")
			}
			result := resumeRun(t, t.TempDir(), "resume", 3, 0, callFn)
			want := 1
			if cleared {
				want = 3
			}
			if result.Outcome != schemas.IssueOutcomeCompleted || reviews != want {
				t.Fatal(result, reviews)
			}
		})
	}
}

func TestResumeAdvisorRoundAliasesStayIndependent(t *testing.T) {
	pairs := [][]struct {
		name  string
		round int
	}{
		{{"victim", 1}, {"advisor-rounds/r1/victim", 0}},
		{{"victim", 0}, {"../../victim", 1}},
	}
	for i, pair := range pairs {
		for _, reverse := range []bool{false, true} {
			t.Run(fmt.Sprintf("pair%d/reverse%v", i, reverse), func(t *testing.T) {
				root := t.TempDir()
				if err := os.MkdirAll(filepath.Join(root, "execution", "iterations", "build", "advisor-rounds", "r1"), 0o755); err != nil {
					t.Fatal(err)
				}
				windows := append([]struct {
					name  string
					round int
				}{}, pair...)
				if reverse {
					windows[0], windows[1] = windows[1], windows[0]
				}
				for _, w := range windows {
					calls := 0
					fn := func(_ context.Context, target string, kw map[string]any) (map[string]any, error) {
						calls++
						if lastSeg(target) == "run_coder" {
							if toInt(kw["iteration"]) != 1 {
								t.Fatal("foreign iteration")
							}
							return map[string]any{"files_changed": []string{w.name + ".py"}}, nil
						}
						return map[string]any{"approved": true, "blocking": false, "summary": w.name}, nil
					}
					result := resumeRun(t, root, w.name, 1, w.round, fn)
					if calls != 2 || result.ResultSummary != w.name || !reflect.DeepEqual(result.FilesChanged, []string{w.name + ".py"}) {
						t.Fatalf("calls=%d result=%+v", calls, result)
					}
				}
				for _, w := range windows {
					result := resumeRun(t, root, w.name, 1, w.round, resumeNoCalls(t))
					if result.ResultSummary != w.name {
						t.Fatal(result)
					}
				}
			})
		}
	}
}

func TestResumeRetryNameIsOneEncodedComponent(t *testing.T) {
	names := []struct{ name, encoded string }{
		{"a/b", "a%2Fb"}, {`a\b`, "a%5Cb"}, {"a%b", "a%25b"},
		{"C:drive", "C%3Adrive"}, {"a%2Fb", "a%252Fb"},
		{".", "."}, {"..", ".."}, {"", ""},
		{"CON", "%43ON"}, {"nul.txt", "%6Eul.txt"},
		{"a<>\"|?*\x00\x1f\x7f", "a%3C%3E%22%7C%3F%2A%00%1F%7F"},
		{"café", "caf%C3%A9"}, {"space ", "space%20"},
	}
	for _, tc := range names {
		for _, buildID := range []string{"", "build"} {
			t.Run(fmt.Sprintf("%q/build%s", tc.name, buildID), func(t *testing.T) {
				state := makeDAGState(t.TempDir())
				state.BuildID = buildID
				calls := 0
				fn := func(_ context.Context, target string, kw map[string]any) (map[string]any, error) {
					calls++
					if lastSeg(target) == "run_coder" {
						if toInt(kw["iteration"]) != 1 {
							t.Fatal("foreign iteration")
						}
						return map[string]any{"files_changed": []string{"app.py"}}, nil
					}
					return map[string]any{"approved": true, "blocking": false, "summary": tc.name}, nil
				}
				cfg := makeConfig(t, map[string]any{"max_coding_iterations": 1})
				first, err := RunCodingLoop(context.Background(), map[string]any{"name": tc.name}, state, fn, "test", cfg, nil, nil, 2)
				if err != nil {
					t.Fatal(err)
				}
				want := filepath.Join(state.ArtifactsDir, "execution", "iteration-rounds", buildID, "r2", tc.encoded+".json")
				if _, err := os.Stat(want); err != nil {
					t.Fatal(err)
				}
				again, err := RunCodingLoop(context.Background(), map[string]any{"name": tc.name}, state, resumeNoCalls(t), "test", cfg, nil, nil, 2)
				if err != nil || calls != 2 || first.Outcome != schemas.IssueOutcomeCompleted || again.Outcome != first.Outcome ||
					again.Attempts != first.Attempts || again.ResultSummary != first.ResultSummary || !reflect.DeepEqual(first.FilesChanged, again.FilesChanged) {
					t.Fatalf("calls=%d first=%+v again=%+v err=%v", calls, first, again, err)
				}
			})
		}
	}
}

func TestResumeVeryLongRetryNameDoesNotAlias(t *testing.T) {
	for _, round := range []int{0, 1} {
		root := t.TempDir()
		name := strings.Repeat("x", 300)
		calls := 0
		fn := func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
			calls++
			if lastSeg(target) == "run_coder" {
				return map[string]any{"files_changed": []string{"app.py"}}, nil
			}
			return map[string]any{"approved": true, "blocking": false}, nil
		}
		// Go's base behavior ignores checkpoint write errors. Both entries
		// must run, rather than resume a silently truncated checkpoint.
		resumeRun(t, root, name, 1, round, fn)
		resumeRun(t, root, name, 1, round, fn)
		if calls != 4 {
			t.Fatal("long name aliased a checkpoint")
		}
		if err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if !entry.IsDir() && strings.HasSuffix(path, ".json") && strings.Contains(path, "execution") {
				t.Fatal("truncated checkpoint", path)
			}
			return nil
		}); err != nil {
			t.Fatal(err)
		}
	}
}
