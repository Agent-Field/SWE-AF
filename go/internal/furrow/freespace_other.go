//go:build !(linux || darwin)

package furrow

// statfsFreeBytes has no portable implementation here; reporting ok=false
// disables the free-space floor rather than inventing an answer.
func statfsFreeBytes(string) (int64, bool) { return 0, false }
