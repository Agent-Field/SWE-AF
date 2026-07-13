package coding

import "fmt"

// This file ports the shared-memory helpers from swe_af/execution/coding_loop.py
// (lines 88-258): the memory read/write conventions that propagate codebase
// conventions, interface registries, failure/bug patterns and build health
// across issues when learning is enabled.
//
// The memory seam is nil when learning is disabled, in which case every helper
// is a no-op — mirroring the Python `memory_fn is None` guard.

// memoryGet ports _memory_get: read from shared memory, or nil if unavailable.
func memoryGet(memoryFn MemoryFn, key string) any {
	if memoryFn == nil {
		return nil
	}
	return memoryFn("get", key, nil)
}

// memorySet ports _memory_set: write to shared memory, silently skip if
// unavailable.
func memorySet(memoryFn MemoryFn, key string, value any) {
	if memoryFn == nil {
		return
	}
	memoryFn("set", key, value)
}

// readMemoryContext ports _read_memory_context: gather relevant shared memory
// for injection into agent prompts. Returns an empty map when memory is nil.
func readMemoryContext(memoryFn MemoryFn, issue map[string]any) map[string]any {
	if memoryFn == nil {
		return map[string]any{}
	}

	context := map[string]any{}

	if conventions := memoryGet(memoryFn, "codebase_conventions"); isTruthy(conventions) {
		context["codebase_conventions"] = conventions
	}
	if failurePatterns := memoryGet(memoryFn, "failure_patterns"); isTruthy(failurePatterns) {
		context["failure_patterns"] = failurePatterns
	}
	if bugPatterns := memoryGet(memoryFn, "bug_patterns"); isTruthy(bugPatterns) {
		context["bug_patterns"] = bugPatterns
	}

	// Read interfaces from completed dependencies.
	var depInterfaces []any
	for _, depName := range toStringSlice(issue["depends_on"]) {
		iface := memoryGet(memoryFn, "interfaces/"+depName)
		if !isTruthy(iface) {
			continue
		}
		if m, ok := iface.(map[string]any); ok {
			// {**iface, "issue": dep_name}
			merged := make(map[string]any, len(m)+1)
			for k, v := range m {
				merged[k] = v
			}
			merged["issue"] = depName
			depInterfaces = append(depInterfaces, merged)
		} else {
			depInterfaces = append(depInterfaces, iface)
		}
	}
	if len(depInterfaces) > 0 {
		context["dependency_interfaces"] = depInterfaces
	}

	return context
}

// writeMemoryOnApprove ports _write_memory_on_approve: record conventions,
// interface registry, agent retro and build health after a successful issue.
func writeMemoryOnApprove(memoryFn MemoryFn, issue, coderResult map[string]any, isFirstSuccess bool, note NoteFn) {
	if memoryFn == nil {
		return
	}

	issueName := mapGetStr(issue, "name", "unknown")

	// 3A: Codebase conventions — written by the first successful coder.
	if isFirstSuccess {
		learnings := toStringSlice(coderResult["codebase_learnings"])
		if len(learnings) > 0 {
			conventions := map[string]any{}
			for _, learning := range learnings {
				conventions[fmt.Sprintf("note_%d", len(conventions))] = learning
			}
			memorySet(memoryFn, "codebase_conventions", conventions)
			if note != nil {
				note(
					fmt.Sprintf("Memory: wrote codebase_conventions from %s", issueName),
					[]string{"memory", "conventions"},
				)
			}
		}
	}

	// 3C: Interface registry.
	iface := map[string]any{
		"module":        issueName,
		"exports":       mapGetOr(issue, "provides", []any{}),
		"files_created": toStringSlice(coderResult["files_changed"]),
		"tests_passing": coderResult["tests_passed"], // .get("tests_passed", None)
		"summary":       mapGetStr(coderResult, "summary", ""),
	}
	memorySet(memoryFn, "interfaces/"+issueName, iface)

	// 3E: Agent retro.
	if retro := coderResult["agent_retro"]; isTruthy(retro) {
		memorySet(memoryFn, "retros/"+issueName, retro)
	}

	// 3F: Build health — accumulate.
	healthAny := memoryGet(memoryFn, "build_health")
	health := asMap(healthAny)
	if health == nil || !isTruthy(healthAny) {
		health = defaultBuildHealth()
	}
	health["issues_completed"] = toInt(health["issues_completed"]) + 1
	modulesPassing := toStringSlice(health["modules_passing"])
	if !contains(modulesPassing, issueName) {
		health["modules_passing"] = append(modulesPassing, issueName)
	}
	memorySet(memoryFn, "build_health", health)
}

// writeMemoryOnFailure ports _write_memory_on_failure: record failure patterns,
// bug patterns from reviewer debt items and build health after a failed
// iteration.
func writeMemoryOnFailure(memoryFn MemoryFn, issue map[string]any, feedbackSummary string, reviewResult map[string]any, note NoteFn) {
	if memoryFn == nil {
		return
	}

	issueName := mapGetStr(issue, "name", "unknown")

	// 3B: Failure pattern feed-forward.
	patternsAny := memoryGet(memoryFn, "failure_patterns")
	patterns := toAnySlice(patternsAny)
	if !isTruthy(patternsAny) {
		patterns = []any{}
	}
	patterns = append(patterns, map[string]any{
		"issue":       issueName,
		"pattern":     "iteration_failure",
		"description": truncate(feedbackSummary, 200),
	})
	memorySet(memoryFn, "failure_patterns", lastN(patterns, 10)) // keep last 10

	// 3D: Bug patterns — extract from reviewer debt items.
	if isTruthy(reviewResult) {
		debtItems := toMapSlice(reviewResult["debt_items"])
		if len(debtItems) > 0 {
			bugPatternsAny := memoryGet(memoryFn, "bug_patterns")
			bugPatterns := toAnySlice(bugPatternsAny)
			if !isTruthy(bugPatternsAny) {
				bugPatterns = []any{}
			}
			for _, d := range debtItems {
				// d.get("title", d.get("type", "unknown"))
				var bugType string
				if v, ok := d["title"]; ok {
					bugType = anyToStr(v)
				} else if v, ok := d["type"]; ok {
					bugType = anyToStr(v)
				} else {
					bugType = "unknown"
				}
				// Check if pattern already exists.
				var existing map[string]any
				for _, bp := range bugPatterns {
					if m, ok := bp.(map[string]any); ok && mapGetStr(m, "type", "") == bugType {
						existing = m
						break
					}
				}
				if existing != nil {
					existing["frequency"] = toIntDefault(existing["frequency"], 1) + 1
				} else {
					bugPatterns = append(bugPatterns, map[string]any{
						"type":      bugType,
						"frequency": 1,
						"modules":   []any{issueName},
					})
				}
			}
			memorySet(memoryFn, "bug_patterns", lastN(bugPatterns, 20))
		}
	}

	// 3F: Build health — track failure.
	healthAny := memoryGet(memoryFn, "build_health")
	health := asMap(healthAny)
	if health == nil || !isTruthy(healthAny) {
		health = defaultBuildHealth()
	}
	health["issues_failed"] = toInt(health["issues_failed"]) + 1
	modulesFailing := toStringSlice(health["modules_failing"])
	if !contains(modulesFailing, issueName) {
		health["modules_failing"] = append(modulesFailing, issueName)
	}
	memorySet(memoryFn, "build_health", health)
}

// defaultBuildHealth is the seed dict used by `... or { ... }` in the Python
// build-health accumulators.
func defaultBuildHealth() map[string]any {
	return map[string]any{
		"modules_passing":      []any{},
		"modules_failing":      []any{},
		"total_tests_reported": 0,
		"known_risks":          []any{},
		"issues_completed":     0,
		"issues_failed":        0,
		"debt_items":           []any{},
	}
}
