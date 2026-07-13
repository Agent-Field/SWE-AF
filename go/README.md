# SWE-AF — Go node

A 1:1 Go port of the SWE-AF autonomous engineering node. It registers the same
reasoners under the same names as the Python node, calls between them through
the AgentField control plane, and exposes a byte-compatible HTTP API — so the
control-plane DAG UI renders identically. The Python package under `swe_af/`
is untouched; this port lives entirely under `go/`.

Two binaries:

| Binary            | Node ID          | Default port | Role                              |
|-------------------|------------------|--------------|-----------------------------------|
| `swe-planner`     | `swe-planner-go` | `8005`       | Full pipeline (plan → DAG → PR)   |
| `swe-fast`        | `swe-fast-go`    | `8006`       | Fast mode (lighter-weight path)   |

Module path: `github.com/Agent-Field/SWE-AF/go`.

## Opt-in alongside Python

The Python node is the **default**: `swe-planner` on `:8003` (and `swe-fast` on
`:8004`), unchanged. This Go port registers **separately** under distinct
identities — `swe-planner-go` on `:8005` and `swe-fast-go` on `:8006` — so both
stacks can run against **one** control plane at the same time. Nothing is
replaced; callers **opt in** by targeting the `-go` reasoner path, e.g.

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner-go.build \
  -H 'Content-Type: application/json' \
  -d '{"input":{"goal":"...","repo_url":"https://github.com/you/repo"}}'
```

`NODE_ID` / `PORT` still override the defaults if you want different ids/ports.

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
make run-planner    # run the full-pipeline node (swe-planner-go, :8005)
make run-fast       # run the fast-mode node   (swe-fast-go, :8006)
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

| Service        | Port   | Node id          | Notes                                  |
|----------------|--------|------------------|----------------------------------------|
| `swe-agent-go` | `8005` | `swe-planner-go` | full pipeline                          |
| `swe-fast-go`  | `8006` | `swe-fast-go`    | fast mode (runs the `swe-fast` binary) |

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
| `GH_TOKEN`                                                | GitHub PAT (`repo` scope) for PRs                    |
| `SWE_DEFAULT_RUNTIME`                                     | `claude_code` \| `open_code` \| `codex` (default `claude_code`) |
| `SWE_DEFAULT_MODEL`                                       | Default model when the request config omits `models` |
| `SWE_CODEX_AUTH_MODE`                                     | `auto` \| `chatgpt` \| `api_key` (codex CLI auth)     |
| `OPENCODE_ENABLE_EXA` + `EXA_API_KEY`                     | Optional web search for the open runtime             |
| `AGENTFIELD_SERVER`                                       | Control-plane URL (default `http://localhost:8080`)  |
| `AGENT_CALLBACK_URL`                                      | Public URL the control plane calls the node back on. **Required for any containerized/remote deploy that isn't this compose file** (compose sets it per service) — without it the CP gets `504 agent_unreachable` |
| `NODE_ID`                                                 | Node ID (`swe-planner-go` / `swe-fast-go`)           |
| `PORT`                                                    | Listen port (`8005` / `8006`)                        |

Advanced knobs (HITL/approvals: `HAX_API_KEY`, `HAX_SDK_URL`, `HAX_SENDER_NAME`,
`HAX_SENDER_KEY`, `AGENTFIELD_APPROVAL_USER_ID`; git identity for the resolve
flow: `SWE_AF_GIT_EMAIL`, `SWE_AF_GIT_NAME`; auth: `AGENTFIELD_API_KEY`) are
read from the environment as well — grep `os.Getenv` under `internal/` for the
authoritative set. The per-request build config JSON (`runtime`, `models`,
budget/iteration knobs) is byte-identical to the Python node's — see the root
[README](../README.md) and `.env.example` for the schema and examples.

## Deployment: no `af install` (yet)

`af install <this repo>` installs the **Python** node: the only
`agentfield-package.yaml` in this repository is the root one, which declares
the Python entrypoint (`python -m swe_af`, node id `swe-planner`). There is no
Go manifest, so the Go nodes deploy via **Docker image / compose / binary**.
The upstream AgentField installer does support Go nodes (`language: go` +
`entrypoint.build`), and a subdirectory selector (`af install <repo> --path go`)
is in flight — once both are released, shipping a `go/agentfield-package.yaml`
would make the Go node installable the same way. Until then: compose add-on or
bare binary.
