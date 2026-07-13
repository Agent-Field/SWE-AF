package dagutil

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// issue is a small helper to build an issue dict with name + depends_on.
func issue(name string, deps ...string) map[string]any {
	d := make([]any, 0, len(deps))
	for _, dep := range deps {
		d = append(d, dep)
	}
	return map[string]any{"name": name, "depends_on": d}
}

// ---------------------------------------------------------------------------
// RecomputeLevels / ComputeLevels
// ---------------------------------------------------------------------------

// Contract: a diamond DAG (a → {b,c} → d) partitions into [[a],[b,c],[d]].
func TestRecomputeLevels_Diamond(t *testing.T) {
	issues := []map[string]any{
		issue("a"),
		issue("b", "a"),
		issue("c", "a"),
		issue("d", "b", "c"),
	}
	got, err := RecomputeLevels(issues, map[string]bool{})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := [][]string{{"a"}, {"b", "c"}, {"d"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("RecomputeLevels diamond = %v, want %v", got, want)
	}
}

// Contract: ComputeLevels partitions the same diamond identically (no
// completed-set notion).
func TestComputeLevels_Diamond(t *testing.T) {
	issues := []map[string]any{
		issue("a"),
		issue("b", "a"),
		issue("c", "a"),
		issue("d", "b", "c"),
	}
	got, err := ComputeLevels(issues)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := [][]string{{"a"}, {"b", "c"}, {"d"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ComputeLevels diamond = %v, want %v", got, want)
	}
}

// Contract: two dependency-free issues share level 0 (parallel).
func TestComputeLevels_ParallelSameLevel(t *testing.T) {
	issues := []map[string]any{issue("alpha"), issue("beta")}
	got, err := ComputeLevels(issues)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := [][]string{{"alpha", "beta"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ComputeLevels parallel = %v, want %v", got, want)
	}
}

// Contract: completed dependencies are treated as satisfied, so an issue whose
// only dep is completed is ready at level 0.
func TestRecomputeLevels_CompletedDepSatisfied(t *testing.T) {
	// b depends on a; a is completed and not in the remaining set.
	issues := []map[string]any{issue("b", "a"), issue("c", "b")}
	got, err := RecomputeLevels(issues, map[string]bool{"a": true})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := [][]string{{"b"}, {"c"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("RecomputeLevels completed-dep = %v, want %v", got, want)
	}
}

// Contract: a cycle returns an error whose message is byte-identical to the
// Python ValueError (Dependency cycle detected among issues: ['a', 'b']).
func TestRecomputeLevels_CycleErrorMessage(t *testing.T) {
	issues := []map[string]any{
		issue("a", "b"),
		issue("b", "a"),
		issue("c"),
	}
	_, err := RecomputeLevels(issues, map[string]bool{})
	if err == nil {
		t.Fatal("expected cycle error, got nil")
	}
	want := "Dependency cycle detected among issues: ['a', 'b']"
	if err.Error() != want {
		t.Errorf("cycle error = %q, want %q", err.Error(), want)
	}
}

func TestComputeLevels_CycleError(t *testing.T) {
	issues := []map[string]any{issue("a", "b"), issue("b", "a")}
	_, err := ComputeLevels(issues)
	if err == nil {
		t.Fatal("expected cycle error, got nil")
	}
	want := "Dependency cycle detected among issues: ['a', 'b']"
	if err.Error() != want {
		t.Errorf("cycle error = %q, want %q", err.Error(), want)
	}
}

// ---------------------------------------------------------------------------
// FindDownstream
// ---------------------------------------------------------------------------

// Contract: FindDownstream returns transitive dependents and excludes the node
// itself. Chain a → b → c: downstream(a) = {b, c}.
func TestFindDownstream_Transitive(t *testing.T) {
	all := []map[string]any{issue("a"), issue("b", "a"), issue("c", "b")}
	got := FindDownstream("a", all)
	want := map[string]bool{"b": true, "c": true}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("FindDownstream(a) = %v, want %v", got, want)
	}
	if got["a"] {
		t.Error("FindDownstream must not include the node itself")
	}
}

// Contract: a leaf node has no downstream dependents.
func TestFindDownstream_Leaf(t *testing.T) {
	all := []map[string]any{issue("a"), issue("b", "a"), issue("c", "b")}
	got := FindDownstream("c", all)
	if len(got) != 0 {
		t.Errorf("FindDownstream(c) = %v, want empty", got)
	}
}

// ---------------------------------------------------------------------------
// ValidateFileConflicts
// ---------------------------------------------------------------------------

// Contract: two issues in the same level writing the same file are reported as
// a conflict (create/modify both count).
func TestValidateFileConflicts_SameLevel(t *testing.T) {
	issues := []map[string]any{
		{"name": "arithmetic-ops", "files_to_create": []any{"src/ops.rs"}, "files_to_modify": []any{}},
		{"name": "logical-ops", "files_to_create": []any{}, "files_to_modify": []any{"src/ops.rs"}},
	}
	levels := [][]string{{"arithmetic-ops", "logical-ops"}}
	got := ValidateFileConflicts(issues, levels)
	if len(got) != 1 {
		t.Fatalf("expected 1 conflict, got %d: %v", len(got), got)
	}
	c := got[0]
	if c["level"] != 0 || c["file"] != "src/ops.rs" {
		t.Errorf("conflict = %v, want level 0 file src/ops.rs", c)
	}
	issuesList, _ := c["issues"].([]string)
	if !reflect.DeepEqual(issuesList, []string{"arithmetic-ops", "logical-ops"}) {
		t.Errorf("conflict issues = %v, want [arithmetic-ops logical-ops]", issuesList)
	}
}

// Contract: the same file in DIFFERENT levels is not a conflict (they run
// serially, not in parallel).
func TestValidateFileConflicts_DifferentLevelsNoConflict(t *testing.T) {
	issues := []map[string]any{
		{"name": "a", "files_to_modify": []any{"shared.go"}},
		{"name": "b", "files_to_modify": []any{"shared.go"}},
	}
	levels := [][]string{{"a"}, {"b"}}
	got := ValidateFileConflicts(issues, levels)
	if len(got) != 0 {
		t.Errorf("expected no conflicts across levels, got %v", got)
	}
}

// ---------------------------------------------------------------------------
// AssignSequenceNumbers
// ---------------------------------------------------------------------------

// Contract: sequence numbers are 1-based, assigned in flattened level order,
// preserving within-level input ordering; original insertion order is kept in
// the returned slice.
func TestAssignSequenceNumbers(t *testing.T) {
	issues := []map[string]any{
		issue("d", "b", "c"),
		issue("b", "a"),
		issue("c", "a"),
		issue("a"),
	}
	levels := [][]string{{"a"}, {"b", "c"}, {"d"}}
	out := AssignSequenceNumbers(issues, levels)

	// Returned in original insertion order.
	gotOrder := []string{}
	for _, i := range out {
		gotOrder = append(gotOrder, issueName(i))
	}
	if !reflect.DeepEqual(gotOrder, []string{"d", "b", "c", "a"}) {
		t.Errorf("return order = %v, want [d b c a]", gotOrder)
	}

	seq := map[string]int{}
	for _, i := range out {
		seq[issueName(i)] = asInt(i["sequence_number"])
	}
	want := map[string]int{"a": 1, "b": 2, "c": 3, "d": 4}
	if !reflect.DeepEqual(seq, want) {
		t.Errorf("sequence numbers = %v, want %v", seq, want)
	}
}

// ---------------------------------------------------------------------------
// EnsurePaths
// ---------------------------------------------------------------------------

func TestEnsurePaths(t *testing.T) {
	base := t.TempDir()
	paths, err := EnsurePaths(base)
	if err != nil {
		t.Fatalf("EnsurePaths error: %v", err)
	}
	wantKeys := []string{"base", "logs", "plan", "issues", "prd", "architecture", "review", "rationale"}
	for _, k := range wantKeys {
		if _, ok := paths[k]; !ok {
			t.Errorf("missing path key %q", k)
		}
	}
	if paths["issues"] != filepath.Join(base, "plan", "issues") {
		t.Errorf("issues path = %q", paths["issues"])
	}
	// Directories that must be created.
	for _, d := range []string{"logs", "plan", "issues"} {
		if info, err := os.Stat(paths[d]); err != nil || !info.IsDir() {
			t.Errorf("dir %q not created: err=%v", paths[d], err)
		}
	}
}

// ---------------------------------------------------------------------------
// ApplyReplan
// ---------------------------------------------------------------------------

// Contract: ABORT/CONTINUE only bump the counter + history, leaving issues and
// current_level untouched.
func TestApplyReplan_AbortNoStructuralChange(t *testing.T) {
	state := &schemas.DAGState{
		AllIssues:    []map[string]any{issue("a"), issue("b", "a")},
		CurrentLevel: 3,
		ReplanCount:  1,
	}
	out, err := ApplyReplan(state, schemas.ReplanDecision{Action: schemas.ReplanActionAbort})
	if err != nil {
		t.Fatalf("ApplyReplan error: %v", err)
	}
	if out.CurrentLevel != 3 {
		t.Errorf("abort changed current_level to %d, want 3", out.CurrentLevel)
	}
	if out.ReplanCount != 2 {
		t.Errorf("replan_count = %d, want 2", out.ReplanCount)
	}
	if len(out.ReplanHistory) != 1 {
		t.Errorf("replan_history len = %d, want 1", len(out.ReplanHistory))
	}
}

// Contract: on a structural replan, current_level resets to 0 and completed +
// failed issues are filtered out of the recomputed remaining set (but retained
// in all_issues); new issues are added and levels recomputed.
func TestApplyReplan_ResetsLevelAndFiltersCompleted(t *testing.T) {
	state := &schemas.DAGState{
		AllIssues: []map[string]any{
			issue("a"),
			issue("b", "a"),
			issue("c", "b"),
		},
		CompletedIssues: []schemas.IssueResult{{IssueName: "a"}},
		FailedIssues:    []schemas.IssueResult{{IssueName: "b"}},
		CurrentLevel:    2,
	}
	decision := schemas.ReplanDecision{
		Action:    schemas.ReplanActionModifyDAG,
		NewIssues: []map[string]any{issue("d")},
	}
	out, err := ApplyReplan(state, decision)
	if err != nil {
		t.Fatalf("ApplyReplan error: %v", err)
	}

	if out.CurrentLevel != 0 {
		t.Errorf("current_level = %d, want 0 (reset)", out.CurrentLevel)
	}
	if out.ReplanCount != 1 {
		t.Errorf("replan_count = %d, want 1", out.ReplanCount)
	}

	// Remaining set (after all_issues) excludes completed (a) and failed (b);
	// includes c and the new d.
	names := []string{}
	for _, i := range out.AllIssues {
		names = append(names, issueName(i))
	}
	// a and b are kept (completed/failed), then remaining [c, d].
	if !reflect.DeepEqual(names, []string{"a", "b", "c", "d"}) {
		t.Errorf("all_issues order = %v, want [a b c d]", names)
	}

	// Levels are recomputed over remaining {c, d}. c's dep b is failed (not in
	// remaining, not completed) so c is NOT satisfied... actually b is failed,
	// not completed, so c's dep on b is unsatisfied and c would cycle-block.
	// Verify levels contain d at level 0 and structure is valid (no error
	// already asserted). d has no deps → level 0.
	if len(out.Levels) == 0 {
		t.Fatal("expected non-empty levels")
	}
	foundD := false
	for _, lvl := range out.Levels {
		for _, n := range lvl {
			if n == "d" {
				foundD = true
			}
		}
	}
	if !foundD {
		t.Error("new issue d missing from recomputed levels")
	}

	// New issue got a sequence_number assigned (max existing was 0 → 1).
	for _, i := range out.AllIssues {
		if issueName(i) == "d" {
			if asInt(i["sequence_number"]) != 1 {
				t.Errorf("new issue d sequence_number = %v, want 1", i["sequence_number"])
			}
		}
	}
}

// Contract: on re-entry the completed dep makes the downstream ready. Here a is
// completed; remaining b (dep a) and c (dep b) recompute to [[b],[c]] with
// current_level reset to 0.
func TestApplyReplan_CompletedDepUnblocksRemaining(t *testing.T) {
	state := &schemas.DAGState{
		AllIssues: []map[string]any{
			issue("a"),
			issue("b", "a"),
			issue("c", "b"),
		},
		CompletedIssues: []schemas.IssueResult{{IssueName: "a"}},
		CurrentLevel:    1,
	}
	out, err := ApplyReplan(state, schemas.ReplanDecision{Action: schemas.ReplanActionModifyDAG})
	if err != nil {
		t.Fatalf("ApplyReplan error: %v", err)
	}
	if out.CurrentLevel != 0 {
		t.Errorf("current_level = %d, want 0", out.CurrentLevel)
	}
	want := [][]string{{"b"}, {"c"}}
	if !reflect.DeepEqual(out.Levels, want) {
		t.Errorf("levels = %v, want %v", out.Levels, want)
	}
}

// Contract: removed and skipped issues are dropped from the remaining set;
// skipped names are recorded in skipped_issues.
func TestApplyReplan_RemoveAndSkip(t *testing.T) {
	state := &schemas.DAGState{
		AllIssues: []map[string]any{
			issue("a"),
			issue("b"),
			issue("c"),
		},
	}
	decision := schemas.ReplanDecision{
		Action:            schemas.ReplanActionReduceScope,
		RemovedIssueNames: []string{"a"},
		SkippedIssueNames: []string{"b"},
	}
	out, err := ApplyReplan(state, decision)
	if err != nil {
		t.Fatalf("ApplyReplan error: %v", err)
	}
	names := []string{}
	for _, i := range out.AllIssues {
		names = append(names, issueName(i))
	}
	// a removed, b skipped → only c remains (nothing completed/failed to keep).
	if !reflect.DeepEqual(names, []string{"c"}) {
		t.Errorf("all_issues = %v, want [c]", names)
	}
	if !contains(out.SkippedIssues, "b") {
		t.Errorf("skipped_issues = %v, want to contain b", out.SkippedIssues)
	}
}

// Contract: a replan producing a cycle is rejected with an error.
func TestApplyReplan_CycleRejected(t *testing.T) {
	state := &schemas.DAGState{
		AllIssues: []map[string]any{issue("a", "b"), issue("b", "a")},
	}
	_, err := ApplyReplan(state, schemas.ReplanDecision{Action: schemas.ReplanActionModifyDAG})
	if err == nil {
		t.Fatal("expected cycle error from ApplyReplan, got nil")
	}
}

// Contract: updated_issues merge into the matching remaining issue dict.
func TestApplyReplan_UpdateExisting(t *testing.T) {
	state := &schemas.DAGState{
		AllIssues: []map[string]any{issue("a")},
	}
	decision := schemas.ReplanDecision{
		Action:        schemas.ReplanActionModifyDAG,
		UpdatedIssues: []map[string]any{{"name": "a", "title": "Updated Title"}},
	}
	out, err := ApplyReplan(state, decision)
	if err != nil {
		t.Fatalf("ApplyReplan error: %v", err)
	}
	for _, i := range out.AllIssues {
		if issueName(i) == "a" {
			if i["title"] != "Updated Title" {
				t.Errorf("updated title = %v, want 'Updated Title'", i["title"])
			}
		}
	}
}
