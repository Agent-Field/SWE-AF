package furrow

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestSupervisorInertWithoutPublicAddr(t *testing.T) {
	m := supervisorManager(t)
	t.Setenv("FURROW_PUBLIC_ADDR", "")
	t.Setenv(EnvDaemonBin, daemonScript(t, "exit 0\n"))
	s := NewSupervisor(m)
	if s.Enabled() || s.Available() || s.Addr() != "" {
		t.Fatalf("supervisor should be inert without FURROW_PUBLIC_ADDR")
	}
	s.Start(context.Background())
}

func TestSupervisorInertWithoutDaemonBinary(t *testing.T) {
	m := supervisorManager(t)
	t.Setenv("FURROW_PUBLIC_ADDR", "mirror.example:8802")
	t.Setenv(EnvDaemonBin, filepath.Join(t.TempDir(), "missing"))
	s := NewSupervisor(m)
	if !s.Enabled() || s.Available() {
		t.Fatalf("gates = enabled %v, available %v; want true, false", s.Enabled(), s.Available())
	}
	s.Start(context.Background())
}

func TestNilSupervisorNoOps(t *testing.T) {
	var s *Supervisor
	if s.Enabled() || s.Available() || s.Addr() != "" {
		t.Fatal("nil supervisor did not report inert")
	}
	s.Start(context.Background())
	s.Wait(time.Millisecond)
}

func TestSupervisorSpawnsWithExpectedEnv(t *testing.T) {
	m := supervisorManager(t)
	out := filepath.Join(t.TempDir(), "env")
	t.Setenv("FURROW_PUBLIC_ADDR", "mirror.example:8802")
	t.Setenv("FURROWD_ADDR", "127.0.0.1:9912")
	t.Setenv("SUPERVISOR_TEST_OUT", out)
	t.Setenv("FURROWD_REMOTES_ROOT", "stale-root")
	t.Setenv(EnvBin, m.bin)
	t.Setenv(EnvDaemonBin, daemonScript(t, `
printf '%s\n%s\n%s\n' "$FURROWD_ADDR" "$FURROWD_REMOTES_ROOT" "$SWE_FURROW_BIN" > "$SUPERVISOR_TEST_OUT"
exit 0
`))
	s := NewSupervisor(m)
	s.maxFailures = 1
	s.Start(context.Background())
	s.Wait(time.Second)
	got, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	want := "127.0.0.1:9912\n" + m.remotesRoot + "\n" + m.bin + "\n"
	if string(got) != want {
		t.Fatalf("child env = %q, want %q", got, want)
	}
	if s.Addr() != "127.0.0.1:9912" {
		t.Fatalf("Addr() = %q", s.Addr())
	}
}

func TestSupervisorRestartsAfterUnexpectedExit(t *testing.T) {
	m := supervisorManager(t)
	count := filepath.Join(t.TempDir(), "count")
	t.Setenv("FURROW_PUBLIC_ADDR", "mirror.example:8802")
	t.Setenv("SUPERVISOR_TEST_COUNT", count)
	t.Setenv(EnvDaemonBin, daemonScript(t, `echo x >> "$SUPERVISOR_TEST_COUNT"; exit 1
`))
	s := NewSupervisor(m)
	s.maxFailures = 3
	s.after = immediateTimer
	s.Start(context.Background())
	s.Wait(time.Second)
	if got := lineCount(t, count); got != 3 {
		t.Fatalf("spawn count = %d, want 3", got)
	}
}

func TestSupervisorGivesUpAfterFailureThreshold(t *testing.T) {
	m := supervisorManager(t)
	count := filepath.Join(t.TempDir(), "count")
	var logs bytes.Buffer
	m.logger.SetOutput(&logs)
	t.Setenv("FURROW_PUBLIC_ADDR", "mirror.example:8802")
	t.Setenv("SUPERVISOR_TEST_COUNT", count)
	t.Setenv(EnvDaemonBin, daemonScript(t, `echo x >> "$SUPERVISOR_TEST_COUNT"; exit 2
`))
	s := NewSupervisor(m)
	s.maxFailures = 2
	s.after = immediateTimer
	s.Start(context.Background())
	s.Wait(time.Second)
	if got := lineCount(t, count); got != 2 {
		t.Fatalf("spawn count = %d, want 2", got)
	}
	if got := strings.Count(logs.String(), "giving up"); got != 1 {
		t.Fatalf("give-up log count = %d, logs %q", got, logs.String())
	}
}

func TestSupervisorCancellationKillsChildAndReturns(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX process-group assertion")
	}
	m := supervisorManager(t)
	pidFile := filepath.Join(t.TempDir(), "pid")
	t.Setenv("FURROW_PUBLIC_ADDR", "mirror.example:8802")
	t.Setenv("SUPERVISOR_TEST_PID", pidFile)
	t.Setenv(EnvDaemonBin, daemonScript(t, `echo $$ > "$SUPERVISOR_TEST_PID"; while :; do sleep 1; done
`))
	ctx, cancel := context.WithCancel(context.Background())
	s := NewSupervisor(m)
	s.Start(ctx)
	pid := waitForPID(t, pidFile)
	cancel()
	s.Wait(time.Second)
	if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
		t.Fatalf("child pid %d survived cancellation: %v", pid, err)
	}
}

func supervisorManager(t *testing.T) *Manager {
	t.Helper()
	bin := daemonScript(t, "exit 0\n")
	t.Setenv(EnvEnabled, "1")
	t.Setenv(EnvBin, bin)
	root := t.TempDir()
	return New(Options{
		Bin: bin, StoreRoot: filepath.Join(root, "store"), RemotesRoot: filepath.Join(root, "remotes"),
		Logger: log.New(io.Discard, "", 0),
	})
}

func daemonScript(t *testing.T, body string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("shell-script fake daemon")
	}
	path := filepath.Join(t.TempDir(), "furrowd")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func immediateTimer(time.Duration) <-chan time.Time {
	ch := make(chan time.Time, 1)
	ch <- time.Now()
	return ch
}

func lineCount(t *testing.T, path string) int {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return strings.Count(string(b), "\n")
}

func waitForPID(t *testing.T, path string) int {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if b, err := os.ReadFile(path); err == nil {
			pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
			if err != nil {
				t.Fatal(err)
			}
			return pid
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal(fmt.Sprintf("timed out waiting for %s", path))
	return 0
}
