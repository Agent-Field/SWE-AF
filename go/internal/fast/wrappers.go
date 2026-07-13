package fast

// wrappers.go ports the seven delegating wrappers from swe_af/fast/__init__.py.
//
// In Python the fast node registers thin wrappers (run_git_init, run_coder,
// run_verifier, run_repo_finalize, run_github_pr, run_ci_watcher, run_ci_fixer)
// on fast_router; each forwards its arguments verbatim to the identically-named
// execution_agents role function. The fast node then ALSO includes the full
// execution router, so the same names are backed by the real role handlers.
//
// In Go there is no per-reasoner forwarding shim: the swe-fast node mounts the
// full-pipeline role handler set (roles/*), and these seven names are registered
// on swe-fast backed by those exact role handlers with arguments unchanged.
// Wrappers is the machine-readable statement of that delegation for T6.2: each
// map KEY is the reasoner name to register on the swe-fast node, and the VALUE
// is the full-pipeline role handler name it delegates to (identity here, because
// the Python wrappers forward to the same-named role function without renaming
// or rewriting any argument).

// wrapperNames is the ordered list of the seven wrapper reasoner names, in the
// exact order they are declared in fast/__init__.py.
var wrapperNames = []string{
	"run_git_init",
	"run_coder",
	"run_verifier",
	"run_repo_finalize",
	"run_github_pr",
	"run_ci_watcher",
	"run_ci_fixer",
}

// Wrappers returns the delegation map for the swe-fast node's wrapper reasoners:
// wrapper reasoner name → full-pipeline role handler name it forwards to. T6.2
// registers each KEY on the swe-fast node backed by the role handler for VALUE.
// The mapping is identity because the Python wrappers forward arguments verbatim
// to the same-named execution role.
func Wrappers() map[string]string {
	m := make(map[string]string, len(wrapperNames))
	for _, name := range wrapperNames {
		m[name] = name
	}
	return m
}

// WrapperNames returns the seven wrapper reasoner names in declaration order.
func WrapperNames() []string {
	out := make([]string, len(wrapperNames))
	copy(out, wrapperNames)
	return out
}
