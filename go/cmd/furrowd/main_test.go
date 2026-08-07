package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/furrow"
)

type testServer struct {
	addr   string
	root   string
	cancel context.CancelFunc
	done   chan error
}

func startTestServer(t *testing.T, maxConns int, script string) testServer {
	t.Helper()
	root := t.TempDir()
	helper := filepath.Join(root, "furrow-helper")
	if err := os.WriteFile(helper, []byte("#!/bin/sh\n"+script+"\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	entries := map[string]furrow.Entry{"run-1": {Token: "correct-token", Namespace: "workspace"}}
	data, _ := json.Marshal(entries)
	if err := os.WriteFile(filepath.Join(root, "registry.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := config{addr: "127.0.0.1:0", root: root, cert: filepath.Join(root, "cert.pem"), key: filepath.Join(root, "key.pem"), furrowBin: helper, maxConns: maxConns}
	ctx, cancel := context.WithCancel(context.Background())
	ready := make(chan net.Addr, 1)
	done := make(chan error, 1)
	go func() { done <- run(ctx, cfg, ready) }()
	var addr net.Addr
	select {
	case addr = <-ready:
	case err := <-done:
		t.Fatalf("server failed to start: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("server did not start")
	}
	s := testServer{addr: addr.String(), root: root, cancel: cancel, done: done}
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(4 * time.Second):
			t.Error("server did not shut down")
		}
	})
	return s
}

func buildDialer(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "furrow-dial")
	if runtime.GOOS == "windows" {
		path += ".exe"
	}
	cmd := exec.Command("go", "build", "-o", path, "../furrow-dial")
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build dialer: %v\n%s", err, output)
	}
	return path
}

func dialTLS(t *testing.T, addr string) *tls.Conn {
	t.Helper()
	conn, err := tls.Dial("tcp", addr, &tls.Config{InsecureSkipVerify: true}) //nolint:gosec -- test certificate.
	if err != nil {
		t.Fatal(err)
	}
	return conn
}

func TestHappyPathRoundTripThroughDialer(t *testing.T) {
	s := startTestServer(t, 32, `printf 'MARKER:'; cat`)
	cmd := exec.Command(buildDialer(t), "-T", "-o", "BatchMode=yes", "--", s.addr, "furrow", "__remote", "workspace")
	cmd.Env = append(os.Environ(), "FURROW_DIAL_TOKEN=correct-token", "FURROW_DIAL_INSECURE=1")
	cmd.Stdin = strings.NewReader("ciphertext")
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("dialer failed: %v: %s", err, stderr.String())
	}
	if got, want := stdout.String(), "MARKER:ciphertext"; got != want {
		t.Fatalf("round trip = %q, want %q", got, want)
	}
}

// furrow never puts the human workspace name on the wire: it sends a keyed
// BLAKE3 digest of it, which the node has no way to predict or recompute. A
// connection therefore has to be accepted on the strength of its token alone,
// with the namespace passed through to the child untouched. Comparing it to the
// registry's namespace rejected every real clone.
func TestBlindedNamespaceIsAcceptedAndPassedThrough(t *testing.T) {
	s := startTestServer(t, 32, `printf 'NS:'; cat`)
	blinded := "54678c944c726f3f5e1af8a279d2a42c" // shape furrow actually sends
	cmd := exec.Command(buildDialer(t), "-T", "-o", "BatchMode=yes", "--", s.addr, "furrow", "__remote", blinded)
	cmd.Env = append(os.Environ(), "FURROW_DIAL_TOKEN=correct-token", "FURROW_DIAL_INSECURE=1")
	cmd.Stdin = strings.NewReader("payload")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("dialer failed for blinded namespace: %v: %s", err, stderr.String())
	}
	if got, want := stdout.String(), "NS:payload"; got != want {
		t.Fatalf("round trip = %q, want %q", got, want)
	}
}

// The manager records each run's store under a SANITIZED directory; the
// daemon must serve the recorded path, not one rebuilt from the raw run ID —
// a traversal-shaped ID would otherwise name a directory outside the root.
func TestChildServesRecordedStoreDirNotRawRunID(t *testing.T) {
	s := startTestServer(t, 32, `printf 'DIR:%s' "$FURROW_REMOTE_DATA_DIR"`)
	storeDir := filepath.Join(s.root, "remotes", "run")
	entries := map[string]furrow.Entry{"../escape": {Token: "dir-token", Namespace: "run", StoreDir: storeDir}}
	data, _ := json.Marshal(entries)
	if err := os.WriteFile(filepath.Join(s.root, "registry.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(buildDialer(t), "-T", "-o", "BatchMode=yes", "--", s.addr, "furrow", "__remote", "workspace")
	cmd.Env = append(os.Environ(), "FURROW_DIAL_TOKEN=dir-token", "FURROW_DIAL_INSECURE=1")
	cmd.Stdin = strings.NewReader("")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("dialer failed: %v: %s", err, stderr.String())
	}
	if got, want := stdout.String(), "DIR:"+storeDir; got != want {
		t.Fatalf("data dir = %q, want %q", got, want)
	}
}

func TestAuthenticationRejected(t *testing.T) {
	s := startTestServer(t, 32, `cat`)
	tests := []struct {
		name, line string
	}{
		{"wrong token", "AUTH wrong workspace\n"},
		{"unknown token", "AUTH absent workspace\n"},
		{"garbage", "hello\n"},
		{"oversized", strings.Repeat("x", 513) + "\n"},
		// The namespace is attacker-chosen text used to pick a directory under
		// the run's data root, so anything outside furrow's own charset —
		// especially a traversal — must never reach the child.
		{"namespace traversal", "AUTH correct-token ../../etc\n"},
		{"namespace slash", "AUTH correct-token a/b\n"},
		{"namespace dotdot", "AUTH correct-token ..\n"},
		{"namespace too long", "AUTH correct-token " + strings.Repeat("n", 97) + "\n"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			conn := dialTLS(t, s.addr)
			defer conn.Close()
			if _, err := io.WriteString(conn, tc.line); err != nil {
				t.Fatal(err)
			}
			_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
			data, _ := io.ReadAll(conn)
			if len(data) != 0 {
				t.Fatalf("rejection disclosed %q", data)
			}
		})
	}
}

func TestChildKilledWhenClientDisconnects(t *testing.T) {
	pidFile := filepath.Join(t.TempDir(), "pid")
	t.Setenv("CHILD_PID_FILE", pidFile)
	s := startTestServer(t, 32, `echo $$ > "$CHILD_PID_FILE"; trap '' TERM; while :; do sleep 1; done`)
	conn := dialTLS(t, s.addr)
	if _, err := io.WriteString(conn, "AUTH correct-token workspace\n"); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 3)
	if _, err := io.ReadFull(conn, buf); err != nil || string(buf) != "OK\n" {
		t.Fatalf("auth response %q, %v", buf, err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, err := os.Stat(pidFile); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("child did not publish pid")
		}
		time.Sleep(10 * time.Millisecond)
	}
	pids, _ := os.ReadFile(pidFile)
	var pid int
	if _, err := fmt.Sscanf(string(pids), "%d", &pid); err != nil {
		t.Fatal(err)
	}
	_ = conn.Close()
	deadline = time.Now().Add(4 * time.Second)
	for {
		err := syscall.Kill(pid, 0)
		if err == syscall.ESRCH {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("child %d remains alive", pid)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestConcurrencyLimitEnforced(t *testing.T) {
	s := startTestServer(t, 1, `cat`)
	first := dialTLS(t, s.addr)
	defer first.Close()
	if _, err := io.WriteString(first, "AUTH correct-token workspace\n"); err != nil {
		t.Fatal(err)
	}
	response := make([]byte, 3)
	if _, err := io.ReadFull(first, response); err != nil {
		t.Fatal(err)
	}
	second, err := tls.Dial("tcp", s.addr, &tls.Config{InsecureSkipVerify: true}) //nolint:gosec -- test certificate.
	if err != nil {
		return
	}
	defer second.Close()
	_ = second.SetDeadline(time.Now().Add(2 * time.Second))
	_, writeErr := io.WriteString(second, "AUTH correct-token workspace\n")
	if writeErr == nil {
		_, writeErr = io.ReadAll(second)
	}
	if writeErr == nil {
		t.Fatal("second connection was not rejected at the concurrency limit")
	}
}
