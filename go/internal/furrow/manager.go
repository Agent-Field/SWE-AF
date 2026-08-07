package furrow

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
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
	CmdTimeout  time.Duration
	MaxBytes    int64
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
	mu               sync.RWMutex
	bin              string
	storeRoot        string
	remotesRoot      string
	publicAddr       string
	transportHealthy func() bool
	logger           *log.Logger
	now              func() time.Time
	exec             func(*exec.Cmd) ([]byte, error)
	cmdTimeout       time.Duration
	maxBytes         int64
	enabled          bool
	entries          map[string]Entry
	runLocks         map[string]*sync.Mutex
}

// SetTransportHealth supplies the public transport health gate used when
// issuing handles. Without a gate, public transport is treated as unavailable.
func (m *Manager) SetTransportHealth(healthy func() bool) {
	if m == nil {
		return
	}
	m.mu.Lock()
	m.transportHealthy = healthy
	m.mu.Unlock()
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
		cmdTimeout:  opts.CmdTimeout,
		maxBytes:    opts.MaxBytes,
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
	if m.cmdTimeout == 0 {
		m.cmdTimeout = 5 * time.Minute
	}
	if m.maxBytes == 0 {
		m.maxBytes = configuredMaxBytes()
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
	ctx, cancel := context.WithTimeout(context.Background(), m.cmdTimeout)
	defer cancel()
	argv := append([]string{"--repo", repoPath, "--json"}, args...)
	cmd := exec.CommandContext(ctx, m.bin, argv...)
	cmd.WaitDelay = time.Second
	cmd.Env = append(os.Environ(), "FURROW_DATA_DIR="+m.storeRoot)
	out, err := m.exec(cmd)
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return nil, fmt.Errorf("furrow command timeout after %s: %w", m.cmdTimeout, ctx.Err())
	}
	return out, err
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
	if m.maxBytes > 0 {
		total, err := m.aggregateSize()
		if err != nil {
			return nil, fmt.Errorf("furrow attach %q: measure aggregate store size: %w", runID, err)
		}
		if total > m.maxBytes {
			err := fmt.Errorf("furrow attach %q: aggregate store size %d exceeds disk budget %d", runID, total, m.maxBytes)
			m.logf("WARN %v; new mirrors are disabled until space is freed", err)
			return nil, err
		}
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
	storeDir := filepath.Join(m.remotesRoot, namespace)
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
	// Attach already owns the run lock, so use the locked publish path directly:
	// calling Publish here would try to acquire the same non-reentrant mutex.
	_ = m.publishLocked(runID, "attached")
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
	out := b.String()[:min(b.Len(), 96)]
	// Dots survive sanitization, and "." / ".." are the two surviving names
	// the filesystem treats as traversal rather than a directory of its own.
	if out == "." || out == ".." {
		return "run"
	}
	return out
}

func (m *Manager) handle(entry Entry) *Handle {
	// The run's own store, not the root that holds every run's: a caller pairs
	// directly with this path, and the root is not a furrow remote at all.
	// Over the network the path stays on the node — furrowd resolves it from
	// the token — so the address is all the caller needs.
	remote := "dir:" + entry.StoreDir
	if m.publicAddr != "" {
		if m.transportHealthy != nil && m.transportHealthy() {
			remote = "ssh://" + m.publicAddr
		} else {
			m.logf("WARN furrow: public address configured but furrowd is not running or its local listen address is unreachable; using local handle")
		}
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
	return m.publishLocked(runID, label)
}

// publishLocked snapshots and pushes a run while its per-run lock is held.
func (m *Manager) publishLocked(runID, label string) error {
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
	removed := 0
	now := m.now()

	m.mu.RLock()
	stale := make([]string, 0, len(m.entries))
	for runID, entry := range m.entries {
		if maxAge > 0 && now.Sub(entry.UpdatedAt) > maxAge {
			stale = append(stale, runID)
		}
	}
	m.mu.RUnlock()
	for _, runID := range stale {
		dropped, err := m.retire(runID, maxAge)
		if err != nil {
			return removed, err
		}
		if dropped {
			removed++
		}
	}

	if maxBytes >= 0 {
		for {
			// Walking the store is I/O, so it happens with no lock held.
			total, err := m.aggregateSize()
			if err != nil && !errors.Is(err, os.ErrNotExist) {
				return removed, fmt.Errorf("furrow sweep size: %w", err)
			}
			m.mu.RLock()
			var oldestID string
			var oldest Entry
			for id, entry := range m.entries {
				if oldestID == "" || entry.UpdatedAt.Before(oldest.UpdatedAt) {
					oldestID, oldest = id, entry
				}
			}
			empty := len(m.entries) == 0
			m.mu.RUnlock()
			if total <= maxBytes {
				break
			}
			if empty {
				m.logf("WARN furrow sweep: aggregate store size %d exceeds disk budget %d with no remote entries; new mirrors are disabled until space is freed", total, maxBytes)
				break
			}
			dropped, err := m.retire(oldestID, 0)
			if err != nil {
				return removed, err
			}
			if !dropped {
				// Something republished it while we were measuring; measuring
				// again would pick the same victim forever.
				break
			}
			removed++
		}
	}
	return removed, nil
}

// retire deletes one run's remote store and its registry row. It takes that
// run's lock so a publish in flight finishes first rather than pushing into a
// directory being deleted, and re-checks staleness under the lock so a run that
// became active in the meantime is left alone.
func (m *Manager) retire(runID string, maxAge time.Duration) (bool, error) {
	unlock := m.lockRun(runID)
	defer unlock()

	m.mu.RLock()
	entry, ok := m.entries[runID]
	m.mu.RUnlock()
	if !ok {
		return false, nil
	}
	if maxAge > 0 && m.now().Sub(entry.UpdatedAt) <= maxAge {
		return false, nil
	}
	// Remove the files first: a failure here leaves the row in place so the next
	// sweep retries, rather than orphaning a store nothing points at any more.
	if err := os.RemoveAll(entry.StoreDir); err != nil {
		return false, fmt.Errorf("furrow sweep %q: %w", runID, err)
	}
	m.mu.Lock()
	delete(m.entries, runID)
	// The run's lock is deliberately left behind. Dropping it here would let a
	// goroutine already waiting on this mutex and one arriving afterwards end
	// up holding two different mutexes for the same run, which is the one thing
	// the per-run lock exists to prevent. A retired run leaves a bare mutex.
	err := m.saveRegistryLocked()
	m.mu.Unlock()
	if err != nil {
		return true, fmt.Errorf("furrow sweep: save registry: %w", err)
	}
	return true, nil
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

func (m *Manager) aggregateSize() (int64, error) {
	storeSize, err := dirSize(m.storeRoot)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return 0, err
	}
	remotesSize, err := dirSize(m.remotesRoot)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return 0, err
	}
	if within(m.storeRoot, m.remotesRoot) {
		return storeSize, nil
	}
	if within(m.remotesRoot, m.storeRoot) {
		return remotesSize, nil
	}
	return storeSize + remotesSize, nil
}

func within(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func configuredMaxBytes() int64 {
	const defaultMaxGB = 20
	maxGB, err := strconv.ParseInt(os.Getenv("SWE_FURROW_MAX_GB"), 10, 64)
	if err != nil || maxGB < 0 {
		maxGB = defaultMaxGB
	}
	return maxGB * 1024 * 1024 * 1024
}
