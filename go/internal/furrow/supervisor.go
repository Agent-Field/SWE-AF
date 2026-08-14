package furrow

import (
	"bytes"
	"context"
	"log"
	"net"
	"os"
	"os/exec"
	"strings"
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

	mu            sync.Mutex
	started       bool
	running       bool
	done          chan struct{}
	healthChecked time.Time
	healthy       bool
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
	return s != nil && s.manager != nil && s.manager.Enabled() && os.Getenv(EnvPublicAddr) != ""
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

// Healthy reports whether the supervised process is running and accepting TCP
// connections on its local listen address. Results are briefly cached.
func (s *Supervisor) Healthy() bool {
	if s == nil || s.bin == "" {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.running {
		return false
	}
	if s.now().Sub(s.healthChecked) < 250*time.Millisecond {
		return s.healthy
	}
	addr := s.addr
	if strings.HasPrefix(addr, ":") {
		addr = "127.0.0.1" + addr
	}
	conn, err := net.DialTimeout("tcp", addr, 200*time.Millisecond)
	if err == nil {
		_ = conn.Close()
	}
	s.healthChecked = s.now()
	s.healthy = err == nil
	return s.healthy
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
	// Without this the child's output went nowhere: a furrowd that could not
	// bind its port, or could not read its TLS key, restarted every backoff
	// interval and said nothing, until the supervisor gave up after five
	// failures with a single line naming only the exit status. The reason was
	// always on the child's stderr. Assigning an io.Writer (rather than a
	// *os.File) makes exec run its own copier and wait for it in cmd.Wait, so
	// there is no read racing the process teardown.
	stdout := &prefixWriter{logger: s.logger, prefix: "furrowd: "}
	stderr := &prefixWriter{logger: s.logger, prefix: "furrowd: "}
	cmd.Stdout, cmd.Stderr = stdout, stderr
	defer func() {
		stdout.flush()
		stderr.flush()
	}()
	setDaemonProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		return err
	}
	s.mu.Lock()
	s.running = true
	s.healthChecked = time.Time{}
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.running = false
		s.healthy = false
		s.mu.Unlock()
	}()
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

// maxPrefixLine bounds a single buffered line so a child that writes megabytes
// without a newline cannot grow the supervisor's memory without limit.
const maxPrefixLine = 64 << 10

// prefixWriter forwards a child process's output into the node's log one line
// at a time, tagged so it is attributable. exec.Cmd writes to it from its own
// copier goroutine — one per stream — and log.Logger is already safe for
// concurrent use; the mutex guards this writer's own partial-line buffer.
type prefixWriter struct {
	logger *log.Logger
	prefix string

	mu  sync.Mutex
	buf []byte
}

func (w *prefixWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf = append(w.buf, p...)
	for {
		index := bytes.IndexByte(w.buf, '\n')
		if index < 0 {
			break
		}
		w.emitLocked(w.buf[:index])
		w.buf = w.buf[index+1:]
	}
	if len(w.buf) >= maxPrefixLine {
		w.emitLocked(w.buf)
		w.buf = w.buf[:0]
	}
	return len(p), nil
}

// flush emits whatever the child left without a trailing newline. Safe to call
// once exec.Cmd's copiers have finished, which cmd.Wait guarantees.
func (w *prefixWriter) flush() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if len(w.buf) > 0 {
		w.emitLocked(w.buf)
		w.buf = w.buf[:0]
	}
}

func (w *prefixWriter) emitLocked(line []byte) {
	text := strings.TrimRight(string(line), "\r")
	if text == "" || w.logger == nil {
		return
	}
	w.logger.Printf("%s%s", w.prefix, text)
}

func envOrDefault(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
