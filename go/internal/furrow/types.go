// Package furrow gives a run's workspace a byte-exact, encrypted mirror that a
// main harness (Claude Code, another coding agent) can clone and follow while
// the run is still going.
//
// Mirroring has to be asked for: an explicit SWE_FURROW_ENABLED decides in
// either direction, and when it is unconfigured the feature follows
// FURROW_PUBLIC_ADDR — set by the desktop app's cloud deploy exactly when a
// public sync endpoint was provisioned for this node (see enabledByEnv). A
// local install that set neither stays off. A mirror is a byte-exact second
// copy of the build workspace, untracked files and all, so turning it on means
// accepting the disk it costs.
//
// The contract with the rest of SWE-AF is deliberately one-way: orchestration
// calls Attach when a workspace exists and Publish when something worth seeing
// has landed, and never has to care whether furrow is installed. Every entry
// point is a no-op that returns a nil handle when the binary is missing, the
// feature is switched off, or the workspace cannot be attached — availability is
// discovered by the caller finding a handle in the result, never by asking.
package furrow

import "time"

// HandleVersion is the schema version of the handle embedded in reasoner
// results. Bump it only for a breaking change to the wire shape; consumers are
// expected to ignore a handle whose version they do not recognise.
const HandleVersion = 1

// Handle is what a caller needs to materialize this run's workspace on another
// machine. It travels inside the reasoner result under the key "workspace_handle".
//
// Remote carries its own transport:
//
//	dir:<abs path>          same box; pair with the directory and bootstrap-pull
//	ssh://<host>[:<port>]   furrowd (or sshd) reachable over the network
//
// Key is a 64-hex furrow recovery key scoped to this run alone: it decrypts this
// run's namespace and nothing else. Token authenticates the transport hop to
// furrowd and is empty for dir: handles, which are guarded by filesystem
// permissions instead.
type Handle struct {
	Version   int    `json:"v"`
	Remote    string `json:"remote"`
	Namespace string `json:"namespace"`
	Key       string `json:"key"`
	Token     string `json:"token,omitempty"`
	// RepoPath is the workspace's absolute path on the node. It is advisory —
	// useful when the caller shares the filesystem, meaningless otherwise.
	RepoPath string `json:"repo_path,omitempty"`
	// Deliberately no Ref: every run gets its OWN remote directory (the
	// namespace IS the directory), so there is nothing on a remote to
	// disambiguate, and publishing under a named ref would break the pull the
	// agentfield-use skill documents — `sync --pull --bootstrap`, which reads
	// the default HEAD. Verified live: publishing to a named ref makes that
	// exact command fail with "no such file or directory". If remotes are ever
	// shared between runs, add the ref to the publish, the handle, and the
	// skill's recipe together — never one without the others.
}

// Entry is one run's row in the registry: the mapping from a control-plane run
// ID to the workspace on disk that SWE-AF only ever knew by its own build ID.
type Entry struct {
	RunID     string    `json:"run_id"`
	BuildID   string    `json:"build_id,omitempty"`
	RepoPath  string    `json:"repo_path"`
	Namespace string    `json:"namespace"`
	Key       string    `json:"key"`
	Token     string    `json:"token,omitempty"`
	StoreDir  string    `json:"store_dir"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Attacher is the surface orchestration code depends on.
//
// Every method on *Manager tolerates a nil RECEIVER, but that is not the same
// as callers never needing a nil check: they hold this interface, and a nil
// Attacher interface value has no method set to dispatch to — calling through
// it panics. node.buildFurrowManager returns a nil interface when furrow is off
// or unavailable, so orch.Deps.furrowAttach and furrowPublish nil-check the
// field before every call. Anything else holding an Attacher must do the same.
type Attacher interface {
	// Enabled reports whether this manager will do anything at all.
	Enabled() bool
	// Attach begins mirroring repoPath for runID and returns the handle a caller
	// needs to reach it. It is idempotent per runID, and an empty runID is
	// refused outright — it would be a key two builds share, not a missing
	// label. A nil handle with a nil error means furrow is simply unavailable —
	// never an error worth failing a build over.
	Attach(runID, buildID, repoPath string) (*Handle, error)
	// Publish seals current state and pushes it to the run's remote. Safe to
	// call often; cheap when nothing changed.
	Publish(runID, label string) error
	// Handle returns a previously attached run's handle, or nil if unknown.
	Handle(runID string) *Handle
	// Detach reports whether the run is still known, returning an error when it
	// is not. It does NOT stop mirroring: `furrow watch --no-daemon` leaves
	// nothing running to stop, and the mirror's remote and registry row are
	// reclaimed by Sweep on age or budget instead. The name is kept because
	// callers use it as an "is this run still attached" probe.
	Detach(runID string) error
	// Sweep removes registry entries and remote stores older than maxAge, and
	// trims the store root to maxBytes (oldest first). Returns entries removed.
	// Each limit is independently opt-out: a maxAge of zero or less disables
	// age expiry, and a maxBytes of zero or less means UNLIMITED disk and skips
	// budget eviction entirely — it never means "evict everything".
	Sweep(maxAge time.Duration, maxBytes int64) (int, error)
}
