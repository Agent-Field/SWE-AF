package config

import (
	"testing"
)

// providerEnvKeys are the env vars that steer runtime/model selection. Tests
// clear them (via t.Setenv to "") so results never depend on the developer's
// ambient shell — mirroring test_model_config.py's _provider_env fixture.
var providerEnvKeys = []string{
	"ANTHROPIC_API_KEY",
	"OPENROUTER_API_KEY",
	"SWE_DEFAULT_RUNTIME",
	"SWE_DEFAULT_MODEL",
	"AI_MODEL",
	"HARNESS_MODEL",
	"SWE_CODEX_AUTH_MODE",
	"OPENAI_API_KEY",
}

func clearProviderEnv(t *testing.T) {
	t.Helper()
	for _, k := range providerEnvKeys {
		t.Setenv(k, "")
	}
}

// ---------------------------------------------------------------------------
// DefaultRuntime + OpenRouter auto-selection
// ---------------------------------------------------------------------------

func TestDefaultRuntime(t *testing.T) {
	tests := []struct {
		name string
		env  map[string]string
		want string
	}{
		{"no keys -> claude_code", nil, "claude_code"},
		{"anthropic -> claude_code", map[string]string{"ANTHROPIC_API_KEY": "sk-ant"}, "claude_code"},
		{"openrouter only -> open_code", map[string]string{"OPENROUTER_API_KEY": "sk-or"}, "open_code"},
		{"both keys -> claude_code", map[string]string{"ANTHROPIC_API_KEY": "sk-ant", "OPENROUTER_API_KEY": "sk-or"}, "claude_code"},
		{"explicit runtime beats autoselect", map[string]string{"OPENROUTER_API_KEY": "sk-or", "SWE_DEFAULT_RUNTIME": "claude_code"}, "claude_code"},
		{"env open_code", map[string]string{"SWE_DEFAULT_RUNTIME": "open_code"}, "open_code"},
		{"env codex", map[string]string{"SWE_DEFAULT_RUNTIME": "codex"}, "codex"},
		{"invalid env -> claude_code", map[string]string{"SWE_DEFAULT_RUNTIME": "bogus_runtime"}, "claude_code"},
		{"empty env -> claude_code", map[string]string{"SWE_DEFAULT_RUNTIME": ""}, "claude_code"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clearProviderEnv(t)
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			if got := DefaultRuntime(); got != tc.want {
				t.Fatalf("DefaultRuntime() = %q, want %q", got, tc.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// DefaultPlanningModel
// ---------------------------------------------------------------------------

func TestDefaultPlanningModel(t *testing.T) {
	tests := []struct {
		name string
		env  map[string]string
		want string
	}{
		{"claude env -> sonnet", map[string]string{"ANTHROPIC_API_KEY": "sk-ant"}, "sonnet"},
		{"no provider env -> sonnet", nil, "sonnet"},
		{"openrouter only -> deepseek", map[string]string{"OPENROUTER_API_KEY": "sk-or"}, openRouterAutoDefaultModel},
		{"swe_default_model wins", map[string]string{"OPENROUTER_API_KEY": "sk-or", "SWE_DEFAULT_MODEL": "openrouter/qwen/qwen3-max"}, "openrouter/qwen/qwen3-max"},
		{"ai_model cascade", map[string]string{"ANTHROPIC_API_KEY": "sk-ant", "AI_MODEL": "opus"}, "opus"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clearProviderEnv(t)
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			if got := DefaultPlanningModel(); got != tc.want {
				t.Fatalf("DefaultPlanningModel() = %q, want %q", got, tc.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// ResolveRuntimeModels
// ---------------------------------------------------------------------------

func mustResolve(t *testing.T, runtime string, models map[string]string) map[string]string {
	t.Helper()
	got, err := ResolveRuntimeModels(runtime, models, nil)
	if err != nil {
		t.Fatalf("ResolveRuntimeModels(%q) unexpected error: %v", runtime, err)
	}
	return got
}

func TestResolveRuntimeModels_ClaudeCodeDefaults(t *testing.T) {
	clearProviderEnv(t)
	got := mustResolve(t, "claude_code", nil)
	for _, field := range AllModelFields {
		want := "sonnet"
		if field == "qa_synthesizer_model" {
			want = "haiku"
		}
		if got[field] != want {
			t.Errorf("field %s = %q, want %q", field, got[field], want)
		}
	}
}

func TestResolveRuntimeModels_OpenCodeDefaults(t *testing.T) {
	clearProviderEnv(t) // no provider env -> not auto-openrouter -> minimax base
	got := mustResolve(t, "open_code", nil)
	for _, field := range AllModelFields {
		if got[field] != "openrouter/minimax/minimax-m2.5" {
			t.Errorf("field %s = %q, want minimax", field, got[field])
		}
	}
}

func TestResolveRuntimeModels_OpenRouterAutoDefaults(t *testing.T) {
	clearProviderEnv(t)
	t.Setenv("OPENROUTER_API_KEY", "sk-or")
	got := mustResolve(t, "open_code", nil)
	for _, field := range AllModelFields {
		if got[field] != "openrouter/deepseek/deepseek-v4-flash" {
			t.Errorf("field %s = %q, want deepseek auto", field, got[field])
		}
	}
}

func TestResolveRuntimeModels_ExplicitOpenCodeKeepsMinimax(t *testing.T) {
	clearProviderEnv(t)
	t.Setenv("OPENROUTER_API_KEY", "sk-or")
	t.Setenv("SWE_DEFAULT_RUNTIME", "open_code")
	got := mustResolve(t, "open_code", nil)
	for _, field := range AllModelFields {
		if got[field] != "openrouter/minimax/minimax-m2.5" {
			t.Errorf("field %s = %q, want minimax (explicit)", field, got[field])
		}
	}
}

func TestResolveRuntimeModels_Precedence(t *testing.T) {
	clearProviderEnv(t)
	// models.default applies to all.
	got := mustResolve(t, "claude_code", map[string]string{"default": "opus"})
	for _, field := range AllModelFields {
		if got[field] != "opus" {
			t.Errorf("default: field %s = %q, want opus", field, got[field])
		}
	}
	// role override beats default.
	got = mustResolve(t, "claude_code", map[string]string{"default": "sonnet", "coder": "opus"})
	if got["coder_model"] != "opus" {
		t.Errorf("coder_model = %q, want opus", got["coder_model"])
	}
	if got["qa_model"] != "sonnet" {
		t.Errorf("qa_model = %q, want sonnet", got["qa_model"])
	}
}

func TestResolveRuntimeModels_EnvCascade(t *testing.T) {
	clearProviderEnv(t)
	// SWE_DEFAULT_MODEL overrides runtime base.
	t.Setenv("SWE_DEFAULT_MODEL", "openrouter/minimax/minimax-m2.6")
	got := mustResolve(t, "open_code", nil)
	for _, field := range AllModelFields {
		if got[field] != "openrouter/minimax/minimax-m2.6" {
			t.Errorf("env-default: field %s = %q", field, got[field])
		}
	}
	// caller models.default beats env.
	got = mustResolve(t, "open_code", map[string]string{"default": "openrouter/qwen/qwen-3-coder"})
	for _, field := range AllModelFields {
		if got[field] != "openrouter/qwen/qwen-3-coder" {
			t.Errorf("models.default over env: field %s = %q", field, got[field])
		}
	}
	// per-role beats env; other roles keep env.
	got = mustResolve(t, "open_code", map[string]string{"coder": "openrouter/deepseek/deepseek-v3"})
	if got["coder_model"] != "openrouter/deepseek/deepseek-v3" {
		t.Errorf("coder over env = %q", got["coder_model"])
	}
	if got["pm_model"] != "openrouter/minimax/minimax-m2.6" {
		t.Errorf("pm keeps env = %q", got["pm_model"])
	}
}

func TestResolveRuntimeModels_EnvCascadeOrder(t *testing.T) {
	clearProviderEnv(t)
	// AI_MODEL used when SWE_DEFAULT_MODEL unset.
	t.Setenv("AI_MODEL", "openrouter/moonshotai/kimi-k2.6")
	got := mustResolve(t, "open_code", nil)
	if got["pm_model"] != "openrouter/moonshotai/kimi-k2.6" {
		t.Errorf("AI_MODEL cascade = %q", got["pm_model"])
	}
	// SWE_DEFAULT_MODEL beats AI_MODEL.
	t.Setenv("SWE_DEFAULT_MODEL", "openrouter/qwen/qwen-3-coder")
	got = mustResolve(t, "open_code", nil)
	if got["pm_model"] != "openrouter/qwen/qwen-3-coder" {
		t.Errorf("SWE_DEFAULT_MODEL beats AI_MODEL = %q", got["pm_model"])
	}
}

func TestResolveRuntimeModels_EmptyEnvTreatedAsUnset(t *testing.T) {
	clearProviderEnv(t)
	t.Setenv("SWE_DEFAULT_MODEL", "   ")
	t.Setenv("AI_MODEL", "")
	t.Setenv("HARNESS_MODEL", "   ")
	got := mustResolve(t, "open_code", nil)
	for _, field := range AllModelFields {
		if got[field] != "openrouter/minimax/minimax-m2.5" {
			t.Errorf("empty env -> base: field %s = %q", field, got[field])
		}
	}
}

func TestResolveRuntimeModels_Errors(t *testing.T) {
	clearProviderEnv(t)
	if _, err := ResolveRuntimeModels("bad_runtime", nil, nil); err == nil {
		t.Fatal("expected error for invalid runtime")
	} else if err.Error() != "Unsupported runtime 'bad_runtime'. Valid runtimes: claude_code, open_code, codex" {
		t.Fatalf("runtime error string = %q", err.Error())
	}
	_, err := ResolveRuntimeModels("claude_code", map[string]string{"bad": "opus"}, nil)
	if err == nil {
		t.Fatal("expected error for unknown model key")
	}
	want := "Unknown model keys: 'bad'. Valid keys: architect, ci_fixer, code_reviewer, coder, default, git, integration_tester, issue_advisor, issue_writer, merger, pm, qa, qa_synthesizer, replan, retry_advisor, sprint_planner, tech_lead, verifier"
	if err.Error() != want {
		t.Fatalf("unknown model key error =\n%q\nwant\n%q", err.Error(), want)
	}
}

// ---------------------------------------------------------------------------
// Codex auth modes
// ---------------------------------------------------------------------------

func TestResolveRuntimeModels_Codex(t *testing.T) {
	t.Run("api_key uses -codex model", func(t *testing.T) {
		clearProviderEnv(t)
		t.Setenv("SWE_CODEX_AUTH_MODE", "api_key")
		got := mustResolve(t, "codex", nil)
		for _, field := range AllModelFields {
			if got[field] != "gpt-5.3-codex" {
				t.Errorf("api_key: field %s = %q", field, got[field])
			}
		}
	})
	t.Run("chatgpt uses non-codex model", func(t *testing.T) {
		clearProviderEnv(t)
		t.Setenv("SWE_CODEX_AUTH_MODE", "chatgpt")
		got := mustResolve(t, "codex", nil)
		for _, field := range AllModelFields {
			if got[field] != "gpt-5.5" {
				t.Errorf("chatgpt: field %s = %q", field, got[field])
			}
		}
	})
	t.Run("auto follows OPENAI_API_KEY presence", func(t *testing.T) {
		clearProviderEnv(t)
		t.Setenv("SWE_CODEX_AUTH_MODE", "auto")
		t.Setenv("OPENAI_API_KEY", "sk-test")
		if got := mustResolve(t, "codex", nil); got["coder_model"] != "gpt-5.3-codex" {
			t.Errorf("auto+key coder = %q", got["coder_model"])
		}
		t.Setenv("OPENAI_API_KEY", "")
		if got := mustResolve(t, "codex", nil); got["coder_model"] != "gpt-5.5" {
			t.Errorf("auto+nokey coder = %q", got["coder_model"])
		}
	})
	t.Run("default+role override", func(t *testing.T) {
		clearProviderEnv(t)
		got := mustResolve(t, "codex", map[string]string{"default": "gpt-5.3-codex", "coder": "gpt-5.3-codex-spark"})
		if got["coder_model"] != "gpt-5.3-codex-spark" {
			t.Errorf("coder = %q", got["coder_model"])
		}
		if got["qa_model"] != "gpt-5.3-codex" {
			t.Errorf("qa = %q", got["qa_model"])
		}
	})
}

// ---------------------------------------------------------------------------
// BuildConfig — defaults, runtime, provider
// ---------------------------------------------------------------------------

func mustLoadBuild(t *testing.T, raw map[string]any) *BuildConfig {
	t.Helper()
	cfg, err := LoadBuildConfig(raw)
	if err != nil {
		t.Fatalf("LoadBuildConfig(%v) unexpected error: %v", raw, err)
	}
	return cfg
}

func TestBuildConfig_Defaults(t *testing.T) {
	clearProviderEnv(t)
	cfg := mustLoadBuild(t, nil)
	if cfg.Runtime != "claude_code" {
		t.Errorf("runtime = %q", cfg.Runtime)
	}
	if cfg.AIProvider() != "claude" {
		t.Errorf("ai_provider = %q", cfg.AIProvider())
	}
	// spot-check numeric/bool defaults.
	checks := map[string]bool{
		"max_review_iterations=2":           cfg.MaxReviewIterations == 2,
		"max_coding_iterations=5":           cfg.MaxCodingIterations == 5,
		"agent_max_turns=150":               cfg.AgentMaxTurns == 150,
		"max_concurrent_issues=3":           cfg.MaxConcurrentIssues == 3,
		"ci_wait_seconds=1500":              cfg.CIWaitSeconds == 1500,
		"ci_poll_seconds=30":                cfg.CIPollSeconds == 30,
		"ci_startup_grace_seconds=30":       cfg.CIStartupGraceSeconds == 30,
		"agent_timeout_seconds=2700":        cfg.AgentTimeoutSeconds == 2700,
		"approval_expires_in_hours=72":      cfg.ApprovalExpiresInHours == 72,
		"max_retries_per_issue=2":           cfg.MaxRetriesPerIssue == 2,
		"git_init_max_retries=3":            cfg.GitInitMaxRetries == 3,
		"git_init_retry_delay=1.0":          cfg.GitInitRetryDelay == 1.0,
		"level_failure_abort_threshold=0.8": cfg.LevelFailureAbortThreshold == 0.8,
		"check_ci=true":                     cfg.CheckCI,
		"enable_replanning=true":            cfg.EnableReplanning,
		"enable_integration_testing=true":   cfg.EnableIntegrationTesting,
		"enable_github_pr=true":             cfg.EnableGithubPR,
		"enable_issue_advisor=true":         cfg.EnableIssueAdvisor,
		"enable_learning=false":             !cfg.EnableLearning,
		"max_ci_fix_cycles=2":               cfg.MaxCIFixCycles == 2,
		"max_advisor_invocations=2":         cfg.MaxAdvisorInvocations == 2,
		"max_plan_revision_iterations=2":    cfg.MaxPlanRevisionIterations == 2,
		"max_verify_fix_cycles=1":           cfg.MaxVerifyFixCycles == 1,
		"max_integration_test_retries=1":    cfg.MaxIntegrationTestRetries == 1,
	}
	for name, ok := range checks {
		if !ok {
			t.Errorf("default check failed: %s", name)
		}
	}
}

func TestBuildConfig_OpenCodeProvider(t *testing.T) {
	clearProviderEnv(t)
	cfg := mustLoadBuild(t, map[string]any{"runtime": "open_code"})
	if cfg.AIProvider() != "opencode" {
		t.Errorf("ai_provider = %q", cfg.AIProvider())
	}
	resolved, err := cfg.ResolvedModels()
	if err != nil {
		t.Fatal(err)
	}
	if resolved["coder_model"] != "openrouter/minimax/minimax-m2.5" {
		t.Errorf("coder_model = %q", resolved["coder_model"])
	}
}

func TestBuildConfig_AutoOpenRouterEndToEnd(t *testing.T) {
	clearProviderEnv(t)
	t.Setenv("OPENROUTER_API_KEY", "sk-or")
	cfg := mustLoadBuild(t, nil)
	if cfg.Runtime != "open_code" {
		t.Fatalf("runtime = %q, want open_code", cfg.Runtime)
	}
	resolved, err := cfg.ResolvedModels()
	if err != nil {
		t.Fatal(err)
	}
	if resolved["coder_model"] != "openrouter/deepseek/deepseek-v4-flash" {
		t.Errorf("coder_model = %q", resolved["coder_model"])
	}
}

func TestBuildConfig_EnvRuntimeOverrides(t *testing.T) {
	clearProviderEnv(t)
	t.Setenv("SWE_DEFAULT_RUNTIME", "open_code")
	if mustLoadBuild(t, nil).Runtime != "open_code" {
		t.Error("env runtime not applied")
	}
	// explicit beats env.
	if mustLoadBuild(t, map[string]any{"runtime": "claude_code"}).Runtime != "claude_code" {
		t.Error("explicit runtime should beat env")
	}
}

// ---------------------------------------------------------------------------
// BuildConfig — legacy key rejection (verbatim error strings)
// ---------------------------------------------------------------------------

func TestBuildConfig_LegacyKeyRejection(t *testing.T) {
	clearProviderEnv(t)
	tests := []struct {
		name string
		raw  map[string]any
		want string
	}{
		{"ai_provider", map[string]any{"ai_provider": "claude"},
			"Legacy config keys are not supported in V2: 'ai_provider' -> 'runtime'."},
		{"coder_model top-level", map[string]any{"coder_model": "opus"},
			"Legacy config keys are not supported in V2: 'coder_model' -> 'models.coder'."},
		{"preset", map[string]any{"preset": "fast"},
			"Legacy config keys are not supported in V2: 'preset' -> 'runtime + models'."},
		{"model", map[string]any{"model": "opus"},
			"Legacy config keys are not supported in V2: 'model' -> 'models.default'."},
		{"models.planning group", map[string]any{"models": map[string]any{"planning": "opus"}},
			"Legacy model group key 'planning' is not supported in V2. Use flat role keys: models.pm, models.architect, models.tech_lead, models.sprint_planner."},
		{"models.coding group", map[string]any{"models": map[string]any{"coding": "opus"}},
			"Legacy model group key 'coding' is not supported in V2. Use flat role keys: models.coder, models.qa, models.code_reviewer."},
		{"models.replan_model key", map[string]any{"models": map[string]any{"replan_model": "sonnet"}},
			"Legacy model key 'replan_model' is not supported in V2. Use 'models.replan'."},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := LoadBuildConfig(tc.raw)
			if err == nil {
				t.Fatalf("expected error")
			}
			if err.Error() != tc.want {
				t.Fatalf("error =\n%q\nwant\n%q", err.Error(), tc.want)
			}
		})
	}
}

func TestExecutionConfig_LegacyKeyRejection(t *testing.T) {
	clearProviderEnv(t)
	tests := []struct {
		raw  map[string]any
		want string
	}{
		{map[string]any{"ai_provider": "claude"},
			"Legacy config keys are not supported in V2: 'ai_provider' -> 'runtime'."},
		{map[string]any{"replan_model": "sonnet"},
			"Legacy config keys are not supported in V2: 'replan_model' -> 'models.replan'."},
		{map[string]any{"models": map[string]any{"coding": "opus"}},
			"Legacy model group key 'coding' is not supported in V2. Use flat role keys: models.coder, models.qa, models.code_reviewer."},
	}
	for _, tc := range tests {
		_, err := LoadExecutionConfig(tc.raw)
		if err == nil || err.Error() != tc.want {
			t.Fatalf("error = %v, want %q", err, tc.want)
		}
	}
}

// ---------------------------------------------------------------------------
// BuildConfig — unknown-field rejection (extra="forbid")
// ---------------------------------------------------------------------------

func TestBuildConfig_UnknownFieldRejected(t *testing.T) {
	clearProviderEnv(t)
	if _, err := LoadBuildConfig(map[string]any{"totally_unknown": 1}); err == nil {
		t.Fatal("expected error for unknown field")
	}
}

// ---------------------------------------------------------------------------
// BuildConfig — repo normalization
// ---------------------------------------------------------------------------

func repoSpecMap(url, role string) map[string]any {
	return map[string]any{"repo_url": url, "role": role}
}

func TestBuildConfig_RepoNormalization(t *testing.T) {
	clearProviderEnv(t)

	t.Run("single repo_url synthesises primary", func(t *testing.T) {
		cfg := mustLoadBuild(t, map[string]any{"repo_url": "https://github.com/org/repo.git"})
		if len(cfg.Repos) != 1 {
			t.Fatalf("repos len = %d", len(cfg.Repos))
		}
		if cfg.Repos[0].Role != "primary" || cfg.Repos[0].RepoURL != "https://github.com/org/repo.git" {
			t.Errorf("synthesised repo = %+v", cfg.Repos[0])
		}
		if !cfg.Repos[0].CreatePR {
			t.Error("synthesised repo create_pr should default true")
		}
		if cfg.PrimaryRepo() == nil {
			t.Error("primary_repo nil")
		}
	})

	t.Run("multi-repo backfills repo_url", func(t *testing.T) {
		cfg := mustLoadBuild(t, map[string]any{"repos": []any{
			repoSpecMap("https://github.com/org/api.git", "primary"),
			repoSpecMap("https://github.com/org/lib.git", "dependency"),
		}})
		if cfg.RepoURL != "https://github.com/org/api.git" {
			t.Errorf("repo_url backfill = %q", cfg.RepoURL)
		}
	})

	t.Run("empty allowed", func(t *testing.T) {
		cfg := mustLoadBuild(t, nil)
		if len(cfg.Repos) != 0 || cfg.RepoURL != "" {
			t.Error("empty build should have no repos")
		}
		if cfg.PrimaryRepo() != nil {
			t.Error("primary_repo should be nil for empty")
		}
	})

	errTests := []struct {
		name string
		raw  map[string]any
		want string
	}{
		{"both repo_url and repos", map[string]any{
			"repo_url": "https://github.com/org/a.git",
			"repos":    []any{repoSpecMap("https://github.com/org/b.git", "primary")},
		}, "Specify either 'repo_url' (single-repo shorthand) or 'repos' (multi-repo list), not both."},
		{"two primaries", map[string]any{"repos": []any{
			repoSpecMap("https://github.com/org/a.git", "primary"),
			repoSpecMap("https://github.com/org/b.git", "primary"),
		}}, "Exactly one RepoSpec with role='primary' is required; found 2."},
		{"no primary", map[string]any{"repos": []any{
			repoSpecMap("https://github.com/org/lib.git", "dependency"),
		}}, "Exactly one RepoSpec with role='primary' is required; found 0."},
		{"duplicate url", map[string]any{"repos": []any{
			repoSpecMap("https://github.com/org/myrepo.git", "primary"),
			repoSpecMap("https://github.com/org/myrepo.git", "dependency"),
		}}, "Duplicate repo_url values are not allowed in 'repos'."},
		{"invalid role", map[string]any{"repos": []any{
			repoSpecMap("https://github.com/org/a.git", "invalid"),
		}}, "role must be 'primary' or 'dependency', got 'invalid'"},
		{"invalid url", map[string]any{"repos": []any{
			repoSpecMap("not-a-valid-url", "primary"),
		}}, "repo_url must be an HTTP(S) or SSH git URL, got 'not-a-valid-url'"},
		{"invalid url via shorthand", map[string]any{"repo_url": "not-a-valid-url"},
			"repo_url must be an HTTP(S) or SSH git URL, got 'not-a-valid-url'"},
	}
	for _, tc := range errTests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := LoadBuildConfig(tc.raw)
			if err == nil || err.Error() != tc.want {
				t.Fatalf("error = %v, want %q", err, tc.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// BuildConfig — ToExecutionConfigDict round-trip
// ---------------------------------------------------------------------------

func TestBuildConfig_ToExecutionConfigDictRoundtrip(t *testing.T) {
	clearProviderEnv(t)
	cfg := mustLoadBuild(t, map[string]any{
		"runtime": "open_code",
		"models":  map[string]any{"coder": "deepseek/deepseek-chat"},
	})
	d := cfg.ToExecutionConfigDict()
	if d["runtime"] != "open_code" {
		t.Errorf("dict runtime = %v", d["runtime"])
	}
	// max_retries_per_issue is forwarded from BuildConfig's 2 (not EC's default 1).
	if d["max_retries_per_issue"] != 2 {
		t.Errorf("dict max_retries_per_issue = %v, want 2", d["max_retries_per_issue"])
	}
	execCfg, err := LoadExecutionConfig(d)
	if err != nil {
		t.Fatal(err)
	}
	if execCfg.CoderModel() != "deepseek/deepseek-chat" {
		t.Errorf("exec coder_model = %q", execCfg.CoderModel())
	}
	if execCfg.QAModel() != "openrouter/minimax/minimax-m2.5" {
		t.Errorf("exec qa_model = %q", execCfg.QAModel())
	}
	if execCfg.MaxRetriesPerIssue != 2 {
		t.Errorf("exec max_retries_per_issue = %d, want 2", execCfg.MaxRetriesPerIssue)
	}
}

// ---------------------------------------------------------------------------
// ExecutionConfig — defaults + accessors
// ---------------------------------------------------------------------------

func mustLoadExec(t *testing.T, raw map[string]any) *ExecutionConfig {
	t.Helper()
	cfg, err := LoadExecutionConfig(raw)
	if err != nil {
		t.Fatalf("LoadExecutionConfig(%v) unexpected error: %v", raw, err)
	}
	return cfg
}

func TestExecutionConfig_DefaultResolution(t *testing.T) {
	clearProviderEnv(t)
	cfg := mustLoadExec(t, nil)
	if cfg.Runtime != "claude_code" {
		t.Errorf("runtime = %q", cfg.Runtime)
	}
	if cfg.AIProvider() != "claude" {
		t.Errorf("ai_provider = %q", cfg.AIProvider())
	}
	if cfg.CoderModel() != "sonnet" {
		t.Errorf("coder_model = %q", cfg.CoderModel())
	}
	if cfg.QASynthesizerModel() != "haiku" {
		t.Errorf("qa_synthesizer_model = %q", cfg.QASynthesizerModel())
	}
	// EC's own default divergence: max_retries_per_issue = 1.
	if cfg.MaxRetriesPerIssue != 1 {
		t.Errorf("max_retries_per_issue = %d, want 1 (EC default)", cfg.MaxRetriesPerIssue)
	}
}

func TestExecutionConfig_AllRoleKeysResolve(t *testing.T) {
	clearProviderEnv(t)
	models := map[string]any{}
	for role := range RoleToModelField {
		models[role] = "model-" + role
	}
	cfg := mustLoadExec(t, map[string]any{"runtime": "open_code", "models": models})
	accessors := map[string]func() string{
		"pm_model": cfg.PMModel, "architect_model": cfg.ArchitectModel, "tech_lead_model": cfg.TechLeadModel,
		"sprint_planner_model": cfg.SprintPlannerModel, "coder_model": cfg.CoderModel, "qa_model": cfg.QAModel,
		"code_reviewer_model": cfg.CodeReviewerModel, "qa_synthesizer_model": cfg.QASynthesizerModel,
		"replan_model": cfg.ReplanModel, "retry_advisor_model": cfg.RetryAdvisorModel,
		"issue_writer_model": cfg.IssueWriterModel, "issue_advisor_model": cfg.IssueAdvisorModel,
		"verifier_model": cfg.VerifierModel, "git_model": cfg.GitModel, "merger_model": cfg.MergerModel,
		"integration_tester_model": cfg.IntegrationTesterModel, "ci_fixer_model": cfg.CIFixerModel,
	}
	for field, accessor := range accessors {
		role := modelFieldToRole[field]
		want := "model-" + role
		if accessor() != want {
			t.Errorf("%s = %q, want %q", field, accessor(), want)
		}
	}
}

func TestExecutionConfig_CIFixerRole(t *testing.T) {
	clearProviderEnv(t)
	if mustLoadExec(t, map[string]any{"runtime": "claude_code"}).CIFixerModel() != "sonnet" {
		t.Error("ci_fixer default claude")
	}
	if mustLoadExec(t, map[string]any{"runtime": "open_code"}).CIFixerModel() != "openrouter/minimax/minimax-m2.5" {
		t.Error("ci_fixer default opencode")
	}
	cfg := mustLoadExec(t, map[string]any{"runtime": "claude_code", "models": map[string]any{"ci_fixer": "opus"}})
	if cfg.CIFixerModel() != "opus" {
		t.Error("ci_fixer override")
	}
	if cfg.CoderModel() != "sonnet" {
		t.Error("other roles untouched")
	}
}

func TestExecutionConfig_CIGateCapsRoundTrip(t *testing.T) {
	clearProviderEnv(t)
	if !mustLoadExec(t, nil).CheckCI {
		t.Error("check_ci should default true")
	}
	cfg := mustLoadExec(t, map[string]any{
		"check_ci": false, "max_ci_fix_cycles": 5, "ci_wait_seconds": 600, "ci_poll_seconds": 15,
	})
	if cfg.CheckCI || cfg.MaxCIFixCycles != 5 || cfg.CIWaitSeconds != 600 || cfg.CIPollSeconds != 15 {
		t.Errorf("ci caps = %+v", cfg)
	}
}

func TestExecutionConfig_EnvRuntime(t *testing.T) {
	clearProviderEnv(t)
	t.Setenv("SWE_DEFAULT_RUNTIME", "codex")
	t.Setenv("SWE_CODEX_AUTH_MODE", "api_key")
	cfg := mustLoadExec(t, nil)
	if cfg.Runtime != "codex" || cfg.AIProvider() != "codex" {
		t.Errorf("runtime=%q provider=%q", cfg.Runtime, cfg.AIProvider())
	}
	if cfg.CoderModel() != "gpt-5.3-codex" {
		t.Errorf("coder = %q", cfg.CoderModel())
	}
}

func TestExecutionConfig_ClaudeAliasNormalized(t *testing.T) {
	clearProviderEnv(t)
	// _normalize_provider_field maps legacy "claude" -> "claude_code".
	cfg := mustLoadExec(t, map[string]any{"runtime": "claude"})
	if cfg.Runtime != "claude_code" {
		t.Errorf("runtime = %q, want claude_code", cfg.Runtime)
	}
}

// ---------------------------------------------------------------------------
// FastBuildConfig
// ---------------------------------------------------------------------------

func TestDefaultFastRuntime(t *testing.T) {
	tests := []struct {
		name string
		env  map[string]string
		set  bool
		want string
	}{
		{"unset -> claude_code", nil, false, "claude_code"},
		{"empty -> claude_code", map[string]string{"SWE_DEFAULT_RUNTIME": ""}, true, "claude_code"},
		{"open_code", map[string]string{"SWE_DEFAULT_RUNTIME": "open_code"}, true, "open_code"},
		{"invalid -> claude_code", map[string]string{"SWE_DEFAULT_RUNTIME": "bogus"}, true, "claude_code"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clearProviderEnv(t)
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			if got := DefaultFastRuntime(); got != tc.want {
				t.Fatalf("DefaultFastRuntime() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestFastBuildConfig_Defaults(t *testing.T) {
	clearProviderEnv(t)
	cfg, err := LoadFastBuildConfig(nil)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Runtime != "claude_code" || cfg.MaxTasks != 10 || cfg.TaskTimeoutSeconds != 300 ||
		cfg.BuildTimeoutSeconds != 600 || !cfg.EnableGithubPR || cfg.AgentMaxTurns != 50 {
		t.Errorf("fast defaults = %+v", cfg)
	}
}

func TestFastBuildConfig_UnknownFieldRejected(t *testing.T) {
	clearProviderEnv(t)
	if _, err := LoadFastBuildConfig(map[string]any{"nope": 1}); err == nil {
		t.Fatal("expected unknown-field error")
	}
}

func TestFastResolveModels(t *testing.T) {
	clearProviderEnv(t)

	t.Run("claude_code default", func(t *testing.T) {
		cfg, _ := LoadFastBuildConfig(map[string]any{"runtime": "claude_code"})
		got, err := FastResolveModels(cfg)
		if err != nil {
			t.Fatal(err)
		}
		for _, role := range fastRoles {
			if got[role] != "haiku" {
				t.Errorf("%s = %q, want haiku", role, got[role])
			}
		}
	})

	t.Run("open_code default", func(t *testing.T) {
		cfg, _ := LoadFastBuildConfig(map[string]any{"runtime": "open_code"})
		got, _ := FastResolveModels(cfg)
		for _, role := range fastRoles {
			if got[role] != "qwen/qwen-2.5-coder-32b-instruct" {
				t.Errorf("%s = %q", role, got[role])
			}
		}
	})

	t.Run("default and per-role override", func(t *testing.T) {
		cfg, _ := LoadFastBuildConfig(map[string]any{
			"runtime": "claude_code",
			"models":  map[string]any{"default": "sonnet", "coder": "opus"},
		})
		got, _ := FastResolveModels(cfg)
		if got["coder_model"] != "opus" {
			t.Errorf("coder_model = %q", got["coder_model"])
		}
		if got["pm_model"] != "sonnet" {
			t.Errorf("pm_model = %q", got["pm_model"])
		}
	})

	t.Run("codex auth mode", func(t *testing.T) {
		t.Setenv("SWE_CODEX_AUTH_MODE", "api_key")
		cfg, _ := LoadFastBuildConfig(map[string]any{"runtime": "codex"})
		got, _ := FastResolveModels(cfg)
		if got["coder_model"] != "gpt-5.3-codex" {
			t.Errorf("codex coder = %q", got["coder_model"])
		}
	})

	t.Run("unknown role key error", func(t *testing.T) {
		cfg, _ := LoadFastBuildConfig(map[string]any{"models": map[string]any{"bad": "x"}})
		_, err := FastResolveModels(cfg)
		want := "Unknown role key 'bad' in models dict. Valid keys are: ['coder', 'default', 'git', 'pm', 'verifier']"
		if err == nil || err.Error() != want {
			t.Fatalf("error = %v, want %q", err, want)
		}
	})
}
