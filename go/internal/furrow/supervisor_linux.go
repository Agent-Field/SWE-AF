//go:build linux

package furrow

import (
	"os/exec"
	"syscall"
)

func setDaemonProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
}

func killDaemonProcessGroup(pid int) { _ = syscall.Kill(-pid, syscall.SIGKILL) }
