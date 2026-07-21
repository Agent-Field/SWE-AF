package config

import (
	"strings"
	"testing"
)

func TestIssueBuildConfigDefaults(t *testing.T) {
	cfg, err := LoadIssueBuildConfig(nil)
	if err != nil {
		t.Fatalf("LoadIssueBuildConfig(nil): %v", err)
	}
	if cfg.MaxCodingIterations != 3 {
		t.Errorf("MaxCodingIterations = %d, want 3", cfg.MaxCodingIterations)
	}
	if cfg.AgentMaxTurns != 50 {
		t.Errorf("AgentMaxTurns = %d, want 50", cfg.AgentMaxTurns)
	}
	if cfg.AgentTimeoutSeconds != 1800 {
		t.Errorf("AgentTimeoutSeconds = %d, want 1800", cfg.AgentTimeoutSeconds)
	}
	if !cfg.Verify {
		t.Error("Verify default should be true")
	}
	if cfg.EnableGithubPR {
		t.Error("EnableGithubPR default should be false")
	}
	if cfg.BranchPrefix != "issue/" {
		t.Errorf("BranchPrefix = %q", cfg.BranchPrefix)
	}
	if cfg.KeepWorktree {
		t.Error("KeepWorktree default should be false")
	}
}

func TestIssueBuildConfigModelKeyValidation(t *testing.T) {
	if _, err := LoadIssueBuildConfig(map[string]any{
		"models": map[string]any{"default": "haiku", "coder": "sonnet"},
	}); err != nil {
		t.Errorf("valid model keys rejected: %v", err)
	}
	_, err := LoadIssueBuildConfig(map[string]any{
		"models": map[string]any{"pm": "haiku"},
	})
	if err == nil || !strings.Contains(err.Error(), "Unknown model keys") {
		t.Errorf("unknown model key accepted: %v", err)
	}
}

func TestIssueBuildConfigStrictDecode(t *testing.T) {
	if _, err := LoadIssueBuildConfig(map[string]any{"max_tasks": 5}); err == nil {
		t.Error("unknown config key accepted (extra=forbid)")
	}
}

func TestIssueBuildConfigToExecutionRaw(t *testing.T) {
	cfg, err := LoadIssueBuildConfig(map[string]any{
		"runtime":               "claude_code",
		"models":                map[string]any{"default": "haiku", "coder": "sonnet"},
		"max_coding_iterations": 2,
	})
	if err != nil {
		t.Fatalf("LoadIssueBuildConfig: %v", err)
	}
	execCfg, err := LoadExecutionConfig(cfg.ToExecutionRaw())
	if err != nil {
		t.Fatalf("LoadExecutionConfig(ToExecutionRaw): %v", err)
	}
	if execCfg.MaxCodingIterations != 2 {
		t.Errorf("MaxCodingIterations = %d", execCfg.MaxCodingIterations)
	}
	if execCfg.CoderModel() != "sonnet" {
		t.Errorf("CoderModel = %q", execCfg.CoderModel())
	}
	if execCfg.CodeReviewerModel() != "haiku" {
		t.Errorf("CodeReviewerModel = %q", execCfg.CodeReviewerModel())
	}
	if execCfg.EnableIssueAdvisor || execCfg.EnableReplanning || execCfg.CheckCI ||
		execCfg.EnableIntegrationTesting || execCfg.EnableLearning {
		t.Error("issue-level execution config must disable advisor/replanning/CI/integration/learning")
	}
}
