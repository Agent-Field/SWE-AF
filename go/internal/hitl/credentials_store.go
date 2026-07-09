// Package hitl provides human-in-the-loop primitives for SWE-AF: the scout's
// scoped-credential store, ask-user forms, the hax REST client, and the
// approval wrapper. This file is the process-local, execution-scoped store for
// credentials the environment scout negotiates.
//
// Why a process-local map instead of BuildConfig or app.memory (verbatim from
// swe_af/hitl/credentials_store.py):
//
//   - BuildConfig is serialized through ToExecutionConfigDict() and passed to
//     execute() via app.Call. The control plane logs all app.Call input data,
//     which would persist the credentials.
//   - app.memory (scope=run) is synced to the control plane DB by design —
//     also persists.
//   - Filesystem under artifacts_dir is written to disk and archived.
//
// The scout's negotiation produces credentials that should *only* live in the
// agent process's memory for the duration of the build, then be cleared. A
// module-level map keyed by run ID is the simplest way to achieve that while
// keeping concurrent builds (which share the process) isolated.
//
// Security boundary:
//
//   - Values are never logged.
//   - Values are never written to disk.
//   - Values are not serialized through app.Call (use this store from inside
//     the receiving reasoner, not as a kwarg).
//   - The build()'s deferred cleanup MUST call ClearScopedCredentials — every
//     error path included.
package hitl

import (
	"strings"
	"sync"
)

// store is process-local, keyed by run ID (each build has its own), guarded by
// mu. Never persisted anywhere.
var (
	mu    sync.Mutex
	store = map[string]map[string]string{}
)

// StoreScopedCredentials replaces the stored credentials for runID with creds.
//
// Filters out empty/whitespace-only values so a partially-filled mega-form
// (user skipped some fields) doesn't surface as empty env vars to downstream
// subprocesses (which can be confusing — "is the env set or not?"). If nothing
// survives the filter, any existing entry for runID is removed.
func StoreScopedCredentials(runID string, creds map[string]string) {
	if runID == "" {
		return
	}
	filtered := map[string]string{}
	for k, v := range creds {
		if strings.TrimSpace(v) != "" {
			filtered[k] = v
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(filtered) > 0 {
		store[runID] = filtered
	} else {
		delete(store, runID)
	}
}

// GetScopedCredentials returns a copy of the stored credentials for runID.
//
// Returns an empty (non-nil) map if nothing is stored — callers should treat
// that as "no credentials negotiated; rely on the base env only".
func GetScopedCredentials(runID string) map[string]string {
	out := map[string]string{}
	if runID == "" {
		return out
	}
	mu.Lock()
	defer mu.Unlock()
	for k, v := range store[runID] {
		out[k] = v
	}
	return out
}

// ClearScopedCredentials removes credentials for runID from process memory.
func ClearScopedCredentials(runID string) {
	if runID == "" {
		return
	}
	mu.Lock()
	defer mu.Unlock()
	delete(store, runID)
}

// InjectCredentialsIntoEnv returns a NEW env map = base ∪ scoped credentials.
//
// Scoped credentials WIN over base so a freshly-minted token from the scout
// overrides any stale value already in the environment (e.g. an expired
// RAILWAY_TOKEN from a previous build).
//
// Callers should use this immediately before each harness call, passing the
// result as the env. The base is normally a copy of the process environment so
// the subprocess still inherits everything the parent has — we only
// ADD/override the scoped creds.
func InjectCredentialsIntoEnv(base map[string]string, runID string) map[string]string {
	merged := map[string]string{}
	for k, v := range base {
		merged[k] = v
	}
	for k, v := range GetScopedCredentials(runID) {
		merged[k] = v
	}
	return merged
}
