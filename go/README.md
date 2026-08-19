# SWE-AF — Go node

A 1:1 Go port of the SWE-AF autonomous engineering node. It registers the same
reasoners under the same names as the Python node, calls between them through
the AgentField control plane, and exposes a byte-compatible HTTP API — so the
control-plane DAG UI renders identically. The Python package under `swe_af/`
is untouched; this port lives entirely under `go/`.

Two binaries:

| Binary            | Node ID          | Default port | Role                              |
|-------------------|------------------|--------------|-----------------------------------|
| `swe-planner`     | `swe-planner`    | `8005`       | Full pipeline (plan → DAG → PR)   |
| `swe-fast`        | `swe-fast`       | `8006`       | Fast mode (lighter-weight path)   |

Module path: `github.com/Agent-Field/SWE-AF/go`.

## This is the installed node

`af install https://github.com/Agent-Field/SWE-AF` installs this, under the
name `swe-planner` — the repo-root manifest declares itself `superseded_by`
this directory, so the bare URL lands here and an existing Python install is
replaced in place. Same node id, same reasoner names, same triggers:

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H 'Content-Type: application/json' \
  -d '{"input":{"goal":"...","repo_url":"https://github.com/you/repo"}}'
```

The Python package under `swe_af/` is untouched and still what `python -m
swe_af` and the Python compose stack run. Because both now answer to the same
node id, running them together against **one** control plane needs an explicit
`NODE_ID` on one of them — `docker-compose.go.yml` sets `swe-planner-go` /
`swe-fast-go` for exactly that. `NODE_ID` / `PORT` override the defaults
anywhere you need different ids or ports.

## Depending on the AgentField Go SDK

There are **no `sdk/go/vX.Y.Z` submodule tags** in the agentfield repo, so a
normal versioned `require` is impossible. The port depends on the SDK
(`github.com/Agent-Field/agentfield/sdk/go`) two ways:

- **Dev — Go workspace.** A `go.work` at the shared parent of both repos
  (`<workspace>/go.work`) lists `./SWE-AF/go` and `./agentfield/sdk/go`,
  so edits to the SDK are picked up live with zero `go.mod` churn. It is not
  committed (it spans two repos). With the workspace present, `go build ./...`
  just works.
- **CI / Docker — `replace` directive.** `go.mod` carries
  `replace github.com/Agent-Field/agentfield/sdk/go => ../../agentfield/sdk/go`.
  Any build without the workspace (set `GOWORK=off`, or build where no `go.work`
  exists) resolves the SDK through that relative path, which must point at a
  sibling checkout of the agentfield repo. The Docker builder clones it there
  automatically (see below).

Migration target: once agentfield publishes `sdk/go/vX.Y.Z` submodule tags, drop
the `replace` and switch to a real `require`. The agentfield repo is treated as
read-only — every SDK gap is worked around app-side.

## Build & run locally

From `go/`:

```bash
make build          # go build ./...
make vet            # go vet ./...
make test           # go test ./...
make check          # vet + test
make run-planner    # run the full-pipeline node (swe-planner, :8005)
make run-fast       # run the fast-mode node   (swe-fast, :8006)
```

`make run-planner` / `make run-fast` need a control plane reachable at
`AGENTFIELD_SERVER` (default `http://localhost:8080`). Both nodes read all
configuration from the environment at startup (the Go SDK reads no env itself).

To build without the dev workspace (the way CI/Docker do), a sibling agentfield
checkout must exist at `../../agentfield`:

```bash
GOWORK=off go build ./...
```

## Docker

The image is a multi-stage build. The builder clones the AgentField Go SDK at a
**pinned ref** and lays it out so the `replace` path resolves, then builds both
static binaries; the runtime stage is a slim Debian with the same external CLI
surface the agents shell out to (`git`, `gh`, `jq`, OpenCode, Codex, Claude
Code).

Build the image (context is the **repo root**, so the whole `go/` module is
available and the SDK clone can be laid out as a sibling):

```bash
# from go/
make docker-build                                  # tag swe-af-go:latest
make docker-build IMAGE=myrepo/swe-af-go:dev \
     AGENTFIELD_SDK_REF=<agentfield-sha>           # override tag / SDK ref

# or directly from the repo root
docker build -f go/Dockerfile \
     --build-arg AGENTFIELD_SDK_REF=<agentfield-sha> \
     -t swe-af-go:latest .
```

The default `AGENTFIELD_SDK_REF` is pinned to a real agentfield `main` commit.
The SDK clone layer is cache-keyed on this arg — **bump the ref to pull a newer
SDK**; an unchanged ref restores the cached clone (same rationale as the
docker-pip cache-busting rule: the constraint string itself must change to
invalidate the layer).

### Compose: opt-in add-on to the Python stack

`docker-compose.go.yml` (at the repo root) is an **add-on**, not a standalone
stack. It defines only the two Go nodes and joins the Python stack's compose
network as an external reference, sharing the control plane and `workspaces`
volume the Python stack brings up. The Python `docker-compose.yml` is left
untouched. Start the Python stack first, then layer the Go nodes:

```bash
docker compose up -d                          # Python stack (control plane + Python nodes)
docker compose -f docker-compose.go.yml up -d # adds the Go nodes

# or, from go/ (Python stack must already be up)
make docker-up      # docker compose -f ../docker-compose.go.yml up --build
make docker-down
```

Adds:

| Service        | Port   | Node id          | Notes                                     |
|----------------|--------|------------------|-------------------------------------------|
| `swe-agent-go` | `8005` | `swe-planner-go` | full pipeline                             |
| `swe-fast-go`  | `8006` | `swe-fast-go`    | fast mode (runs the `swe-fast` binary)    |

This add-on stack overrides `NODE_ID` to the `-go` ids on purpose: it joins a
running Python stack on one control plane, and the two would otherwise register
under the same names.

The control plane (`:8080`), `build-db`, and the `workspaces` volume come from
the Python stack — the Go add-on joins them via the external `swe-af_default`
network and `swe-af_workspaces` volume (this assumes the Python stack was
brought up with the default project name `swe-af`; see the compose file header
for the override). Health: `curl -f http://localhost:8005/health` and
`:8006/health`.

## Environment variables

Both nodes are configured entirely through the environment. The compose file
loads `.env` (`env_file: .env`) and adds the per-service overrides. See
[`.env.example`](../.env.example) at the repo root for the documented common
set; the load-bearing ones:

| Variable                                                  | Purpose                                              |
|-----------------------------------------------------------|------------------------------------------------------|
| `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`           | Claude runtime (`claude_code`)                       |
| `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY`| Open runtimes (`open_code` / `codex`)                |
| `GH_TOKEN`                                                | Optional: GitHub PAT (`repo` scope) — needed for private repos and PRs |
| `SWE_DEFAULT_RUNTIME`                                     | `claude_code` \| `open_code` \| `codex` (unset: auto — `open_code` when only an OpenRouter key is present, else `claude_code`) |
| `SWE_DEFAULT_MODEL`                                       | Default model when the request config omits `models` |
| `SWE_CODEX_AUTH_MODE`                                     | `auto` \| `chatgpt` \| `api_key` (codex CLI auth)     |
| `OPENCODE_ENABLE_EXA` + `EXA_API_KEY`                     | Optional web search for the open runtime             |
| `AGENTFIELD_SERVER`                                       | Control-plane URL (default `http://localhost:8080`)  |
| `AGENT_CALLBACK_URL`                                      | Public URL the control plane calls the node back on. **Required for any containerized/remote deploy that isn't this compose file** (compose sets it per service) — without it the CP gets `504 agent_unreachable` |
| `NODE_ID`                                                 | Node ID (`swe-planner` / `swe-fast`)           |
| `PORT`                                                    | Listen port (`8005` / `8006`)                        |
| `SWE_PRO_ENGINE`                                          | Route per-issue coding through the bundled high-performance coding engine (beta), vendored for darwin-arm64, darwin-amd64, linux-amd64, and linux-arm64. Windows is not yet supported by the engine; the node logs a warning and falls back to the classic loop there. **Set to `1` for you by `af install` / Desktop; unset (off) everywhere else** — clone, fork, compose, bare binary. `1`/`true`/`yes`/`on` enables, `0`/`false` disables |
| `SWE_PRO_VARIANT`                                         | Engine reasoning-effort variant (e.g. `low` for fastest turnaround, `high` for depth). Unset keeps the engine's own default |
| `SWE_PRO_MAX_COST`                                        | Per-run cost ceiling in USD forwarded to the engine on every dispatch. Unset: no SWE-AF-side ceiling |
| `SWE_PRO_PUBLIC_URL`                                      | Callback base URL for the engine, mirroring `AGENT_CALLBACK_URL` on the nodes. **In Docker this must be set to a container-reachable URL**, otherwise the control plane can't call the engine back |

Advanced knobs (HITL/approvals: `HAX_API_KEY`, `HAX_SDK_URL`, `HAX_SENDER_NAME`,
`HAX_SENDER_KEY`, `AGENTFIELD_APPROVAL_USER_ID`; git identity for the resolve
flow: `SWE_AF_GIT_EMAIL`, `SWE_AF_GIT_NAME`; auth: `AGENTFIELD_API_KEY`) are
read from the environment as well — grep `os.Getenv` under `internal/` for the
authoritative set. The per-request build config JSON (`runtime`, `models`,
budget/iteration knobs) is byte-identical to the Python node's — see the root
[README](../README.md) and `.env.example` for the schema and examples.

## Coding engine (beta)

The Go node bundles a prebuilt high-performance coding engine alongside the
classic coding loop. Whether it runs is decided entirely by `SWE_PRO_ENGINE`,
and the two ways of running the node differ only in who sets that variable:

- **`af install` / AgentField Desktop — on by default.**
  `agentfield-package.yaml` declares `SWE_PRO_ENGINE` with `default: "1"` and
  the installer's env resolver injects it, so the node supervises the engine as
  a sidecar and routes per-issue coding through it without being asked. Set
  `SWE_PRO_ENGINE=0` to get the classic coder → reviewer/QA loop.
- **Anything else — off.** A clone, a fork, `docker-compose.go.yml` (which
  passes `SWE_PRO_ENGINE` through but leaves it empty), or a bare `swe-planner`
  binary starts on the classic loop. Set `SWE_PRO_ENGINE=1` to opt in.

A missing or non-runnable binary is not fatal — the node logs a warning and
keeps using the classic loop.

Full env surface (including the `SWE_PRO_*` knobs above, model pools, and the
sidecar's restart behaviour): [`docs/pro-engine.md`](docs/pro-engine.md).

## Deployment: `af install`

This directory ships its own `agentfield-package.yaml` (node `swe-planner`),
and the repo-root manifest redirects here, so the bare repo URL is enough. The
`//` subdirectory selector still works if you want to be explicit:

```bash
af install https://github.com/Agent-Field/SWE-AF        # redirects here
af install https://github.com/Agent-Field/SWE-AF//go    # same thing, explicit
af run swe-planner       # builds bin/swe-planner at install time (needs Go)
af uninstall swe-planner
```

Both manifests declare the name `swe-planner`, so only one can be installed at
a time — installing this replaces an existing Python install in place, keeping
its node-scoped secrets. To install the Python node deliberately, clone the
repo and install the checkout as a local path; local-path installs do not
follow the redirect. The SDK is
pinned in `go.mod` by pseudo-version (the same commit the Dockerfile pins), so
the module resolves without the dev workspace. Docker image / compose / bare
binary remain the container deployment paths.
