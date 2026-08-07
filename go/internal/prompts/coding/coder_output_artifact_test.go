package coding

import (
	"strings"
	"testing"
)

// TestCoderTaskPromptRequiresOutputArtifactContract is a regression test for
// the SWE-AF/OpenCode output-contract fix.
//
// Background
//
// At SWE-AF commit 4237bebe7b63, the CoderTaskPrompt ended with the
// instruction "Report codebase_learnings and agent_retro in your output."
// The coding runtime (opencode/Claude Code/Codex) interpreted "in your
// output" as the chat response, never writing a file to disk. The harness
// in agentfield/sdk-go/harness/schema.go:541 then reported
//
//   "The output file was NOT created."
//
// because .agentfield_output.json was not present at the harness's expected
// path (OutputPath = <worktree>/.agentfield_output.json). The coder call
// failed with this error every time, for every model, on every worktree.
//
// Fix
//
// Step 6 of the "Your Task" section in CoderTaskPrompt now contains an
// explicit MANDATORY instruction to write .agentfield_output.json in the
// current working directory, with valid JSON conforming to the
// CoderResult schema. This test fails on the upstream behavior and passes
// with the patch.
//
// This test does NOT need a regenerated golden fixture. It is content-based
// and verifies the structural contract that the coding runtime depends on.
func TestCoderTaskPromptRequiresOutputArtifactContract(t *testing.T) {
	prompt := CoderTaskPrompt(CoderTaskPromptOpts{
		Issue: map[string]any{
			"name":                "fix-add-bug",
			"title":               "Fix the add() function",
			"description":         "Change return a - b to return a + b",
			"acceptance_criteria": []any{"add(2, 3) == 5"},
		},
		WorktreePath: "/workspaces/coder-test-branch",
		Iteration:    1,
	})

	// Required: the exact filename the harness looks for.
	const requiredFilename = ".agentfield_output.json"
	if !strings.Contains(prompt, requiredFilename) {
		t.Errorf("CoderTaskPrompt must mention %q so the coding runtime knows to write the output artifact. Got prompt:\n%s",
			requiredFilename, prompt)
	}

	// Required: the instruction must be MANDATORY (the coding runtime
	// must not treat it as optional). Without the MANDATORY framing, the
	// runtime may deprioritize the artifact write in favor of code edits.
	requiredFraming := []string{"MANDATORY", "current working directory"}
	for _, kw := range requiredFraming {
		if !strings.Contains(prompt, kw) {
			t.Errorf("CoderTaskPrompt must include the keyword %q to frame the artifact write as a hard contract. Got prompt:\n%s",
				kw, prompt)
		}
	}

	// Required: the schema field names the harness validates against
	// (per the CoderResult struct in internal/schemas/execution.go:375).
	requiredSchemaFields := []string{
		"summary",
		"files_changed",
		"complete",
		"test_results",
		"codebase_learnings",
		"agent_retro",
	}
	for _, f := range requiredSchemaFields {
		if !strings.Contains(prompt, f) {
			t.Errorf("CoderTaskPrompt must list the CoderResult schema field %q so the runtime writes a valid result. Got prompt:\n%s",
				f, prompt)
		}
	}

	// Required: the prompt must mention the harness's specific failure
	// message so the runtime can recognize and avoid it. The phrase
	// "output file" + "NOT created" together signal the failure mode.
	if !strings.Contains(prompt, "output file") || !strings.Contains(prompt, "NOT created") {
		t.Errorf("CoderTaskPrompt must reference the harness failure mode (output file ... NOT created) so the runtime can avoid it. Got prompt:\n%s",
			prompt)
	}
}

// TestCoderTaskPromptOutputArtifactNotSilentlyRemovable protects against
// regressions where someone reverts the MANDATORY framing. The fix is
// minimal and surgical; this test ensures the contract is loud, not
// implied.
func TestCoderTaskPromptOutputArtifactNotSilentlyRemovable(t *testing.T) {
	prompt := CoderTaskPrompt(CoderTaskPromptOpts{
		Issue:        map[string]any{"name": "x"},
		WorktreePath: "/tmp",
		Iteration:    1,
	})

	// The MANDATORY word must appear immediately before the artifact
	// instruction. If a future edit removes MANDATORY or moves it
	// elsewhere, the test fails.
	const mandatoryPrefix = "6. MANDATORY"
	if !strings.HasPrefix(prompt[strings.Index(prompt, "## Your Task"):], mandatoryPrefix) &&
		!strings.Contains(prompt[strings.Index(prompt, "## Your Task"):], "6. MANDATORY") {
		t.Errorf("CoderTaskPrompt must begin step 6 of 'Your Task' with %q. Got:\n%s",
			mandatoryPrefix, prompt[strings.Index(prompt, "## Your Task"):])
	}
}
