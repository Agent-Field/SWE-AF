package furrow

import (
	"context"
	"log"
	"os"
	"os/exec"
	"sync"
	"time"
)

const defaultDaemonAddr = ":8802"

// Supervisor owns the furrowd process lifecycle. A nil Supervisor is safe.
type Supervisor struct {
	manager *Manager
	bin     string
	addr    string
	logger  *log.Logger

	backoffInitial time.Duration
	backoffMax     time.Duration
	healthyUptime  time.Duration
	maxFailures    int
	now            func() time.Time
	after          func(time.Duration) <-chan time.Time

	mu      sync.Mutex
	started bool
	done    chan struct{}
}

// NewSupervisor constructs an inert supervisor unless every feature gate is
// satisfied. Gate failures are deliberately silent feature discovery.
func NewSupervisor(manager *Manager) *Supervisor {
	s := &Supervisor{manager: manager}
	if !s.Enabled() {
		return s
	}
	daemon, err := ResolveDaemonBin()
	if err != nil {
		return s
	}
	s.bin = daemon
	s.addr = envOrDefault("FURROWD_ADDR", defaultDaemonAddr)
	s.logger = manager.logger
	s.backoffInitial = time.Second
	s.backoffMax = 60 * time.Second
	s.healthyUptime = 60 * time.Second
	s.maxFailures = 5
	s.now = time.Now
	s.after = time.After
	return s
}

// Enabled reports whether the manager and advertised-address gates are open.
func (s *Supervisor) Enabled() bool {
	return s != nil && s.manager != nil && s.manager.Enabled() && os.Getenv("FURROW_PUBLIC_ADDR") != ""
}

// Available reports whether all gates, including binary resolution, are open.
func (s *Supervisor) Available() bool { return s != nil && s.Enabled() && s.bin != "" }

// Addr returns furrowd's listen address, or empty for an inert supervisor.
func (s *Supervisor) Addr() string {
	if s == nil {
		return ""
	}
	return s.addr
}

// Start begins supervision and returns immediately.
func (s *Supervisor) Start(ctx context.Context) {
	if s == nil || !s.Available() {
		return
	}
	s.mu.Lock()
	if s.started {
		s.mu.Unlock()
		return
	}
	s.started = true
	s.done = make(chan struct{})
	s.mu.Unlock()
	go s.loop(ctx)
}

// Wait blocks until supervision ends or timeout expires.
func (s *Supervisor) Wait(timeout time.Duration) {
	if s == nil {
		return
	}
	s.mu.Lock()
	done := s.done
	s.mu.Unlock()
	if done == nil {
		return
	}
	select {
	case <-done:
	case <-time.After(timeout):
	}
}

func (s *Supervisor) loop(ctx context.Context) {
	defer close(s.done)
	backoff := s.backoffInitial
	failures := 0
	for {
		if ctx.Err() != nil {
			return
		}
		started := s.now()
		err := s.runOnce(ctx)
		uptime := s.now().Sub(started)
		if ctx.Err() != nil {
			return
		}
		if uptime >= s.healthyUptime {
			failures = 0
			backoff = s.backoffInitial
		} else {
			failures++
			if failures >= s.maxFailures {
				s.logger.Printf("WARN furrowd: %d consecutive failures; giving up (last: %v)", failures, err)
				return
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-s.after(backoff):
		}
		backoff *= 2
		if backoff > s.backoffMax {
			backoff = s.backoffMax
		}
	}
}

func (s *Supervisor) runOnce(ctx context.Context) error {
	s.manager.mu.RLock()
	remotesRoot, furrowBin := s.manager.remotesRoot, s.manager.bin
	s.manager.mu.RUnlock()
	cmd := exec.Command(s.bin)
	cmd.Env = append(os.Environ(),
		"FURROWD_ADDR="+s.addr,
		"FURROWD_REMOTES_ROOT="+remotesRoot,
		"SWE_FURROW_BIN="+furrowBin,
	)
	setDaemonProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		return err
	}
	wait := make(chan error, 1)
	go func() { wait <- cmd.Wait() }()
	select {
	case err := <-wait:
		return err
	case <-ctx.Done():
		killDaemonProcessGroup(cmd.Process.Pid)
		<-wait
		return ctx.Err()
	}
}

func envOrDefault(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
