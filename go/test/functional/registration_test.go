//go:build functional

package functional

import (
	"sort"
	"testing"
)

// The parity checklist (design constraint #1, §8): every Python reasoner is a
// separately registered Go reasoner with the SAME name. These are the EXACT
// expected reasoner-name sets — derived from the Python registration surface
// (swe_af/app.py orchestrators + swe_af/reasoners/router roles; swe_af/fast/app
// fast reasoners + the same roles), NOT from reading the Go register.go. The
// test fails loudly on any missing or extra name.

// plannerReasoners: 5 orchestrators + 25 role reasoners = 30.
var plannerReasoners = []string{
	// orchestrators (swe_af/app.py)
	"build", "plan", "execute", "resolve", "resume_build",
	// planning roles
	"run_product_manager", "run_environment_scout", "run_architect",
	"run_tech_lead", "run_sprint_planner",
	// coding roles
	"run_coder", "run_qa", "run_code_reviewer", "run_qa_synthesizer",
	// git / workspace roles
	"run_git_init", "run_workspace_setup", "run_workspace_cleanup",
	"run_merger", "run_integration_tester", "run_repo_finalize", "run_github_pr",
	// advisor / verify roles
	"run_retry_advisor", "run_issue_advisor", "run_replanner", "run_verifier",
	"generate_fix_issues", "run_issue_writer",
	// ci / resolve roles
	"run_ci_watcher", "run_ci_fixer", "run_pr_resolver",
}

// fastReasoners: 4 fast reasoners + the same 25 role reasoners = 29. The fast
// node deliberately does NOT register the 5 orchestrators (swe_af/fast/app.py
// only defines its own `build`).
var fastReasoners = func() []string {
	fast := []string{"build", "fast_plan_tasks", "fast_execute_tasks", "fast_verify"}
	// the 25 roles = plannerReasoners minus the 5 orchestrators.
	orchestrators := map[string]bool{
		"build": true, "plan": true, "execute": true, "resolve": true, "resume_build": true,
	}
	for _, r := range plannerReasoners {
		if !orchestrators[r] {
			fast = append(fast, r)
		}
	}
	return fast
}()

// TestRegistrationParity asserts each node exposes EXACTLY its expected reasoner
// set — no more, no fewer (design §11(b) DAG/registration parity).
func TestRegistrationParity(t *testing.T) {
	requireStack(t)

	cases := []struct {
		nodeID string
		want   []string
		count  int
	}{
		{plannerNodeID, plannerReasoners, 30},
		{fastNodeID, fastReasoners, 29},
	}

	for _, tc := range cases {
		t.Run(tc.nodeID, func(t *testing.T) {
			if len(tc.want) != tc.count {
				t.Fatalf("test bug: %s expected-list has %d names, want %d",
					tc.nodeID, len(tc.want), tc.count)
			}

			got, err := fetchReasonerNames(tc.nodeID)
			if err != nil {
				t.Fatalf("fetch reasoner names for %s: %v", tc.nodeID, err)
			}

			want := make(map[string]bool, len(tc.want))
			for _, n := range tc.want {
				want[n] = true
			}

			missing, extra := diffSets(want, got)
			sort.Strings(missing)
			sort.Strings(extra)
			if len(missing) > 0 || len(extra) > 0 {
				t.Errorf("%s reasoner-name parity MISMATCH:\n  missing (expected, not registered): %v\n  extra (registered, not expected): %v\n  got %d names, want %d",
					tc.nodeID, missing, extra, len(got), tc.count)
			}
			if len(got) != tc.count {
				t.Errorf("%s registered %d reasoners, want exactly %d", tc.nodeID, len(got), tc.count)
			}
		})
	}
}
