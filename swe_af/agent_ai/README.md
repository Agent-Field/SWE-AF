# agent_ai

Provider-agnostic AI runtime for the SWE pipeline.

## Providers
- `claude`: backed by Claude Code SDK (`claude_agent_sdk`)
- `opencode`: backed by OpenCode CLI (`opencode run -m model`) for 75+ LLM providers (OpenRouter, OpenAI, Google, Anthropic)

## Selection
Public pipeline config selects `runtime`:
- `claude_code` -> internal provider `claude`
- `open_code` -> internal provider `opencode`

The resolved provider is exposed to internals as:
- `BuildConfig.ai_provider`
- `ExecutionConfig.ai_provider`

A single run should use one provider end-to-end.
