package harnessx

import (
	"context"
	"encoding/json"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// runIDFromContext extracts the build's run ID from the reasoner execution
// context. In production this reads agent.ExecutionContextFrom(ctx).RunID, which
// the SDK populates on the handler's ctx before dispatch. It is a package var
// (not a direct call) purely so tests can inject a run ID — the SDK's context
// key is unexported, so there is no public way to seed ExecutionContext into a
// ctx from an external package.
var runIDFromContext = func(ctx context.Context) string {
	return agent.ExecutionContextFrom(ctx).RunID
}

// HarnessCaller is the minimal method set Run needs from *agent.Agent. Declaring
// it as an interface (rather than depending on the concrete *agent.Agent) lets
// tests supply a mock harness without a live subprocess — the same seam the
// Python tests get by patching router.harness. *agent.Agent satisfies it via
// its Harness method (sdk/go/agent/harness.go:84).
type HarnessCaller interface {
	Harness(ctx context.Context, prompt string, schema map[string]any, dest any, opts harness.Options) (*harness.Result, error)
}

// Run is the single generic entry point every role reasoner uses to invoke the
// harness for structured output of type T.
//
// Sequence (design §4.1, §2.3, §4.3):
//  1. Reflect T into the JSON schema the harness consumes (cached per type).
//  2. Inject the build's run-scoped credentials into opts.Env, scoped creds
//     overriding the base env — mirroring the Python precedence where a freshly
//     minted scout token beats a stale value inherited from os.environ. The run
//     ID comes from agent.ExecutionContextFrom(ctx).RunID.
//  3. Call app.Harness with a fresh *T dest.
//  4. Classify fatal (non-retryable) API errors FIRST, before the Parsed==nil
//     fallback, so the real billing/auth message surfaces past every retry layer
//     as a *fatal.FatalHarnessError (callers must propagate it, not swallow it).
//  5. On Result.Parsed == nil (the harness could not parse valid JSON into T),
//     return a default-seeded T plus the Result — NOT an error — so the caller
//     inspects Result.IsError and applies its role-specific deterministic
//     fallback. The seed comes from unmarshaling "{}" into T, which triggers
//     T's UnmarshalJSON default-seeding (schemas §2.2) when present, and yields
//     the Go zero value otherwise.
//
// Returns (*T, *harness.Result, error). The Result is returned even alongside a
// non-nil error so callers can inspect diagnostics.
func Run[T any](ctx context.Context, app HarnessCaller, prompt string, opts harness.Options) (*T, *harness.Result, error) {
	schema := schemaFor[T]()

	runID := runIDFromContext(ctx)
	opts.Env = hitl.InjectCredentialsIntoEnv(opts.Env, runID)

	var dest T
	result, err := app.Harness(ctx, prompt, schema, &dest, opts)
	if err != nil {
		return nil, result, err
	}

	// Fatal-error classification comes before the Parsed==nil fallback so the
	// real non-retryable message is not masked by a generic fallback struct.
	if fErr := fatal.CheckFatalHarnessError(result); fErr != nil {
		return nil, result, fErr
	}

	// Schema parse failure: hand the caller a default-seeded value plus the
	// Result so it can apply its own deterministic fallback. Not an error.
	if result == nil || result.Parsed == nil {
		seeded := seedDefaults[T]()
		schemas.EmptyForNilSlices(&seeded)
		return &seeded, result, nil
	}

	// Normalise nil slices to empty slices on the parsed result so every role
	// result serialises its list fields as [] (pydantic parity) — matching the
	// checkpoint/DAGState normalisation. Maps and nil pointers are left null.
	schemas.EmptyForNilSlices(&dest)
	return &dest, result, nil
}

// seedDefaults returns a T seeded with its pydantic-parity defaults. Unmarshaling
// an empty JSON object invokes T's UnmarshalJSON (which seeds non-zero defaults,
// schemas §2.2) when T implements it; for a plain struct it leaves the Go zero
// value. Any unmarshal error is ignored — the zero value is an acceptable floor.
func seedDefaults[T any]() T {
	var v T
	_ = json.Unmarshal([]byte("{}"), &v)
	return v
}

// RoleOptions is the role→harness parameter mapping (design §4.1). Each role
// reasoner fills it from its resolved config, then ToOptions produces the
// harness.Options passed to Run. Centralizing the mapping here keeps every role
// consistent — the field set mirrors the keyword arguments the Python role
// reasoners pass to router.harness (system_prompt, schema, model, provider,
// tools, cwd, max_turns, permission_mode).
type RoleOptions struct {
	// Provider is the harness ADAPTER string (not the provider), e.g. the output
	// of runtimex.RuntimeToHarnessAdapter — "claude-code", "opencode", "codex".
	// Python passes provider=runtime_to_harness_adapter(ai_provider).
	Provider string

	// Model is the resolved role model identifier.
	Model string

	// MaxTurns caps agent iterations (Python DEFAULT_AGENT_MAX_TURNS per role).
	MaxTurns int

	// Tools is the allowed-tool list (e.g. ["Read","Write","Glob","Grep","Bash"]).
	Tools []string

	// PermissionMode maps to Python's permission_mode; empty means the harness
	// default (Python passes `permission_mode or None`, and harness.Options
	// treats "" as "use default").
	PermissionMode string

	// SystemPrompt is the role's module-level system prompt.
	SystemPrompt string

	// Cwd is the working directory for the subprocess (repo path / worktree).
	Cwd string

	// Env is the base environment for the subprocess. Run overlays the build's
	// scoped credentials on top of this before invoking the harness.
	Env map[string]string
}

// ToOptions converts a RoleOptions into a harness.Options. Run injects scoped
// credentials into Env afterwards, so callers leave Env as the base env only.
func (r RoleOptions) ToOptions() harness.Options {
	return harness.Options{
		Provider:       r.Provider,
		Model:          r.Model,
		MaxTurns:       r.MaxTurns,
		Tools:          r.Tools,
		PermissionMode: r.PermissionMode,
		SystemPrompt:   r.SystemPrompt,
		Cwd:            r.Cwd,
		Env:            r.Env,
	}
}
