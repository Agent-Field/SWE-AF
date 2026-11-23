package config

import (
	"os"
	"testing"
)

// N4: Unset, empty, 0, false and off leave SWE_STRICT_BUILD off; 1, true, yes
// and on turn it on, in any case.
func TestStrictBuildSwitch(t *testing.T) {
	// Register restore cleanup via t.Setenv, then genuinely unset the variable.
	t.Setenv(EnvStrictBuild, "1")
	if err := os.Unsetenv(EnvStrictBuild); err != nil {
		t.Fatalf("unsetenv: %v", err)
	}
	if _, set := os.LookupEnv(EnvStrictBuild); set {
		t.Fatal("SWE_STRICT_BUILD still set after Unsetenv")
	}
	if StrictBuildEnabled() {
		t.Fatal("unset SWE_STRICT_BUILD must be off")
	}

	for _, off := range []string{
		"",
		"   ",
		"0",
		" 0 ",
		"false",
		"FALSE",
		"False",
		"fAlSe",
		"off",
		"OFF",
		"Off",
		"oFf",
		"no",
		"NO",
	} {
		t.Setenv(EnvStrictBuild, off)
		if StrictBuildEnabled() {
			t.Fatalf("SWE_STRICT_BUILD=%q must be off", off)
		}
	}

	for _, on := range []string{
		"1",
		" 1 ",
		"true",
		"TRUE",
		"True",
		"tRuE",
		"yes",
		"YES",
		"Yes",
		"yEs",
		"on",
		"ON",
		"On",
		"oN",
	} {
		t.Setenv(EnvStrictBuild, on)
		if !StrictBuildEnabled() {
			t.Fatalf("SWE_STRICT_BUILD=%q must be on", on)
		}
	}
}
