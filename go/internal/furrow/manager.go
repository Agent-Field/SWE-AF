package furrow

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/Agent-Field/SWE-AF/go/internal/workspace"
)

// Options configures one node-wide furrow manager.
type Options struct {
	Bin         string
	StoreRoot   string
	RemotesRoot string
	PublicAddr  string
	Logger      *log.Logger
	Now         func() time.Time
	Exec        func(*exec.Cmd) ([]byte, error)
}

// Manager owns the node's persistent run registry and furrow content store.
//
// Locking has two levels on purpose. mu guards the registry and is only ever
// held for map access, never across a furrow invocation: a node serves several
// builds at once, and an initial capture of a large repository takes long
// enough that holding one lock across it would stall every other run's publish.
// runLocks serializes work per run instead, which is the only ordering that
// actually matters — two calls for the same run must not both pair it.
type Manager struct {
	mu          sync.RWMutex
	bin         string
	storeRoot   string
	remotesRoot string
	publicAddr  string
	logger      *log.Logger
	now         func() time.Time
	exec        func(*exec.Cmd) ([]byte, error)
	enabled     bool
	entries     map[string]Entry
	runLocks    map[string]*sync.Mutex
}

// lockRun serializes callers working on one run and returns its unlock.
func (m *Manager) lockRun(runID string) func() {
	m.mu.Lock()
	if m.runLocks == nil {
		m.runLocks = make(map[string]*sync.Mutex)
	}
	lock, ok := m.runLocks[runID]
	if !ok {
		lock = &sync.Mutex{}
		m.runLocks[runID] = lock
	}
	m.mu.Unlock()
	lock.Lock()
	return lock.Unlock
}

// New constructs a manager and loads its persisted registry. Missing helpers
// and corrupt registries deliberately degrade to an inert or empty manager.
func New(opts Options) *Manager {
	m := &Manager{
		storeRoot:   opts.StoreRoot,
		remotesRoot: opts.RemotesRoot,
		publicAddr:  opts.PublicAddr,
		logger:      opts.Logger,
		now:         opts.Now,
		exec:        opts.Exec,
		entries:     make(map[string]Entry),
	}
	if m.logger == nil {
		m.logger = log.Default()
	}
	if m.now == nil {
		m.now = time.Now
	}
	if m.exec == nil {
		m.exec = func(cmd *exec.Cmd) ([]byte, error) { return cmd.Output() }
	}
	if m.storeRoot == "" {
		m.storeRoot = filepath.Join(workspace.Root(), "furrow")
	}
	if m.remotesRoot == "" {
		m.remotesRoot = filepath.Join(m.storeRoot, "remotes")
	}
	if os.Getenv(EnvEnabled) == "0" {
		return m
	}
	if opts.Bin != "" {
		if !runnable(opts.Bin) {
			m.logf("furrow disabled: binary %q is missing or not executable", opts.Bin)
			return m
		}
		m.bin = opts.Bin
	} else {
		bin, err := ResolveBin()
		if err != nil {
			m.logf("furrow disabled: %v", err)
			return m
		}
		m.bin = bin
	}
	m.enabled = true
	m.loadRegistry()
	return m
}

func (m *Manager) logf(format string, args ...any) {
	if m != nil && m.logger != nil {
		m.logger.Printf(format, args...)
	}
}

func (m *Manager) Enabled() bool {
	if m == nil {
		return false
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.enabled
}

func (m *Manager) command(repoPath string, args ...string) ([]byte, error) {
	argv := append([]string{"--repo", repoPath, "--json"}, args...)
	cmd := exec.Command(m.bin, argv...)
	cmd.Env = append(os.Environ(), "FURROW_DATA_DIR="+m.storeRoot)
	return m.exec(cmd)
}

func (m *Manager) Attach(runID, buildID, repoPath string) (*Handle, error) {
	if m == nil || !m.Enabled() {
		return nil, nil
	}
	if info, err := os.Stat(filepath.Join(repoPath, ".git")); err != nil || !info.IsDir() {
		return nil, nil
	}

	unlock := m.lockRun(runID)
	defer unlock()
	if handle := m.Handle(runID); handle != nil {
		return handle, nil
	}
	// Every line must be `exclude <relative-subtree>`; furrow rejects the whole
	// file otherwise and `watch` then fails, which would leave the mirror
	// silently switched off for every build.
	policy := []byte("exclude .obs\nexclude node_modules\n")
	if err := os.WriteFile(filepath.Join(repoPath, ".furrowpolicy"), policy, 0o644); err != nil {
		m.logf("furrow attach %q: write policy: %v", runID, err)
		return nil, nil
	}
	if _, err := m.command(repoPath, "watch", "--no-daemon"); err != nil {
		m.logf("furrow attach %q: watch: %v", runID, err)
		return nil, nil
	}

	namespace := sanitizeNamespace(runID)
	storeDir := filepath.Join(m.remotesRoot, runID)
	if err := os.MkdirAll(storeDir, 0o700); err != nil {
		m.logf("furrow attach %q: create remote: %v", runID, err)
		return nil, nil
	}
	out, err := m.command(repoPath, "remote", "add", storeDir, "--name", namespace)
	if err != nil {
		m.logf("furrow attach %q: pair remote: %v", runID, err)
		return nil, nil
	}
	var paired struct {
		Key string `json:"key_hex"`
	}
	if err := json.Unmarshal(out, &paired); err != nil || len(paired.Key) != 64 {
		m.logf("furrow attach %q: invalid remote response", runID)
		return nil, nil
	}
	if _, err := hex.DecodeString(paired.Key); err != nil {
		m.logf("furrow attach %q: invalid key_hex", runID)
		return nil, nil
	}
	tokenBytes := make([]byte, 32)
	if _, err := rand.Read(tokenBytes); err != nil {
		return nil, fmt.Errorf("furrow attach %q: mint token: %w", runID, err)
	}
	now := m.now()
	entry := Entry{RunID: runID, BuildID: buildID, RepoPath: repoPath, Namespace: namespace,
		Key: paired.Key, Token: hex.EncodeToString(tokenBytes), Ref: namespace, StoreDir: storeDir,
		CreatedAt: now, UpdatedAt: now}
	m.mu.Lock()
	m.entries[runID] = entry
	err = m.saveRegistryLocked()
	if err != nil {
		delete(m.entries, runID)
	}
	m.mu.Unlock()
	if err != nil {
		return nil, fmt.Errorf("furrow attach %q: save registry: %w", runID, err)
	}
	return m.handle(entry), nil
}

func sanitizeNamespace(runID string) string {
	var b strings.Builder
	for _, r := range runID {
		if unicode.IsLetter(r) && r <= unicode.MaxASCII || unicode.IsDigit(r) && r <= unicode.MaxASCII || strings.ContainsRune("._-", r) {
			b.WriteRune(r)
		} else {
			b.WriteByte('-')
		}
		if b.Len() >= 96 {
			break
		}
	}
	if b.Len() == 0 {
		return "run"
	}
	return b.String()[:min(b.Len(), 96)]
}

func (m *Manager) handle(entry Entry) *Handle {
	remote := "dir:" + m.remotesRoot
	if m.publicAddr != "" {
		remote = "ssh://" + m.publicAddr
	}
	return &Handle{Version: HandleVersion, Remote: remote, Namespace: entry.Namespace,
		Key: entry.Key, Token: entry.Token, RepoPath: entry.RepoPath, Ref: entry.Ref}
}

func (m *Manager) Publish(runID, label string) error {
	if m == nil || !m.Enabled() {
		return nil
	}
	unlock := m.lockRun(runID)
	defer unlock()

	m.mu.RLock()
	entry, ok := m.entries[runID]
	m.mu.RUnlock()
	if !ok {
		return fmt.Errorf("furrow publish: unknown run ID %q", runID)
	}
	if _, err := m.command(entry.RepoPath, "snap", "-m", label); err != nil {
		m.logf("furrow publish %q: snapshot: %v", runID, err)
		return nil
	}
	if _, err := m.command(entry.RepoPath, "sync", "--push"); err != nil {
		m.logf("furrow publish %q: sync: %v", runID, err)
		return nil
	}

	m.mu.Lock()
	// A sweep may have retired this run while the push was in flight; recording
	// a fresh timestamp then would resurrect a row whose store is already gone.
	if current, ok := m.entries[runID]; ok {
		current.UpdatedAt = m.now()
		m.entries[runID] = current
		if err := m.saveRegistryLocked(); err != nil {
			m.logf("furrow publish %q: save registry: %v", runID, err)
		}
	}
	m.mu.Unlock()
	return nil
}

func (m *Manager) Handle(runID string) *Handle {
	if m == nil || !m.Enabled() {
		return nil
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	entry, ok := m.entries[runID]
	if !ok {
		return nil
	}
	return m.handle(entry)
}

func (m *Manager) Detach(runID string) error {
	if m == nil || !m.Enabled() {
		return nil
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	if _, ok := m.entries[runID]; !ok {
		return fmt.Errorf("furrow detach: unknown run ID %q", runID)
	}
	return nil
}

func (m *Manager) Sweep(maxAge time.Duration, maxBytes int64) (int, error) {
	if m == nil || !m.Enabled() {
		return 0, nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	removed := 0
	now := m.now()
	for runID, entry := range m.entries {
		if maxAge > 0 && now.Sub(entry.UpdatedAt) > maxAge {
			if err := os.RemoveAll(entry.StoreDir); err != nil {
				return removed, fmt.Errorf("furrow sweep %q: %w", runID, err)
			}
			delete(m.entries, runID)
			removed++
		}
	}
	if removed > 0 {
		if err := m.saveRegistryLocked(); err != nil {
			return removed, fmt.Errorf("furrow sweep: save registry: %w", err)
		}
	}
	if maxBytes >= 0 {
		for {
			total, err := dirSize(m.remotesRoot)
			if err != nil && !errors.Is(err, os.ErrNotExist) {
				return removed, fmt.Errorf("furrow sweep size: %w", err)
			}
			if total <= maxBytes || len(m.entries) == 0 {
				break
			}
			var oldestID string
			var oldest Entry
			for id, entry := range m.entries {
				if oldestID == "" || entry.UpdatedAt.Before(oldest.UpdatedAt) {
					oldestID, oldest = id, entry
				}
			}
			if err := os.RemoveAll(oldest.StoreDir); err != nil {
				return removed, fmt.Errorf("furrow sweep %q: %w", oldestID, err)
			}
			delete(m.entries, oldestID)
			removed++
			// registry.json is part of remotesRoot's byte total. Persist each
			// removal before measuring again so stale rows cannot over-evict.
			if err := m.saveRegistryLocked(); err != nil {
				return removed, fmt.Errorf("furrow sweep: save registry: %w", err)
			}
		}
	}
	return removed, nil
}

func dirSize(root string) (int64, error) {
	var size int64
	err := filepath.Walk(root, func(_ string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.Mode().IsRegular() {
			size += info.Size()
		}
		return nil
	})
	return size, err
}
