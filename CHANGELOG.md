# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Changed

- Breaking V2 config migration: replaced layered model configuration (`preset`, grouped `models`, `model`, `*_model`, `ai_provider`) with `runtime + flat models` contract.
- Public runtime values are now `claude_code` and `open_code`.
- Runtime/model selection is accepted only inside `config` for build/execute flows.

### Removed

- Legacy configuration keys for model/provider selection (`ai_provider`, `preset`, `model`, grouped model maps, and all `*_model` request keys).

## [0.1.0] - 2026-02-16

### Added

- Initial public release of SWE-AF
- Multi-agent SWE orchestration with plan/execute/verify flow
- Docker and local execution modes
- Agent-level API endpoints and artifact persistence
- OpenCode provider support for open-source models (DeepSeek, Qwen, Llama, MiniMax via OpenRouter)
- Multiple AI provider support: Claude (Anthropic) and OpenCode (OpenRouter/OpenAI/Google)
