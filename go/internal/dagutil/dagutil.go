// Package dagutil holds the pure DAG-manipulation helpers used by the execution
// engine and the planning pipeline. It is a verbatim port of
// swe_af/execution/dag_utils.py plus the pure helpers from
// swe_af/reasoners/pipeline.py (_ensure_paths, _compute_levels,
// _validate_file_conflicts, _assign_sequence_numbers).
//
// Ordering semantics match Python exactly: Python dicts preserve insertion
// order, so this package uses order-tracking slices (never bare Go maps) to
// keep level partitioning, sequence numbering, and conflict reporting
// deterministic and byte-compatible with the Python implementation.
package dagutil

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Small value-extraction helpers (issue dicts are map[string]any after JSON
// unmarshalling, so fields arrive as any and must be coerced).
// ---------------------------------------------------------------------------

// issueName returns the "name" field of an issue dict as a string ("" if
// absent or not a string), mirroring Python's issue["name"] usage.
func issueName(issue map[string]any) string {
	return asString(issue["name"])
}

func asString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// dependsOn returns the "depends_on" list of an issue dict as []string,
// mirroring Python's issue.get("depends_on", []).
func dependsOn(issue map[string]any) []string {
	return asStringSlice(issue["depends_on"])
}

func asStringSlice(v any) []string {
	switch t := v.(type) {
	case []string:
		return t
	case []any:
		out := make([]string, 0, len(t))
		for _, e := range t {
			out = append(out, asString(e))
		}
		return out
	default:
		return nil
	}
}

// asInt coerces a value to an int, treating absent/None/non-numeric as 0.
// JSON numbers arrive as float64; ints/int64 are also accepted. This mirrors
// Python's `x or 0` truthiness where 0/None both become 0.
func asInt(v any) int {
	switch t := v.(type) {
	case int:
		return t
	case int64:
		return int(t)
	case float64:
		return int(t)
	case float32:
		return int(t)
	default:
		return 0
	}
}

// pyListRepr renders a list of strings the way Python's f-string renders a
// list — e.g. ['a', 'b'] — so cycle-error messages are byte-identical.
func pyListRepr(names []string) string {
	out := "["
	for i, n := range names {
		if i > 0 {
			out += ", "
		}
		out += "'" + n + "'"
	}
	out += "]"
	return out
}

// ---------------------------------------------------------------------------
// Level computation (Kahn's algorithm)
// ---------------------------------------------------------------------------

// RecomputeLevels performs a topological sort (Kahn's algorithm) treating
// completed issues as resolved. It is the verbatim port of
// dag_utils.recompute_levels.
//
// remainingIssues are the issue dicts that still need execution (each has
// "name" and "depends_on" keys). completedNames is the set of issue names
// already successfully completed; dependencies on those are treated as
// satisfied. Returns the list of levels (each a list of names that may run
// concurrently), or an error if the remaining issues contain a cycle.
func RecomputeLevels(remainingIssues []map[string]any, completedNames map[string]bool) ([][]string, error) {
	nameSet := make(map[string]bool, len(remainingIssues))
	order := make([]string, 0, len(remainingIssues))
	inDegree := make(map[string]int, len(remainingIssues))
	for _, issue := range remainingIssues {
		name := issueName(issue)
		nameSet[name] = true
		if _, seen := inDegree[name]; !seen {
			order = append(order, name)
		}
		inDegree[name] = 0
	}

	dependents := make(map[string][]string)
	for _, issue := range remainingIssues {
		name := issueName(issue)
		for _, dep := range dependsOn(issue) {
			// Only count deps that are in the remaining set (not completed).
			if nameSet[dep] && !completedNames[dep] {
				inDegree[name]++
				dependents[dep] = append(dependents[dep], name)
			}
		}
	}

	return kahn(order, inDegree, dependents, len(remainingIssues))
}

// ComputeLevels is the verbatim port of pipeline._compute_levels: a
// topological sort of issues into parallel execution levels, with no notion of
// completed issues (all in-set dependencies count). Raises an error on cycles.
func ComputeLevels(issues []map[string]any) ([][]string, error) {
	nameSet := make(map[string]bool, len(issues))
	order := make([]string, 0, len(issues))
	inDegree := make(map[string]int, len(issues))
	for _, issue := range issues {
		name := issueName(issue)
		nameSet[name] = true
		if _, seen := inDegree[name]; !seen {
			order = append(order, name)
		}
		inDegree[name] = 0
	}

	dependents := make(map[string][]string)
	for _, issue := range issues {
		name := issueName(issue)
		for _, dep := range dependsOn(issue) {
			if nameSet[dep] {
				inDegree[name]++
				dependents[dep] = append(dependents[dep], name)
			}
		}
	}

	return kahn(order, inDegree, dependents, len(issues))
}

// kahn runs the shared BFS level-partitioning loop. order is the insertion
// order of node names (so the initial zero-in-degree queue matches Python's
// dict-order iteration), inDegree is the mutable in-degree map, dependents maps
// a node to nodes that depend on it, and total is the expected processed count.
func kahn(order []string, inDegree map[string]int, dependents map[string][]string, total int) ([][]string, error) {
	queue := make([]string, 0, len(order))
	for _, name := range order {
		if inDegree[name] == 0 {
			queue = append(queue, name)
		}
	}

	levels := [][]string{}
	processed := 0
	for len(queue) > 0 {
		level := queue
		levels = append(levels, level)
		processed += len(level)
		next := []string{}
		for _, name := range level {
			for _, depName := range dependents[name] {
				inDegree[depName]--
				if inDegree[depName] == 0 {
					next = append(next, depName)
				}
			}
		}
		queue = next
	}

	if processed != total {
		cycleNodes := []string{}
		for _, name := range order {
			if inDegree[name] > 0 {
				cycleNodes = append(cycleNodes, name)
			}
		}
		return nil, fmt.Errorf("Dependency cycle detected among issues: %s", pyListRepr(cycleNodes))
	}

	return levels, nil
}

// ---------------------------------------------------------------------------
// Downstream discovery
// ---------------------------------------------------------------------------

// FindDownstream returns the set of issue names that directly or indirectly
// depend on issueName. It does NOT include issueName itself. Verbatim port of
// dag_utils.find_downstream.
func FindDownstream(issueName string, allIssues []map[string]any) map[string]bool {
	// Build adjacency: issue -> list of issues that depend on it.
	dependents := make(map[string][]string)
	for _, issue := range allIssues {
		name := asString(issue["name"])
		for _, dep := range dependsOn(issue) {
			dependents[dep] = append(dependents[dep], name)
		}
	}

	// BFS from issueName.
	visited := make(map[string]bool)
	queue := append([]string{}, dependents[issueName]...)
	for len(queue) > 0 {
		name := queue[0]
		queue = queue[1:]
		if visited[name] {
			continue
		}
		visited[name] = true
		queue = append(queue, dependents[name]...)
	}

	return visited
}

// ---------------------------------------------------------------------------
// File-conflict validation
// ---------------------------------------------------------------------------

// ValidateFileConflicts detects file conflicts between issues scheduled at the
// same parallel level. For each level it collects files_to_create and
// files_to_modify across all issues; a file touched by more than one issue at
// the same level is reported as a conflict. Verbatim port of
// pipeline._validate_file_conflicts.
//
// Returns a list of conflict dicts, e.g.
// {"level": 0, "file": "src/ops.rs", "issues": ["arithmetic-ops", "logical-ops"]}.
// An empty (non-nil) slice means no conflicts.
func ValidateFileConflicts(issues []map[string]any, levels [][]string) []map[string]any {
	issueByName := make(map[string]map[string]any, len(issues))
	for _, issue := range issues {
		issueByName[issueName(issue)] = issue
	}

	conflicts := []map[string]any{}
	for levelIdx, levelNames := range levels {
		fileToIssues := make(map[string][]string)
		fileOrder := []string{}
		add := func(f, name string) {
			if _, seen := fileToIssues[f]; !seen {
				fileOrder = append(fileOrder, f)
			}
			fileToIssues[f] = append(fileToIssues[f], name)
		}
		for _, name := range levelNames {
			issue, ok := issueByName[name]
			if !ok {
				continue
			}
			for _, f := range asStringSlice(issue["files_to_create"]) {
				add(f, name)
			}
			for _, f := range asStringSlice(issue["files_to_modify"]) {
				add(f, name)
			}
		}

		for _, filepath := range fileOrder {
			touching := fileToIssues[filepath]
			if len(touching) > 1 {
				conflicts = append(conflicts, map[string]any{
					"level":  levelIdx,
					"file":   filepath,
					"issues": touching,
				})
			}
		}
	}

	return conflicts
}

// ---------------------------------------------------------------------------
// Sequence numbering
// ---------------------------------------------------------------------------

// AssignSequenceNumbers assigns 1-based sequential numbers based on
// topo-sorted level order, preserving the sprint planner's ordering within each
// level. Mutates each issue dict's "sequence_number" in place and returns the
// issues in their original insertion order. Verbatim port of
// pipeline._assign_sequence_numbers.
func AssignSequenceNumbers(issues []map[string]any, levels [][]string) []map[string]any {
	issueByName := make(map[string]map[string]any, len(issues))
	valueOrder := []string{}
	for _, issue := range issues {
		name := issueName(issue)
		if _, seen := issueByName[name]; !seen {
			valueOrder = append(valueOrder, name)
		}
		issueByName[name] = issue
	}

	counter := 1
	for _, levelNames := range levels {
		levelSet := make(map[string]bool, len(levelNames))
		for _, n := range levelNames {
			levelSet[n] = true
		}
		// Preserve sprint planner's ordering within each level.
		for _, issue := range issues {
			if levelSet[issueName(issue)] {
				issueByName[issueName(issue)]["sequence_number"] = counter
				counter++
			}
		}
	}

	out := make([]map[string]any, 0, len(valueOrder))
	for _, name := range valueOrder {
		out = append(out, issueByName[name])
	}
	return out
}

// ---------------------------------------------------------------------------
// Artifact path scaffolding
// ---------------------------------------------------------------------------

// EnsurePaths creates the artifact directories under base and returns a path
// map. Verbatim port of pipeline._ensure_paths.
func EnsurePaths(base string) (map[string]string, error) {
	paths := map[string]string{
		"base":         base,
		"logs":         filepath.Join(base, "logs"),
		"plan":         filepath.Join(base, "plan"),
		"issues":       filepath.Join(base, "plan", "issues"),
		"prd":          filepath.Join(base, "plan", "prd.md"),
		"architecture": filepath.Join(base, "plan", "architecture.md"),
		"review":       filepath.Join(base, "plan", "review.md"),
		"rationale":    filepath.Join(base, "rationale.md"),
	}
	for _, d := range []string{"logs", "plan", "issues"} {
		if err := os.MkdirAll(paths[d], 0o755); err != nil {
			return nil, err
		}
	}
	return paths, nil
}

// ---------------------------------------------------------------------------
// Replan application
// ---------------------------------------------------------------------------

// ApplyReplan applies a replan decision to the DAG state, removing, modifying,
// and adding issues as directed, then recomputing execution levels for the
// remaining work and resetting current_level to 0. Verbatim port of
// dag_utils.apply_replan. Returns the (mutated) state, or an error if the
// resulting DAG contains a cycle (the replan is rejected).
func ApplyReplan(state *schemas.DAGState, decision schemas.ReplanDecision) (*schemas.DAGState, error) {
	if decision.Action == schemas.ReplanActionAbort {
		state.ReplanCount++
		state.ReplanHistory = append(state.ReplanHistory, decision)
		return state, nil
	}
	if decision.Action == schemas.ReplanActionContinue {
		state.ReplanCount++
		state.ReplanHistory = append(state.ReplanHistory, decision)
		return state, nil
	}

	completedNames := make(map[string]bool, len(state.CompletedIssues))
	for _, r := range state.CompletedIssues {
		completedNames[r.IssueName] = true
	}
	failedNames := make(map[string]bool, len(state.FailedIssues))
	for _, r := range state.FailedIssues {
		failedNames[r.IssueName] = true
	}

	// Build a working copy of issues (exclude completed and failed),
	// preserving insertion order via remainingOrder.
	remainingByName := make(map[string]map[string]any)
	remainingOrder := []string{}
	addRemaining := func(name string, issue map[string]any) {
		if _, seen := remainingByName[name]; !seen {
			remainingOrder = append(remainingOrder, name)
		}
		remainingByName[name] = issue
	}
	removeRemaining := func(name string) {
		if _, seen := remainingByName[name]; !seen {
			return
		}
		delete(remainingByName, name)
		for i, n := range remainingOrder {
			if n == name {
				remainingOrder = append(remainingOrder[:i], remainingOrder[i+1:]...)
				break
			}
		}
	}

	for _, issue := range state.AllIssues {
		name := issueName(issue)
		if !completedNames[name] && !failedNames[name] {
			addRemaining(name, copyMap(issue))
		}
	}

	// 1. Remove issues.
	for _, name := range decision.RemovedIssueNames {
		removeRemaining(name)
	}

	// 2. Skip issues (mark as skipped, remove from remaining).
	for _, name := range decision.SkippedIssueNames {
		removeRemaining(name)
		if !contains(state.SkippedIssues, name) {
			state.SkippedIssues = append(state.SkippedIssues, name)
		}
	}

	// 3. Update existing issues.
	for _, updated := range decision.UpdatedIssues {
		name := asString(updated["name"])
		if existing, ok := remainingByName[name]; ok {
			for k, v := range updated {
				existing[k] = v
			}
		}
	}

	// 4. Add new issues (with next-available sequence numbers).
	// Build target_repo lookup from all existing issues for inheritance.
	targetRepoByName := make(map[string]string)
	for _, issue := range state.AllIssues {
		if tr := asString(issue["target_repo"]); tr != "" {
			targetRepoByName[issueName(issue)] = tr
		}
	}

	maxSeq := 0
	for _, issue := range state.AllIssues {
		if s := asInt(issue["sequence_number"]); s > maxSeq {
			maxSeq = s
		}
	}
	for _, newIssue := range decision.NewIssues {
		name := asString(newIssue["name"])
		if name != "" {
			if _, exists := remainingByName[name]; !exists {
				if asInt(newIssue["sequence_number"]) == 0 {
					maxSeq++
					newIssue["sequence_number"] = maxSeq
				}
				// Inherit target_repo from dependencies if not explicitly set.
				if asString(newIssue["target_repo"]) == "" && len(state.WorkspaceManifest) > 0 {
					for _, dep := range dependsOn(newIssue) {
						if inherited := targetRepoByName[dep]; inherited != "" {
							newIssue["target_repo"] = inherited
							break
						}
					}
				}
				addRemaining(name, newIssue)
			}
		}
	}

	remaining := make([]map[string]any, 0, len(remainingOrder))
	for _, name := range remainingOrder {
		remaining = append(remaining, remainingByName[name])
	}

	// Recompute levels (returns an error on cycle).
	newLevels, err := RecomputeLevels(remaining, completedNames)
	if err != nil {
		return nil, err
	}

	// Update DAG state: keep completed/failed issues, then the remaining set.
	kept := []map[string]any{}
	for _, issue := range state.AllIssues {
		name := issueName(issue)
		if completedNames[name] || failedNames[name] {
			kept = append(kept, issue)
		}
	}
	state.AllIssues = append(kept, remaining...)
	state.Levels = newLevels
	state.CurrentLevel = 0 // reset to start of recomputed levels
	state.ReplanCount++
	state.ReplanHistory = append(state.ReplanHistory, decision)

	return state, nil
}

func copyMap(m map[string]any) map[string]any {
	c := make(map[string]any, len(m))
	for k, v := range m {
		c[k] = v
	}
	return c
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}
