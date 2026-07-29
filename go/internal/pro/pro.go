// Package pro is the opt-in "pro engine" integration: a prebuilt coding-engine
// binary shipped alongside SWE-AF that registers on the same control plane as
// its own node and can take over per-issue coding work.
//
// Everything in this package is inert unless SWE_PRO_ENGINE is set to a truthy
// value: no child process is spawned, no reasoner is registered, and the
// default swe-planner surface (and its parity test) is byte-identical to a
// build without this package.
//
// Two surfaces, mirroring the two ways SWE-AF itself is used:
//
//   - Supervisor (this file): spawns `<bin> serve` as a sidecar so the engine
//     registers its own node (default "swe-pro") with its native reasoners —
//     the sub-harness surface other reasoners call directly.
//   - pro_execute (adapter.go): an execute_fn_target-compatible reasoner on the
//     planner node that routes one issue's coding to the engine, so a normal
//     `build` opts in per request via the existing config key with no schema
//     changes.
package pro

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"strings"
	"time"
)

// Env var surface. SWE-AF-side names only; the supervisor translates them to
// the engine's own env contract in childEnv so callers never see engine names.
const (
	// EnvEnabled gates the whole package: "1"/"true"/"yes"/"on" enable it.
	EnvEnabled = "SWE_PRO_ENGINE"
	// EnvBin overrides the engine binary path.
	EnvBin = "SWE_PRO_BIN"
	// EnvNodeID overrides the engine's control-plane node id.
	EnvNodeID = "SWE_PRO_NODE_ID"
	// EnvPort overrides the engine's listen port.
	EnvPort = "SWE_PRO_PORT"
	// EnvPublicURL sets the engine's callback base URL (containers), mirroring
	// AGENT_CALLBACK_URL on the SWE-AF nodes.
	EnvPublicURL = "SWE_PRO_PUBLIC_URL"
	// EnvMaxCost, when set, is forwarded as the engine's per-run cost ceiling
	// (USD) on every pro_execute dispatch.
	EnvMaxCost = "SWE_PRO_MAX_COST"

	DefaultBin    = "/usr/local/bin/swe-pro"
	DefaultNodeID = "swe-pro"
	DefaultPort   = "8801"
)

// Restart policy. Vars, not consts, so tests can tighten them.
var (
	backoffInitial = time.Second
	backoffMax     = 30 * time.Second
	// healthyUptime is the run length after which the backoff and the
	// fast-crash counter reset — the sidecar was evidently serving.
	healthyUptime = 60 * time.Second
	// maxFastCrashes stops the restart loop after this many consecutive
	// short-lived exits: a binary that can never start (bad glibc, bad arch,
	// port taken) should log and give up, not spin forever.
	maxFastCrashes = 10
)

// Enabled reports whether the pro engine is opted in via SWE_PRO_ENGINE.
func Enabled() bool {
	switch strings.ToLower(os.Getenv(EnvEnabled)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// BinPath returns the engine binary path (SWE_PRO_BIN or the default).
func BinPath() string { return envOr(EnvBin, DefaultBin) }

// NodeID returns the engine's control-plane node id (SWE_PRO_NODE_ID or the
// default). The adapter dispatches to "<NodeID()>.<reasoner>".
func NodeID() string { return envOr(EnvNodeID, DefaultNodeID) }

// Port returns the engine's listen port (SWE_PRO_PORT or the default).
func Port() string { return envOr(EnvPort, DefaultPort) }

// Options carries the control-plane coordinates the sidecar inherits from the
// host node — the same values node.BuildAgent resolved from the environment.
type Options struct {
	// Server is the control-plane base URL (AGENTFIELD_SERVER).
	Server string
	// Token is the control-plane bearer token (AGENTFIELD_API_KEY); may be "".
	Token string
	// Bin overrides the engine binary path; empty means BinPath().
	Bin string
	// Stdout/Stderr receive the sidecar's prefixed output; nil means the
	// process's own streams. Test seams.
	Stdout, Stderr io.Writer
}

// Supervisor owns the sidecar process lifecycle: spawn, restart with backoff,
// stop on context cancellation.
type Supervisor struct {
	done chan struct{}
}

// Start launches the engine sidecar under supervision and returns immediately.
// A missing binary is a warning, not an error — the host node must come up
// regardless — so Start returns nil in that case (and Wait on nil is a no-op).
// Cancel ctx to stop the sidecar; then Wait for the loop to wind down.
func Start(ctx context.Context, opts Options) *Supervisor {
	bin := opts.Bin
	if bin == "" {
		bin = BinPath()
	}
	if _, err := os.Stat(bin); err != nil {
		log.Printf("pro engine: binary not found at %s — sidecar disabled (%v)", bin, err)
		return nil
	}
	if opts.Stdout == nil {
		opts.Stdout = os.Stdout
	}
	if opts.Stderr == nil {
		opts.Stderr = os.Stderr
	}
	s := &Supervisor{done: make(chan struct{})}
	go s.loop(ctx, bin, opts)
	return s
}

// Wait blocks until the supervision loop has exited (after ctx cancellation or
// give-up), or until timeout. Safe on a nil Supervisor.
func (s *Supervisor) Wait(timeout time.Duration) {
	if s == nil {
		return
	}
	select {
	case <-s.done:
	case <-time.After(timeout):
	}
}

func (s *Supervisor) loop(ctx context.Context, bin string, opts Options) {
	defer close(s.done)
	backoff := backoffInitial
	fastCrashes := 0
	for {
		if ctx.Err() != nil {
			return
		}
		start := time.Now()
		err := runOnce(ctx, bin, opts)
		uptime := time.Since(start)
		if ctx.Err() != nil {
			return
		}
		if uptime >= healthyUptime {
			backoff = backoffInitial
			fastCrashes = 0
		} else {
			fastCrashes++
			if fastCrashes >= maxFastCrashes {
				log.Printf("pro engine: %d consecutive fast exits — giving up (last: %v)", fastCrashes, err)
				return
			}
		}
		log.Printf("pro engine: sidecar exited after %s (%v) — restarting in %s",
			uptime.Round(time.Second), err, backoff)
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff *= 2; backoff > backoffMax {
			backoff = backoffMax
		}
	}
}

// runOnce runs one `<bin> serve` process to completion. Cancellation sends
// SIGINT (the engine shuts its node down cleanly), escalating to SIGKILL after
// WaitDelay.
func runOnce(ctx context.Context, bin string, opts Options) error {
	cmd := exec.CommandContext(ctx, bin, "serve")
	cmd.Env = childEnv(os.Environ(), opts)
	cmd.Cancel = func() error { return cmd.Process.Signal(os.Interrupt) }
	cmd.WaitDelay = 5 * time.Second
	setSysProcAttr(cmd)

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	copied := make(chan struct{}, 2)
	go func() { pipeLines(opts.Stdout, stdout); copied <- struct{}{} }()
	go func() { pipeLines(opts.Stderr, stderr); copied <- struct{}{} }()
	<-copied
	<-copied
	return cmd.Wait()
}

// childEnv builds the sidecar environment: the parent env (so provider keys
// like OPENROUTER_API_KEY pass through) plus the engine's own env surface
// derived from the SWE-AF-side names. os/exec keeps the LAST value for a
// duplicated key, so the appended entries win over anything inherited.
func childEnv(base []string, opts Options) []string {
	env := append([]string(nil), base...)
	env = append(env,
		"AGENTFIELD_URL="+opts.Server,
		"AGENT_NODE_ID="+NodeID(),
		"AGENT_LISTEN_ADDR=:"+Port(),
	)
	if opts.Token != "" {
		env = append(env, "AGENTFIELD_TOKEN="+opts.Token)
	}
	if pub := os.Getenv(EnvPublicURL); pub != "" {
		env = append(env, "AGENT_PUBLIC_URL="+pub)
	}
	return env
}

// pipeLines copies r to w line by line under a "[pro-engine]" prefix so the
// sidecar's output is attributable in the host node's logs.
func pipeLines(w io.Writer, r io.Reader) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		fmt.Fprintf(w, "[pro-engine] %s\n", sc.Text())
	}
}

// envOr returns the value of key, or def when unset or empty.
func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
