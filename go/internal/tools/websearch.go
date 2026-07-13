// Package tools ports swe_af/tools — helpers for steering reasoner behavior
// when opencode's web search is enabled.
//
// Opencode ships built-in websearch and webfetch tools that activate when
// OPENCODE_ENABLE_EXA=1 and EXA_API_KEY are set in the deployment env. The
// opencode subprocess inherits parent env via agentfield's run_cli, so no
// SWE-AF wiring is required to enable web search — set the env vars in your
// deploy and reasoners running through opencode automatically gain the
// capability.
//
// Tool awareness is also handled for free: opencode advertises its built-in
// tools to the LLM via the standard tool-definition layer, so the model knows
// websearch exists without any system-prompt boilerplate from us.
//
// What lives here is the one place a prompt-level addition genuinely earns its
// keep: the coder reasoner. run_coder runs many turns inside a single coding
// loop, and an unrestrained model will happily spend turns searching for
// things it could read from the codebase. The WebSearchCoderGuardrail snippet
// narrows the agent's discretion to the cases where external lookup is
// actually load-bearing.
package tools

import "os"

// WebSearchCoderGuardrail is the coder-specific web-search restraint snippet.
// It is byte-identical to the Python WEB_SEARCH_CODER_GUARDRAIL constant.
const WebSearchCoderGuardrail = "\n" +
	"\n" +
	"## When to use the web_search / web_fetch tools\n" +
	"\n" +
	"You may have access to a `websearch` (and possibly `webfetch`) tool. Use it sparingly. Reach for it ONLY when:\n" +
	"\n" +
	"- You encounter an unfamiliar API and cannot understand its behavior from the existing codebase\n" +
	"- An error message is opaque and the codebase + test output don't explain it\n" +
	"- You need to verify library/framework version compatibility for a specific call\n" +
	"- You need to check whether a function or pattern is deprecated\n" +
	"\n" +
	"Do NOT use websearch for:\n" +
	"- General programming knowledge or design patterns\n" +
	"- Anything answerable by reading existing files in the repo\n" +
	"- Style or convention questions — follow what the codebase already does\n" +
	"\n" +
	"Default to writing code. Reach for websearch only when a concrete blocker requires external context, then return immediately to the implementation.\n"

// websearchEnabled reports whether opencode's built-in websearch is gated open
// in the deploy env (OPENCODE_ENABLE_EXA=1 and a non-empty EXA_API_KEY).
func websearchEnabled() bool {
	return os.Getenv("OPENCODE_ENABLE_EXA") == "1" && os.Getenv("EXA_API_KEY") != ""
}

// MaybeApplyCoderGuardrail appends the coder-specific web-search guardrail when
// web search is enabled. The guardrail is appended only when
// OPENCODE_ENABLE_EXA=1 and EXA_API_KEY are set, so the model isn't told about
// a tool it can't use. When web search isn't enabled, returns systemPrompt
// unchanged.
func MaybeApplyCoderGuardrail(systemPrompt string) string {
	if !websearchEnabled() {
		return systemPrompt
	}
	return systemPrompt + WebSearchCoderGuardrail
}
