//go:build !unix

package furrow

import (
	"os"
	"os/exec"
)

func setDaemonProcessGroup(*exec.Cmd) {}

func killDaemonProcessGroup(pid int) {
	if process, err := os.FindProcess(pid); err == nil {
		_ = process.Kill()
	}
}
