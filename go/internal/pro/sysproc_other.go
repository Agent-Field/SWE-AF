//go:build !linux

package pro

import "os/exec"

// setSysProcAttr is a no-op off Linux: parent-death signals are a Linux
// feature; elsewhere the ctx cancellation in runOnce is the only kill path.
func setSysProcAttr(*exec.Cmd) {}
