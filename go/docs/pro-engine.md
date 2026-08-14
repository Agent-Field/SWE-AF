# Pro engine (beta)

The Go node runs a high-performance coding engine, shipped as prebuilt
binaries — one per supported platform, vendored at `go/bin` as
`swe-pro-darwin-arm64` and `swe-pro-linux-amd64`, because one checkout is
installed on macOS and Linux alike and the node picks the matching build at
startup.

It is **on by default for nodes installed with `af install` or AgentField
Desktop**: `agentfield-package.yaml` declares `SWE_PRO_ENGINE` with
`default: "1"`, and the installer's env resolver injects that into the node
process. Turning it off is a one-variable change and every existing
integration — reasoner calls, cron triggers, `execute_fn_target` overrides —
behaves identically either way.

**Everywhere else it is off**, because the gate is purely the env var and
nothing but the installer sets it: a bare `swe-planner`, a clone or fork you
run yourself, and the compose stacks (`docker-compose.go.yml` passes
`SWE_PRO_ENGINE` through but leaves it empty) all start on the classic loop
until you set `SWE_PRO_ENGINE=1`.

This is a beta: it is the coding path we are actively developing, and rough
edges are expected. `SWE_PRO_ENGINE=0` is always one restart away.

## Opting out

```sh
SWE_PRO_ENGINE=0 swe-planner        # classic coder → reviewer/QA loop
```

`0`, `false`, `off`, and any other non-truthy value disable it; only
`1`/`true`/`yes`/`on` (case-insensitive) enable it.

## Opting back in explicitly

```sh
SWE_PRO_ENGINE=1 \
SWE_PRO_BIN=/usr/local/bin/swe-pro \   # default shown
swe-planner
```

On startup the node logs an acknowledgement that the engine is enabled and how
to switch back. Two things differ from the classic loop, both additive:

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

If the flag is set but no *runnable* engine binary is found — missing, or
present without its execute bit — the node logs a warning naming the path and
comes up on the classic coding loop: `pro_execute` is not registered and
nothing is routed to an engine node that never joined. The binary is searched
for at `SWE_PRO_BIN` when set (authoritative — no fallback), else
`/usr/local/bin/swe-pro` (the Docker image layout: one image, one platform, so
the image build copies its own `swe-pro-linux-amd64` to that path), else next
to the running executable — the layout an `af install` checkout produces —
first as `swe-pro-<GOOS>-<GOARCH>` and then as plain `swe-pro`. The suffix is
what keeps a macOS install from exec'ing the Linux build.

## Environment reference

| Variable | Default | Purpose |
|---|---|---|
| `SWE_PRO_ENGINE` | `1` via the manifest on `af install` / Desktop; unset (off) for a clone, fork, compose stack or bare binary | Truthy (`1`/`true`/`yes`/`on`) enables; `0`/`false` opts out |
| `SWE_PRO_BIN` | `/usr/local/bin/swe-pro`, else a `swe-pro-<GOOS>-<GOARCH>` / `swe-pro` sibling | Engine binary path (authoritative when set) |
| `SWE_PRO_NODE_ID` | `swe-pro` | Engine's control-plane node id |
| `SWE_PRO_PORT` | `8801` | Engine's listen port |
| `SWE_PRO_PUBLIC_URL` | `http://localhost:8801` (engine default) | Callback base URL — **must** be set to a container-reachable address in Docker, otherwise the control plane cannot reach the engine |
| `SWE_PRO_MAX_COST` | unset | Per-dispatch cost ceiling (USD) for `pro_execute` |
| `SWE_PRO_MODELS_HIGH` | engine default | High-tier model pool (comma-separated) |
| `SWE_PRO_MODELS_LOW` | engine default | Low-tier model pool |
| `SWE_PRO_VARIANT` | engine default | Reasoning effort (`low` = fastest) |

The engine inherits `OPENROUTER_API_KEY` and the control-plane coordinates
(`AGENTFIELD_SERVER`, `AGENTFIELD_API_KEY`) from the node's environment.

**OpenRouter-only deployments:** nothing to configure. The compose files leave
`SWE_DEFAULT_RUNTIME` unset, so with an OpenRouter key as the only provider
credential the node auto-selects the `open_code` runtime and defaults every
role — including the advisory and verification roles that run outside the
engine — to `openrouter/deepseek/deepseek-v4-flash-0731`. Setting
`SWE_DEFAULT_RUNTIME` explicitly is supported but unnecessary here.

## Control-plane inactivity sweep

The engine does one issue's coding inside a single long call, where the classic
loop makes many short ones. A control plane that reaps executions by "time
since last activity" therefore sees the waiting parent as idle and can mark a
healthy build `execution timed out (no activity)` while the engine is still
working — the engine keeps going and finishes, but the run is already reported
failed.

AgentField fixes this by not reaping an execution that is waiting on a
non-terminal child. On an older control plane, raise
`agentfield.execution_cleanup.stale_execution_timeout` (shipped default `10m`)
past the longest single issue you expect, or bound engine runs with
`SWE_PRO_MAX_COST` / `max_hours` so they finish inside the window.

## Rollout

The pro engine is the default for `af install` / Desktop nodes as of this
release, and opt-in everywhere else (clone, fork, compose, bare binary). The
classic coding loop remains fully supported and is one variable away
(`SWE_PRO_ENGINE=0`); existing reasoner names and input/output shapes are
identical either way, so switching costs nothing but a restart.
