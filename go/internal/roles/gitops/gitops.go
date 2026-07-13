// Package gitops holds the git/workspace role reasoners of the SWE-AF port:
// git_init, workspace_setup, workspace_cleanup, merger, integration_tester,
// repo_finalize, and github_pr. Each is a 1:1 port of the corresponding
// @router.reasoner in swe_af/reasoners/execution_agents.py (design §4.1, §8).
//
// Every handler has the same shape:
//
//	func(ctx context.Context, deps *Deps, input map[string]any) (any, error)
//
// It binds its typed input (param names/defaults byte-identical to the Python
// signature), builds the role's task prompt, invokes the harness through the
// single choke point harnessx.Run[T] (which injects run-scoped credentials and
// classifies fatal API errors), emits the same note() messages/tags verbatim,
// propagates *fatal.FatalHarnessError, and on a harness parse failure returns
// the role's deterministic fallback (never an error).
//
// Model resolution is input-driven exactly as in Python: the reasoner reads the
// model from its input (default "sonnet"). The caller (dag_executor) resolves
// the per-role model — git_model for run_git_init, merger_model for run_merger,
// integration_tester_model for run_integration_tester — and passes it in; the
// reasoner does not re-resolve it here.
package gitops

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/afx"
	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	gitprompts "github.com/Agent-Field/SWE-AF/go/internal/prompts/gitops"
	"github.com/Agent-Field/SWE-AF/go/internal/runtimex"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// Note is the observability surface each reasoner uses. It mirrors the Python
// router.note(message, tags=[...]) fire-and-forget call. Declaring it as an
// interface (rather than depending on *agent.Agent directly) lets tests capture
// the exact note messages and tags the roles emit — the same seam the Python
// tests get from a fake router.
type Note interface {
	Note(ctx context.Context, message string, tags ...string)
}

// App is the full dependency surface a gitops role needs: the harness choke
// point (satisfied by *agent.Agent via its Harness method, the same as
// harnessx.HarnessCaller) plus Note. *agent.Agent satisfies App.
type App interface {
	harnessx.HarnessCaller
	Note
}

// Deps carries the shared dependencies threaded into every handler. It is a
// struct (not a bare App) so the wiring wave can extend it without touching
// every handler signature.
type Deps struct {
	App App
}

// Handler is the common signature every gitops role reasoner exposes.
type Handler func(ctx context.Context, deps *Deps, input map[string]any) (any, error)

// Handlers returns the name→handler registration surface for wiring (design
// §8). The names are the exact Python reasoner names, addressable at the same
// node.reasoner path.
func Handlers() map[string]Handler {
	return map[string]Handler{
		"run_git_init":           RunGitInit,
		"run_workspace_setup":    RunWorkspaceSetup,
		"run_workspace_cleanup":  RunWorkspaceCleanup,
		"run_merger":             RunMerger,
		"run_integration_tester": RunIntegrationTester,
		"run_repo_finalize":      RunRepoFinalize,
		"run_github_pr":          RunGitHubPR,
	}
}

// ---------------------------------------------------------------------------
// Shared helpers (Python-parity string/number formatting + role plumbing)
// ---------------------------------------------------------------------------

// orDefault returns def when s is empty, else s. Reproduces a Python keyword
// default: run_git_init(model="sonnet", ...) uses "sonnet" when the caller
// omits model. Bind maps an absent key to "" (the Go zero), so "" == "use
// default". Callers in practice always pass a resolved non-empty model.
func orDefault(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

// resolveProvider maps the input ai_provider (default "claude") to the harness
// adapter string, mirroring Python's
// provider = runtime_to_harness_adapter(ai_provider). The adapter — not the
// provider — is what the harness Options.Provider field expects (design §4.7).
// An unsupported value returns the normalize error, matching Python raising
// before the harness call.
func resolveProvider(aiProvider string) (string, error) {
	return runtimex.RuntimeToHarnessAdapter(orDefault(aiProvider, "claude"))
}

// roleOptions builds the harness.Options for a role from its resolved
// parameters. MaxTurns is DEFAULT_AGENT_MAX_TURNS for every role, matching the
// Python reasoners. PermissionMode is passed through as-is: the harness treats
// "" as its default (Python passes `permission_mode or None`).
func roleOptions(provider, model, systemPrompt, cwd string, tools []string, permissionMode string) harness.Options {
	return harnessx.RoleOptions{
		Provider:       provider,
		Model:          model,
		MaxTurns:       config.DefaultAgentMaxTurns,
		Tools:          tools,
		PermissionMode: permissionMode,
		SystemPrompt:   systemPrompt,
		Cwd:            cwd,
	}.ToOptions()
}

// runRole is the shared harness-invocation core every gitops reasoner runs. It
// reproduces the Python try/except control flow around router.harness (design
// §4.1, §4.3):
//
//   - a *fatal.FatalHarnessError is propagated unchanged (Python `except
//     FatalHarnessError: raise`) — the caller must not swallow it into a
//     fallback;
//   - any other harness error emits the role's "<X> agent failed: <e>" note
//     (Python `except Exception as e`) and returns ok=false so the caller uses
//     its deterministic fallback;
//   - a successful parse (result.Parsed != nil) returns the value with ok=true;
//   - a parse failure (result.Parsed == nil, no error) returns ok=false with NO
//     note, matching Python's silent fall-through to the fallback.
//
// It never returns both a value and a non-nil error.
func runRole[T any](ctx context.Context, deps *Deps, prompt string, opts harness.Options, tag, errPrefix string) (*T, bool, error) {
	val, res, err := harnessx.Run[T](ctx, deps.App, prompt, opts)
	if err != nil {
		var fErr *fatal.FatalHarnessError
		if errors.As(err, &fErr) {
			return nil, false, err
		}
		deps.App.Note(ctx, fmt.Sprintf("%s: %v", errPrefix, err), tag, "error")
		return nil, false, nil
	}
	if res != nil && res.Parsed != nil {
		return val, true, nil
	}
	return nil, false, nil
}

// pyBool renders a Go bool the way a Python f-string interpolates a bool:
// True / False. Used so note() messages are byte-identical to Python.
func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

// pyListReprStrs renders a []string exactly like Python's str(list): ['a', 'b']
// (single-quoted elements, ", " separated, square brackets). Empty → [].
func pyListReprStrs(items []string) string {
	parts := make([]string, len(items))
	for i, it := range items {
		parts[i] = "'" + it + "'"
	}
	return "[" + strings.Join(parts, ", ") + "]"
}

// truncateRunes mirrors Python's str[:n] slice (by Unicode code point, not
// byte). Used for goal[:80] in the git_init start note.
func truncateRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// mapStr mirrors Python dict.get(key, default) constrained to string values:
// returns the string at key, or def when absent or not a string. Issue and
// branch dicts carry string names, so this covers the [i.get("name","?")]
// comprehensions used to format note() lists.
func mapStr(m map[string]any, key, def string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return def
}

// maybeWorkspaceManifest reproduces _maybe_workspace_manifest: a nil raw dict
// yields a nil manifest; otherwise the dict is materialized into the minimal
// prompt manifest (only the fields WorkspaceContextBlock consumes:
// repo_name / role / absolute_path). Decoding goes through the canonical
// schemas.WorkspaceManifest so the json tags match the serialized form.
func maybeWorkspaceManifest(raw map[string]any) *gitprompts.WorkspaceManifest {
	if raw == nil {
		return nil
	}
	wm, err := afx.Bind[schemas.WorkspaceManifest](raw)
	if err != nil {
		return nil
	}
	repos := make([]gitprompts.WorkspaceRepo, len(wm.Repos))
	for i, r := range wm.Repos {
		repos[i] = gitprompts.WorkspaceRepo{
			RepoName:     r.RepoName,
			Role:         r.Role,
			AbsolutePath: r.AbsolutePath,
		}
	}
	return &gitprompts.WorkspaceManifest{Repos: repos}
}
