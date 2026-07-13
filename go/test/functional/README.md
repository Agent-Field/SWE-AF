# SWE-AF Go port — functional (black-box) parity tests

These tests exercise the **live** stack — the AgentField control-plane plus the
two Go nodes (`swe-planner-go` on `:8005`, `swe-fast-go` on `:8006`) — brought
up via the self-contained `compose.functional.yml`, and assert the byte-level
parity contracts the Python→Go port must preserve (design `§11(b)`,
work-breakdown `T7.2`).

They are isolated behind the `functional` build tag, so the unit CI job
(`go test ./...`) never touches them. They run only when you ask for them.

## What is asserted

| Test | Contract |
|---|---|
| `TestHealth` | `GET /health` on both Go nodes returns `200`. |
| `TestRegistrationParity` | `swe-planner-go` registers **exactly** 30 reasoners and `swe-fast-go` **exactly** 29 — the parity checklist (name-set equality, no missing/extra). Names come from the Python registration surface, not from the Go `register.go`. |
| `TestDeterministicReasonerKeySets` | `run_ci_watcher` (the only no-LLM reasoner) on **both** nodes, called against a nonexistent repo path (deterministic — `gh pr checks` fails immediately), returns a result whose key set is exactly the Python `CIWatchResult.model_dump()` set: `status, pr_number, elapsed_seconds, failed_checks, summary`. |
| `TestReasonerFailedStatusContract` | The control-plane persistence contract the ReasonerFailed carrier (design `§4.5`) relies on: `status=failed` + `result` + `error` persist **together**, and a resultless `failed` re-post (what the SDK sends) does **not** clobber the carried result. |
| `TestEmptyBuildGuardViaBuild` | **Always skipped** — triggering the real empty-build guard needs an LLM plan/execute cycle; its CP contract is covered by `TestReasonerFailedStatusContract`, end-to-end by the gated build test below. |
| `TestBuildLLMAndDAGParity` | **Env-gated (default-skipped).** A real minimal `swe-planner.build` end to end: asserts the `BuildResult` key set **and** that the control-plane DAG contains child executions for the expected planning role reasoners (`run_product_manager`, `run_architect`, `run_tech_lead`, `run_sprint_planner`, `run_issue_writer`). Costs money/time. |

`TestMain` owns the lifecycle: it runs

```
docker compose -p swe-af-go-functional \
  -f go/test/functional/compose.functional.yml \
  -f go/test/functional/compose.override.functional.yml \
  up -d --build
```

(`compose.functional.yml` is a self-contained stack — control-plane + both Go
nodes — distinct from the production `docker-compose.go.yml`, which is an
add-on to the Python stack and has no control plane of its own.)

**once**, waits for both nodes to be healthy and registered, runs every test
against that shared stack, then `docker compose ... down -v` (volumes removed).
If Docker (or a running daemon) is unavailable, the whole suite **skips with a
message**; if Docker is available but `up` fails, the suite **fails** (that is
a real breakage, not an environmental skip).

The override file remaps only the **host** port bindings — control-plane
`:18080`, swe-planner-go `:18005`, swe-fast-go `:18006` — so the functional
stack can run alongside anything already occupying `8080/8005/8006` on the host
(a host control-plane, the Go add-on nodes, unrelated projects). Container ports
and every service-to-service URL are unchanged. The dedicated compose project
name likewise isolates its containers/volumes/network from the regular
`swe-af` project.

## Prerequisites

- Docker with a running daemon, and the Docker Compose v2 plugin (`docker
  compose`).
- The repo-root `.env` — Compose reads it automatically. A control-plane image
  tagged `agentfield/control-plane:latest` must be available locally (the
  compose file references it); the two Go node images are built from
  `go/Dockerfile` on first `up`.

## Running

The non-LLM suite (health, registration parity, deterministic key sets, the
carrier status contract):

```bash
cd go
go test -tags functional ./test/functional/ -v
```

Notes:

- The first run performs a cold `up --build` (multi-stage Go builds that clone
  the AgentField SDK), so allow **several minutes** before the tests start. The
  suite caps bring-up at 15 min and readiness at 4 min.
- Run from the `go/` module directory (or anywhere inside it) — the harness
  locates the repo root relative to the test source, not the working directory.
- Total wall time once images are cached: roughly **1–2 minutes** of tests on
  top of container start-up.

### The LLM-gated build + DAG-parity test

```bash
cd go
SWE_FUNCTIONAL_LLM=1 go test -tags functional ./test/functional/ -run TestBuildLLMAndDAGParity -v
```

This runs **only** when `SWE_FUNCTIONAL_LLM=1` **and** an Anthropic credential
(`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`) is present in the
environment. It:

- creates a throwaway git repo inside the `swe-agent` container's `/workspaces`
  volume (the node operates on container-local paths, not host paths);
- runs `build` with goal `"Create a file hello.txt containing the word hello"`
  and `config = {"models":{"default":"haiku"}}` to minimise cost;
- polls the build execution to a terminal state with a hard **15-minute** cap;
- asserts the `BuildResult` key set and the DAG child-reasoner set.

Expected duration: a few minutes (dominated by the LLM plan/execute).

## Tearing down manually

`TestMain` always tears the stack down. If a run is interrupted, clean up with:

```bash
docker compose -p swe-af-go-functional \
  -f docker-compose.go.yml -f go/test/functional/compose.override.functional.yml \
  down -v
```
