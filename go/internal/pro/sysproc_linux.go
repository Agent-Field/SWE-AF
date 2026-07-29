//go:build linux

package pro

import (
	"os/exec"
	"syscall"
)

// setSysProcAttr asks the kernel to SIGTERM the sidecar if the host node dies
// without running its shutdown path (log.Fatalf, OOM-kill) — the ctx-based
// cancellation in runOnce only covers orderly returns.
func setSysProcAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGTERM}
}
