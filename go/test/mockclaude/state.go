package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// state.go implements cross-process invocation counters and an append-only
// invocation log under SWE_MOCK_STATE_DIR. Every mock invocation is a separate
// process, and role reasoners run in parallel (level issues concurrently, and
// qa+reviewer concurrently), so all shared-file access is guarded with an
// advisory file lock (flock).

// stateDir returns SWE_MOCK_STATE_DIR (creating it), or "" when unset.
func stateDir() string {
	d := os.Getenv("SWE_MOCK_STATE_DIR")
	if d == "" {
		return ""
	}
	_ = os.MkdirAll(d, 0o755)
	return d
}

// withLock runs fn while holding an exclusive advisory lock on <dir>/.lock.
func withLock(dir string, fn func()) {
	lockPath := filepath.Join(dir, ".lock")
	f, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		fn() // best-effort: proceed unlocked
		return
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		fn()
		return
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	fn()
}

// nextCount atomically increments the counter for key and returns the new value
// (1 on first call). Returns 1 when no state dir is configured.
func nextCount(key string) int {
	dir := stateDir()
	if dir == "" {
		return 1
	}
	result := 1
	withLock(dir, func() {
		path := filepath.Join(dir, "counters.json")
		counters := map[string]int{}
		if b, err := os.ReadFile(path); err == nil {
			_ = json.Unmarshal(b, &counters)
		}
		counters[key]++
		result = counters[key]
		if b, err := json.MarshalIndent(counters, "", "  "); err == nil {
			_ = os.WriteFile(path, b, 0o644)
		}
	})
	return result
}

// logInvocation appends one JSON line to <state>/invocations.jsonl recording the
// role, issue, decision and other context of this invocation.
func logInvocation(role, issue, decision string, extra map[string]any) {
	dir := stateDir()
	if dir == "" {
		return
	}
	rec := map[string]any{
		"ts":       time.Now().UTC().Format(time.RFC3339Nano),
		"role":     role,
		"issue":    issue,
		"decision": decision,
	}
	for k, v := range extra {
		rec[k] = v
	}
	line, err := json.Marshal(rec)
	if err != nil {
		return
	}
	withLock(dir, func() {
		f, err := os.OpenFile(filepath.Join(dir, "invocations.jsonl"),
			os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			return
		}
		defer f.Close()
		fmt.Fprintln(f, string(line))
	})
}
