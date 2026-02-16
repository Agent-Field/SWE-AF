# agent_ai

Provider-agnostic AI runtime for the SWE pipeline.

## Providers
- `claude`: backed by Claude Code SDK (`claude_agent_sdk`)
- `codex`: backed by `codex exec` CLI
- `opencode`: backed by OpenCode ACP (`opencode acp`) via CLI invocation

## Selection
Set provider explicitly in pipeline config:
- `BuildConfig.ai_provider`
- `ExecutionConfig.ai_provider`

Valid values: `"claude"`, `"codex"`, `"opencode"`.

A single run should use one provider end-to-end.
