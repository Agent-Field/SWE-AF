package schemas

// This file makes the invopop-reflected JSON schemas faithful to the Pydantic
// models they port. The AgentField Go SDK now validates every harness output
// against the reflected schema (harness/schema.go validateAgainstSchema), so an
// UN-faithful schema OVER-rejects valid output. invopop's raw reflection differs
// from pydantic in three ways this file corrects:
//
//  1. required: invopop marks EVERY field required (there is no `,omitempty` on
//     these structs — pydantic model_dump emits every key). Pydantic requires
//     only fields WITHOUT a default. pydanticRequired holds the authoritative
//     no-default set per model (derived from pydantic model_fields.is_required()).
//  2. additionalProperties: invopop emits `false`; pydantic's default IGNORES
//     extra keys, so extras must not fail validation.
//  3. nullable: Optional[...] fields (Go pointers / maps) serialise as null when
//     unset (Go has no omitempty here), but invopop emits the non-null subschema,
//     so `null` fails. pydanticNullable holds the fields that must also accept
//     null (X | None in pydantic).
//
// Enums are handled separately, via JSONSchemaExtend methods on the enum types
// (enums.go).
//
// The tables are keyed by the Go struct name, which is exactly how invopop names
// entries in `$defs` and how schemaFor identifies the root type. They are
// cross-checked against defaults.go by pydantic_faithful_test.go.

// pydanticRequired maps a Go schema struct name to the JSON names of its fields
// that have NO pydantic default (i.e. the pydantic `required` set). A struct
// absent from this map has its invopop `required` cleared entirely (permissive —
// never over-rejects), so the map only needs to enumerate the models that carry
// genuinely-required fields.
var pydanticRequired = map[string][]string{
	// execution.go
	"RepoSpec":              {"role"},
	"WorkspaceRepo":         {"repo_name", "repo_url", "role", "absolute_path", "branch"},
	"WorkspaceManifest":     {"workspace_root", "repos", "primary_repo_name"},
	"RepoPRResult":          {"repo_name", "repo_url", "success"},
	"IssueAdaptation":       {"adaptation_type"},
	"SplitIssueSpec":        {"name", "title", "description", "acceptance_criteria"},
	"IssueAdvisorDecision":  {"action", "failure_diagnosis", "rationale"},
	"IssueResult":           {"issue_name", "outcome"},
	"LevelResult":           {"level_index"},
	"ReplanDecision":        {"action", "rationale"},
	"DAGState":              {},
	"GitInitResult":         {"mode", "original_branch", "integration_branch", "initial_commit_sha", "success"},
	"WorkspaceInfo":         {"issue_name", "branch_name", "worktree_path"},
	"MergeResult":           {"success", "merged_branches", "failed_branches", "needs_integration_test", "summary"},
	"IntegrationTestResult": {"passed", "tests_run", "tests_passed", "tests_failed", "summary"},
	"RetryAdvice":           {"should_retry", "diagnosis", "strategy", "modified_context"},
	"CriterionResult":       {"criterion", "passed", "evidence"},
	"VerificationResult":    {"passed", "criteria_results", "summary"},
	"CoderResult":           {},
	"QAResult":              {"passed"},
	"CodeReviewResult":      {"approved"},
	"QASynthesisResult":     {"action"},
	"BuildResult":           {"plan_result", "dag_state", "success", "summary"},
	"RepoFinalizeResult":    {"success"},
	"GitHubPRResult":        {"success"},
	"CIFailedCheck":         {"name"},
	"CIWatchResult":         {"status", "pr_number"},
	"CIFixResult":           {"fixed"},
	"ReviewCommentRef":      {},
	"AddressedComment":      {"addressed"},
	"PRResolveResult":       {"fixed"},
	// planning.go (reasoners/schemas.py)
	"PRD":                   {"validated_description", "acceptance_criteria", "must_have", "nice_to_have", "out_of_scope"},
	"ArchitectureComponent": {"name", "responsibility"},
	"ArchitectureDecision":  {"decision", "rationale"},
	"Architecture":          {"summary", "components", "interfaces", "decisions", "file_changes_overview"},
	"ReviewResult":          {"approved", "feedback", "summary"},
	"IssueGuidance":         {},
	"PlannedIssue":          {"name", "title", "description", "acceptance_criteria"},
	"PlanResult":            {"prd", "architecture", "review", "issues", "levels", "artifacts_dir", "rationale"},
	// fast.go (fast/schemas.py)
	"FastTask":               {"name", "title", "description", "acceptance_criteria"},
	"FastPlanResult":         {"tasks"},
	"FastTaskResult":         {"task_name", "outcome"},
	"FastExecutionResult":    {"task_results", "completed_count", "failed_count"},
	"FastVerificationResult": {"passed"},
	"FastBuildResult":        {"plan_result", "execution_result", "success", "summary"},
	// askuser.go / scout.go / services.go (hitl)
	"AskUserForm":           {"title", "fields"},
	"AskUserFormField":      {"id", "type", "label"},
	"AskUserResponse":       {"status"},
	"ScoutResult":           {},
	"ServiceCredentialSpec": {"service_name", "env_var_name", "mint_url", "permissions_hint"},
}

// pydanticNullable maps a Go schema struct name to the JSON names of its
// Optional[...] fields (X | None in pydantic) whose subschema must also accept
// null — Go serialises an unset pointer/map as null and pydantic accepts it.
var pydanticNullable = map[string][]string{
	"IssueAdvisorDecision": {"ask_user_form"},
	"CoderResult":          {"tests_passed"},
	"IssueResult":          {"split_request"},
	"ReplanDecision":       {"ask_user_form"},
	"DAGState":             {"workspace_manifest"},
	"WorkspaceRepo":        {"git_init_result"},
	"BuildResult":          {"verification"},
	"PRD":                  {"ask_user_form"},
	"ScoutResult":          {"ask_user_form"},
	"PlannedIssue":         {"sequence_number", "guidance"},
	"FastBuildResult":      {"verification"},
	"AskUserForm":          {"description"},
	"AskUserFormField":     {"description", "placeholder", "options", "min", "max", "step"},
	"AskUserResponse":      {"feedback", "error"},
}

// MakePydanticFaithful post-processes an invopop-reflected schema map (root +
// $defs) so it validates exactly what the source Pydantic model accepts. It
// mutates and returns schema. rootName is the Go struct name of the root type.
func MakePydanticFaithful(schema map[string]any, rootName string) map[string]any {
	if schema == nil {
		return schema
	}
	applyObjectFaithfulness(schema, rootName)
	if defs, ok := schema["$defs"].(map[string]any); ok {
		for name, def := range defs {
			if dm, ok := def.(map[string]any); ok {
				applyObjectFaithfulness(dm, name)
			}
		}
	}
	return schema
}

// applyObjectFaithfulness fixes one object schema: relaxes additionalProperties,
// sets the pydantic `required` set, and makes nullable fields accept null.
func applyObjectFaithfulness(obj map[string]any, typeName string) {
	// (2) additionalProperties: relax an explicit `false` (pydantic ignores
	// extras). Leave a schema value (e.g. map[string]string field) untouched.
	if v, ok := obj["additionalProperties"]; ok {
		if b, isBool := v.(bool); isBool && !b {
			delete(obj, "additionalProperties")
		}
	}

	// (1) required: replace invopop's all-fields list with the pydantic no-default
	// set. Unknown types have required cleared entirely (never over-rejects).
	if req, ok := pydanticRequired[typeName]; ok {
		if len(req) == 0 {
			delete(obj, "required")
		} else {
			arr := make([]any, len(req))
			for i, f := range req {
				arr[i] = f
			}
			obj["required"] = arr
		}
	} else {
		delete(obj, "required")
	}

	props, hasProps := obj["properties"].(map[string]any)

	// (3a) nullable: make each explicitly-Optional field's subschema accept null.
	// This covers the non-map Optionals (ask_user_form/$ref, tests_passed/bool,
	// split_request/array, sequence_number/int, min|max|step/number, ...).
	if nulls, ok := pydanticNullable[typeName]; ok && hasProps {
		for _, fn := range nulls {
			if sub, ok := props[fn].(map[string]any); ok {
				props[fn] = makeNullable(sub)
			}
		}
	}

	// (3b) nullable maps: a Go map[...] field (both pydantic `dict` and
	// `dict | None`) serialises as null when unset, but invopop emits a non-null
	// `{"type":"object"}`. Go cannot distinguish the two at the type level (both
	// are map[string]any), so every map-typed property is made to accept null.
	// This is the minimal relaxation that never over-rejects — a real object
	// still validates, a wrong scalar type still fails.
	if hasProps {
		for fn, sub := range props {
			if sm, ok := sub.(map[string]any); ok && isMapSchema(sm) {
				props[fn] = makeNullable(sm)
			}
		}
	}
}

// isMapSchema reports whether a property subschema came from a Go map field. A
// map[...] reflects to {"type":"object"} (optionally with additionalProperties),
// whereas a nested struct reflects to a {"$ref":...} and the root/inline object
// carries "properties". So an object schema WITHOUT "properties" is a map.
func isMapSchema(sm map[string]any) bool {
	if t, ok := sm["type"].(string); ok && t == "object" {
		_, hasProps := sm["properties"]
		return !hasProps
	}
	return false
}

// makeNullable returns a subschema equivalent to sub but that also accepts a
// JSON null. A plain `type` gains "null"; a `$ref` (or anything else) is wrapped
// in an anyOf with a null branch. It is idempotent.
func makeNullable(sub map[string]any) map[string]any {
	if t, ok := sub["type"]; ok {
		switch tv := t.(type) {
		case string:
			if tv == "null" {
				return sub
			}
			sub["type"] = []any{tv, "null"}
			return sub
		case []any:
			for _, e := range tv {
				if e == "null" {
					return sub
				}
			}
			sub["type"] = append(tv, "null")
			return sub
		}
	}
	if _, hasAnyOf := sub["anyOf"]; hasAnyOf {
		return sub
	}
	return map[string]any{"anyOf": []any{sub, map[string]any{"type": "null"}}}
}
