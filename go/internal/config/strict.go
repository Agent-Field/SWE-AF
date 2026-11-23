package config

import (
	"os"
	"strings"
)

// EnvStrictBuild turns on the three strict build gates for plain builds:
// withholding a pull request when a branch changes no files against its base,
// running the target project's own build and test commands after verification,
// and restoring pre-existing files the repo-finalize agent removed or edited.
//
// WITH SWE_STRICT_BUILD UNSET A PLAIN BUILD BEHAVES AS ON MAIN: a zero-change
// run proceeds to the push/PR step, no project build/test gate runs, and
// finalize runs unguarded. WorkAF's build_issue sets strict on its own build
// input and always gets all three gates regardless of this switch.
const EnvStrictBuild = "SWE_STRICT_BUILD"

// StrictBuildEnabled reports whether EnvStrictBuild is truthy.
// WITH SWE_STRICT_BUILD UNSET A PLAIN BUILD BEHAVES AS ON MAIN.
func StrictBuildEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(EnvStrictBuild))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}
