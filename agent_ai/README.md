# agent_ai

Provider-agnostic AI runtime for the SWE pipeline.

## Providers
- `claude`: backed by Claude Code SDK
- `codex`: backed by `codex exec` CLI

## Selection
Set provider explicitly in pipeline config:
- `BuildConfig.ai_provider`
- `ExecutionConfig.ai_provider`

Valid values: `"claude"`, `"codex"`.

A single run should use one provider end-to-end.
