# Pro engine (opt-in preview)

The Go node can run an optional high-performance coding engine, shipped as a
prebuilt binary. It is **off by default**: without the opt-in flag the node
registers exactly the same reasoner surface as before, spawns no extra
process, and every existing integration — reasoner calls, cron triggers,
`execute_fn_target` overrides — behaves identically.

## Opting in

```sh
SWE_PRO_ENGINE=1 \
SWE_PRO_BIN=/usr/local/bin/swe-pro \   # default shown
swe-planner
```

On startup the node logs an acknowledgement that the engine is enabled and how
to switch back. Two things change, both additive:

1. **Engine node.** A supervised sidecar registers on the same control plane
   as its own node (default id `swe-pro`) exposing:
   - `code_task` — run one autonomous coding task: `{goal, dir}` required,
     plus optional `high` / `low` / `frontier` (model pools), `variant`
     (reasoning effort), `hard`, `pr_ready`, `max_cost`, `max_hours`.
     Returns `{status, reason, cost_usd, run_id, elapsed_ms, ...}`; a failed
     task is reported in `status`, not as an execution error.
   - `code_resume` — re-enter a previous task in the same workspace.
2. **Seamless routing.** `build` and `execute` requests that do not name an
   `execute_fn_target` route per-issue coding through `pro_execute` on this
   node (each run carries a note saying so). Requests that pass an explicit
   `execute_fn_target` keep full control, and `pro_execute` can also be
   targeted directly from any config.

The engine never pushes or opens PRs — branch, push and PR creation stay with
the standard pipeline, so the deliverables are unchanged.

## Environment reference

| Variable | Default | Purpose |
|---|---|---|
| `SWE_PRO_ENGINE` | unset | Truthy value opts in (`1`/`true`/`yes`/`on`) |
| `SWE_PRO_BIN` | `/usr/local/bin/swe-pro` | Engine binary path |
| `SWE_PRO_NODE_ID` | `swe-pro` | Engine's control-plane node id |
| `SWE_PRO_PORT` | `8801` | Engine's listen port |
| `SWE_PRO_PUBLIC_URL` | derived | Callback base URL (containers) |
| `SWE_PRO_MAX_COST` | unset | Per-dispatch cost ceiling (USD) for `pro_execute` |
| `SWE_PRO_MODELS_HIGH` | engine default | High-tier model pool (comma-separated) |
| `SWE_PRO_MODELS_LOW` | engine default | Low-tier model pool |
| `SWE_PRO_VARIANT` | engine default | Reasoning effort (`low` = fastest) |

The engine inherits `OPENROUTER_API_KEY` and the control-plane coordinates
(`AGENTFIELD_SERVER`, `AGENTFIELD_API_KEY`) from the node's environment.

**OpenRouter-only deployments:** the compose files default
`SWE_DEFAULT_RUNTIME` to `claude_code`. The engine itself runs on OpenRouter
regardless, but the node's advisory/verification roles follow
`SWE_DEFAULT_RUNTIME` — with no Anthropic credential they fail and degrade to
accept-with-debt fallbacks. If OpenRouter is your only credential, set
`SWE_DEFAULT_RUNTIME=open_code` so every role uses the same provider.

## Rollout

The pro engine is an opt-in preview. It is planned to become the default in a
future release; at that point the classic coding loop remains available by
setting `SWE_PRO_ENGINE=0`. Existing reasoner names and input/output shapes
are stable across the swap.
