//go:build linux || darwin

package furrow

import "syscall"

// statfsFreeBytes reports the space available to unprivileged writers on the
// filesystem holding path. ok is false when the probe itself fails (path
// missing, filesystem not statable) — the caller treats that as "no answer",
// not "no space".
func statfsFreeBytes(path string) (int64, bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, false
	}
	return int64(st.Bavail) * int64(st.Bsize), true
}
