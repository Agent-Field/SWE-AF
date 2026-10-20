package config

import (
	"strings"
	"testing"
)

func TestAforgeGatewayPrecedence(t *testing.T) {
	for _, otherKeys := range [][]string{
		nil, {"ANTHROPIC_API_KEY"}, {"INFRON_API_KEY"}, {"ANTHROPIC_API_KEY", "INFRON_API_KEY"},
	} {
		t.Run("OpenRouter with "+strings.Join(otherKeys, ","), func(t *testing.T) {
			clearProviderEnv(t)
			t.Setenv("OPENROUTER_API_KEY", "test-openrouter")
			t.Setenv("SWE_DEFAULT_RUNTIME", " \t")
			for _, key := range otherKeys {
				t.Setenv(key, "test-key")
			}
			build := mustLoadBuild(t, nil)
			exec := mustLoadExec(t, nil)
			fast, err := LoadFastBuildConfig(nil)
			if err != nil {
				t.Fatal(err)
			}
			if build.Runtime != "aforge" || exec.Runtime != "aforge" || fast.Runtime != "aforge" {
				t.Fatalf("runtimes: build=%s exec=%s fast=%s", build.Runtime, exec.Runtime, fast.Runtime)
			}
			if build.AIProvider() != "aforge" || exec.AIProvider() != "aforge" {
				t.Fatal("AForge configs must dispatch to the AForge provider")
			}
			models, err := FastResolveModels(fast)
			if err != nil {
				t.Fatal(err)
			}
			if exec.CoderModel() != openRouterAutoDefaultModel || models["coder_model"] != openRouterAutoDefaultModel {
				t.Fatal("AForge default must retain the OpenRouter model")
			}
		})
	}
}

func TestAforgeRuntimeOverrides(t *testing.T) {
	for _, runtime := range []string{"aforge", "claude_code", "open_code", "codex"} {
		t.Run(runtime, func(t *testing.T) {
			clearProviderEnv(t)
			for _, key := range []string{"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "INFRON_API_KEY"} {
				t.Setenv(key, "test-key")
			}
			t.Setenv("SWE_DEFAULT_RUNTIME", runtime)
			if DefaultRuntime() != runtime || DefaultFastRuntime() != runtime {
				t.Fatal("deployment runtime must beat gateway auto-selection")
			}
			t.Setenv("SWE_DEFAULT_RUNTIME", "aforge")
			raw := map[string]any{"runtime": runtime}
			build := mustLoadBuild(t, raw)
			exec := mustLoadExec(t, raw)
			fast, err := LoadFastBuildConfig(raw)
			if err != nil {
				t.Fatal(err)
			}
			if build.Runtime != runtime || exec.Runtime != runtime || fast.Runtime != runtime {
				t.Fatal("request runtime must beat the deployment runtime")
			}
		})
	}
}

func TestAforgeModelOverridesAndHarnessIsolation(t *testing.T) {
	clearProviderEnv(t)
	t.Setenv("HARNESS_MODEL", "infron/opencode-only")
	t.Setenv("INFRON_API_KEY", "test-infron")
	models := mustResolve(t, "aforge", nil)
	fast, err := FastResolveModels(&FastBuildConfig{Runtime: "aforge"})
	if err != nil {
		t.Fatal(err)
	}
	if models["pm_model"] != openRouterAutoDefaultModel || fast["pm_model"] != openRouterAutoDefaultModel {
		t.Fatal("OpenCode and Infron settings must not change AForge defaults")
	}
	t.Setenv("SWE_DEFAULT_MODEL", "deployment-model")
	t.Setenv("SWE_MODEL_HIGH", "planning-model")
	models = mustResolve(t, "aforge", map[string]string{"coder": "caller-model"})
	if models["pm_model"] != "planning-model" || models["coder_model"] != "caller-model" || models["qa_model"] != "deployment-model" {
		t.Fatalf("model precedence: %v", models)
	}
}
