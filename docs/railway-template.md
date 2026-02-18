# Deploy and Host swe-af on Railway

SWE-AF is an autonomous software engineering factory built on AgentField. Give it a goal and a repo, and it spins up a fleet of AI agents that plan, code, review, test, and ship the work as a draft PR. Supports Claude, DeepSeek, MiniMax, Qwen, and other models.

## About Hosting swe-af

SWE-AF runs as two Docker services: an AgentField control plane for orchestration, and a worker node that does the coding. The control plane accepts build requests over HTTP and dispatches them to workers. Workers clone the target repo, run agents in isolated git worktrees, and push a draft PR when finished. You need at least one AI provider API key and a GitHub token for repo access. Set your keys as environment variables in the Railway dashboard and you're good to go. Workers can be scaled horizontally for concurrent builds.

## Common Use Cases

- Autonomous feature development from a plain-English goal description
- Large codebase refactors broken into parallel, dependency-sorted issues
- On-demand PR generation triggered from CI pipelines or issue trackers

## Dependencies for swe-af Hosting

- AI provider API key (one of: `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, or `GOOGLE_API_KEY`)
- GitHub personal access token (`GH_TOKEN`) with `repo` scope for cloning and PR creation

For the best experience, we recommend using a Claude Code OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`). This lets the agent use your Claude Pro or Max subscription credits instead of direct API billing, which is significantly cheaper for heavy usage. To generate one, install the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and run `claude setup-token`. It will output a token starting with `sk-ant-oat01-` that you can paste into your Railway environment variables.

### Deployment Dependencies

- [AgentField Control Plane](https://github.com/Agent-Field/agentfield)
- [SWE-AF](https://github.com/Agent-Field/swe-af)
- [GitHub CLI](https://cli.github.com/)

### Implementation Details

Once deployed, interact with the control plane API at your Railway URL. All requests require the header `X-API-Key` set to `this-is-a-secret` (the default).

Available endpoints:

- `POST /api/v1/execute/async/swe-planner.build` — full build: plan, execute, verify, and open a draft PR
- `POST /api/v1/execute/async/swe-planner.plan` — plan only: generates PRD, architecture, and issue DAG without executing
- `POST /api/v1/execute/async/swe-planner.execute` — execute a prebuilt plan
- `POST /api/v1/execute/async/swe-planner.resume_build` — resume a build after interruption
- `GET /api/v1/executions/<execution_id>` — check execution status

The request body takes an `input` object with `goal` (what to build), `repo_url` (GitHub repo to clone and PR against), and an optional `config` for runtime and model selection. You can use `"runtime": "claude_code"` for Claude models or `"runtime": "open_code"` for open-source models via OpenRouter, OpenAI, or Google.

See the [SWE-AF README](https://github.com/Agent-Field/swe-af#api-reference) for full usage examples and config options.

## Why Deploy swe-af on Railway?

Railway is a singular platform to deploy your infrastructure stack. Railway will host your infrastructure so you don't have to deal with configuration, while allowing you to vertically and horizontally scale it.

By deploying swe-af on Railway, you are one step closer to supporting a complete full-stack application with minimal burden. Host your servers, databases, AI agents, and more on Railway.
