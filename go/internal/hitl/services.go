package hitl

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// DetectServicesFromRepo is a deterministic pre-pass for run_environment_scout:
// it returns the subset of schemas.KnownServices whose signal files exist under
// repoPath. Ports services.py::detect_services_from_repo.
//
// Notes (verbatim from the Python contract):
//   - No recursive glob; each signal_file is checked as a path under repoPath.
//     A signal_file may be a file OR a directory; both count as a hit.
//   - Returns an empty list when repoPath is blank or not a directory (never
//     raises).
//   - Order matches schemas.KnownServices so callers get stable output.
func DetectServicesFromRepo(repoPath string) []schemas.ServiceCredentialSpec {
	if repoPath == "" {
		return nil
	}
	info, err := os.Stat(repoPath)
	if err != nil || !info.IsDir() {
		return nil
	}
	var hits []schemas.ServiceCredentialSpec
	for _, spec := range schemas.KnownServices {
		for _, signal := range spec.SignalFiles {
			candidate := filepath.Join(repoPath, signal)
			if _, err := os.Stat(candidate); err == nil {
				hits = append(hits, spec)
				break
			}
		}
	}
	return hits
}

// KnownServiceSummaryForPrompt renders a markdown bullet list of service specs
// for inclusion in the scout prompt. Ports
// services.py::known_service_summary_for_prompt verbatim (character-for-character
// f-string parity).
func KnownServiceSummaryForPrompt(specs []schemas.ServiceCredentialSpec) string {
	lines := make([]string, 0, len(specs))
	for _, spec := range specs {
		parts := make([]string, 0, len(spec.SignalFiles))
		for _, s := range spec.SignalFiles {
			parts = append(parts, "`"+s+"`")
		}
		signals := strings.Join(parts, ", ")
		if signals == "" {
			signals = "(no static signal)"
		}
		lines = append(lines, fmt.Sprintf(
			"- **%s** — env `%s`; signals: %s; mint at %s; hint: %s",
			spec.ServiceName, spec.EnvVarName, signals, spec.MintURL, spec.PermissionsHint,
		))
	}
	return strings.Join(lines, "\n")
}
