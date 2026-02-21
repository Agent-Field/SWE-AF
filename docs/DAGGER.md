# Dagger CI/CD Integration

SWE-AF integrates [Dagger](https://dagger.io/) to provide isolated, reproducible CI/CD pipelines that agents can invoke to validate their work. This enables self-validating agents that run tests, builds, and lints in clean containers without polluting the host system.

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    CODER AGENT                                  │
│                                                                 │
│   ┌─────────────┐                                               │
│   │ Write code  │                                               │
│   └──────┬──────┘                                               │
│          ▼                                                      │
│   ┌─────────────────────────────────────┐                      │
│   │ dagger.pipeline() on target repo:   │                      │
│   │  1. Build (cached layers)           │                      │
│   │  2. Run tests (isolated)            │                      │
│   │  3. Lint/format                     │                      │
│   │  4. Integration tests (with DB)     │                      │
│   └─────────────────────────────────────┘                      │
│          │                                                      │
│          ▼                                                      │
│   ┌─────────────┐                                               │
│   │ Fix if fail │ ◀── feedback loop                            │
│   └─────────────┘                                               │
└─────────────────────────────────────────────────────────────────┘
```

## Benefits

### Strategic Value for Autonomous Agents

Dagger transforms SWE-AF agents from "code writers" to "self-validating engineers":

| Before Dagger | After Dagger |
|---------------|--------------|
| Agent writes code, hopes it works | Agent writes code, validates in isolated container, fixes if broken |
| "Works on my machine" failures | Reproducible validation every time |
| Human must run CI to catch issues | Agent catches issues before human review |
| Agent limited to host-installed tools | Agent can validate ANY project type |

### Key Benefits

**1. Self-Validating Agents**
Agents close the loop themselves:
```
Write code → Validate with Dagger → Parse failures → Fix code → Re-validate → Commit
```
No human intervention needed for CI failures.

**2. Zero Host Pollution**
Every validation runs in a fresh container. No:
- Stray processes
- Modified system files
- Conflicting dependencies
- Leftover test databases

**3. Reproducibility**
Same input = same output. If an agent's code passes Dagger validation, it will pass in:
- GitHub Actions
- GitLab CI
- Any other CI system
- Another developer's machine

**4. Universal Project Support**
Agents don't need to know project setup. Dagger auto-detects:
- Python (poetry, pip, django, fastapi)
- Node (npm, react, nextjs, express)
- Rust (cargo)
- Go (go modules)

An agent can fix a Rust bug without Rust installed on the host.

**5. Integration Test Support**
Spin up real databases for tests:
```python
services=["postgres", "redis"]
```
No mocks, no host-installed databases, no connection string mysteries.

### ROI Rating

| Layer | Rating | Why |
|-------|--------|-----|
| Layer 3 (this implementation) | **10/10** | Immediate value: every agent becomes self-validating |
| Layer 1 (Dagger for SWE-AF DAG) | 8/10 | Good for caching, but SWE-AF already has checkpointing |
| Layer 2 (Container-use per issue) | 9/10 | Excellent for isolation, but higher implementation effort |

Layer 3 is the highest-ROI integration because:
- Agents gain CI/CD capability immediately
- No changes to SWE-AF core architecture
- Works with existing Docker Compose deployment
- Incremental adoption (agents opt-in via prompts)

### Why Not Just Use Docker Compose?

| Aspect | Docker Compose | Dagger |
|--------|----------------|--------|
| Purpose | Service orchestration | Pipeline orchestration |
| Caching | Manual | Automatic (BuildKit) |
| Programmability | YAML | Python SDK |
| Per-run isolation | Requires cleanup | Automatic |
| Agent-friendly | No | Yes (structured output) |

Docker Compose runs SWE-AF. Dagger validates what SWE-AF produces.

## Prerequisites

1. **Dagger CLI** installed:
   ```bash
   curl -fsSL https://dl.dagger.io/dagger/install.sh | sh
   ```

2. **Dagger SDK** in Python (already in `pyproject.toml`):
   ```toml
   dependencies = [
       "dagger-io>=0.15.0",
   ]
   ```

3. **Docker** or compatible container runtime running

## Available Reasoners

### `run_dagger_pipeline`

Main CI/CD pipeline entry point.

```python
result = await app.call("run_dagger_pipeline",
    repo_path="/path/to/worktree",
    pipeline="test",              # "test" | "build" | "lint" | "full" | "custom"
    services=["postgres"],        # Optional: postgres, redis, mysql, mongodb
    custom_command="",            # Only used when pipeline="custom"
    timeout_seconds=600,          # Max pipeline duration
)
```

**Returns:** `DaggerPipelineResult`
```python
{
    "success": bool,
    "pipeline": str,
    "test_result": {              # When pipeline includes tests
        "success": bool,
        "tests_run": int,
        "tests_passed": int,
        "tests_failed": int,
        "output": str,
        "errors": str,
        "test_failures": [{"file": str, "test_name": str, "error": str}]
    },
    "build_result": {...},        # When pipeline includes build
    "lint_result": {...},         # When pipeline includes lint
    "summary": str,
    "duration_seconds": float
}
```

### `run_dagger_test`

Convenience wrapper for tests only.

```python
result = await app.call("run_dagger_test",
    repo_path="/path/to/worktree",
    services=["postgres"],
    test_command="pytest -v",     # Optional: override auto-detected command
)
```

### `detect_project`

Detect project type and configuration.

```python
result = await app.call("detect_project",
    repo_path="/path/to/worktree"
)

# Returns:
{
    "language": "python",         # python, node, rust, go, unknown
    "framework": "fastapi",       # fastapi, django, react, nextjs, etc.
    "build_system": "poetry",     # poetry, pip, npm, cargo, etc.
    "test_command": "pytest -v",
    "lint_command": "ruff check .",
    "build_command": "poetry install",
    "detected_files": ["pyproject.toml", ...]
}
```

## Pipeline Types

| Pipeline | Description |
|----------|-------------|
| `test` | Run test suite only |
| `build` | Build the project only |
| `lint` | Run linters/formatters only |
| `full` | Build → Test → Lint (lint non-blocking) |
| `custom` | Run custom command via `custom_command` param |

## Supported Services

Spin up ephemeral services for integration tests:

| Service | Container | Environment Variables |
|---------|-----------|----------------------|
| `postgres` | `postgres:16-alpine` | `DATABASE_URL=postgresql://test:test@postgres:5432/test` |
| `redis` | `redis:7-alpine` | `REDIS_URL=redis://redis:6379` |
| `mysql` | `mysql:8` | `DATABASE_URL=mysql://root:test@mysql:3306/test` |
| `mongodb` | `mongo:7` | `MONGO_URL=mongodb://test:test@mongodb:27017` |

## Project Detection

Auto-detection inspects these files:

| Language | Detection Files |
|----------|-----------------|
| Python | `pyproject.toml`, `setup.py`, `requirements.txt` |
| Node | `package.json` |
| Rust | `Cargo.toml` |
| Go | `go.mod` |

Framework detection via dependency inspection:
- Python: `fastapi`, `django`, `flask`
- Node: `next`, `react`, `express`

## CLI Usage

SWE-AF provides a CLI for running Dagger pipelines directly:

```bash
# Show help
uv run python -m swe_af dagger --help

# Detect project type only (fast)
uv run python -m swe_af dagger --detect

# Run lint in isolated container
uv run python -m swe_af dagger --pipeline lint

# Run tests in isolated container
uv run python -m swe_af dagger --pipeline test

# Run build in isolated container
uv run python -m swe_af dagger --pipeline build

# Run full CI (build + test + lint)
uv run python -m swe_af dagger --pipeline full

# With services (databases)
uv run python -m swe_af dagger --pipeline test --services postgres,redis

# Run on a different project
uv run python -m swe_af dagger --path /path/to/project --pipeline test

# Custom timeout (seconds)
uv run python -m swe_af dagger --pipeline test --timeout 600
```

### CLI Options

| Flag | Short | Description |
|------|-------|-------------|
| `--detect` | `-d` | Only detect project type, don't run pipeline |
| `--pipeline` | `-p` | Pipeline type: `test`, `build`, `lint`, `full`, `custom` |
| `--path` | `-C` | Project path (default: current directory) |
| `--services` | `-s` | Services: `postgres,redis,mysql,mongodb` |
| `--timeout` | `-t` | Timeout in seconds (default: 300) |
| `--custom-command` | | Custom command (only with `--pipeline custom`) |

### CLI vs Agent API

| Use Case | Method |
|----------|--------|
| Developer testing locally | CLI: `uv run python -m swe_af dagger --pipeline test` |
| Agent validating code | API: `app.call("run_dagger_pipeline", ...)` |
| CI/CD pipeline | CLI or API both work |

## Usage in Agents

### In Coder Prompt

Agents are instructed to validate with Dagger before marking complete:

```python
# After writing code
result = await app.call("run_dagger_pipeline",
    repo_path=worktree_path,
    pipeline="test"
)

if not result["success"]:
    for failure in result["test_result"]["test_failures"]:
        # Fix code based on failure
        await fix_test(failure["file"], failure["test_name"], failure["error"])
```

### With Services

```python
# Tests requiring PostgreSQL
result = await app.call("run_dagger_pipeline",
    repo_path=worktree_path,
    pipeline="test",
    services=["postgres"]
)
```

### Full CI Before Commit

```python
# Comprehensive validation
result = await app.call("run_dagger_pipeline",
    repo_path=worktree_path,
    pipeline="full"
)

if result["success"]:
    await commit_changes()
```

## Architecture

```
swe_af/reasoners/dagger_runner.py
├── Schemas
│   ├── DaggerPipelineType (enum)
│   ├── DaggerService (enum)
│   ├── DaggerTestResult
│   ├── DaggerBuildResult
│   ├── DaggerLintResult
│   ├── DaggerPipelineResult
│   └── ProjectDetectionResult
├── Project Detection
│   └── detect_project_type()
├── Pipeline Builders
│   ├── build_python_pipeline()
│   └── build_node_pipeline()
├── Reasoners (registered via @router.reasoner)
│   ├── run_dagger_pipeline()
│   ├── run_dagger_test()
│   └── detect_project()
└── Helpers
    ├── _parse_test_output()
    ├── _parse_pytest_output()
    ├── _parse_jest_output()
    ├── _parse_lint_output()
    └── _build_summary()
```

## Caching

Dagger leverages BuildKit layer caching:

1. **Base image layers** - Cached per Python/Node version
2. **Dependency installation** - Cached if `pyproject.toml`/`package.json` unchanged
3. **Source code** - Mounted fresh each run (not cached)

This means re-running tests after code changes is fast — dependencies are cached.

## Error Handling

```python
result = await app.call("run_dagger_pipeline", ...)

if not result["success"]:
    # Check what failed
    if result["test_result"] and not result["test_result"]["success"]:
        print(f"Tests failed: {result['test_result']['tests_failed']}")
        for f in result["test_result"]["test_failures"]:
            print(f"  - {f['file']}::{f['test_name']}")
    
    if result["build_result"] and not result["build_result"]["success"]:
        print(f"Build failed: {result['build_result']['errors']}")
```

## Future: Layer 1 & 2 Integration

This implementation is **Layer 3** (Dagger as agent tool). Future layers:

| Layer | Purpose | Status |
|-------|---------|--------|
| Layer 1 | Dagger orchestrates SWE-AF's DAG | Future |
| Layer 2 | Container-use for per-issue isolation | Future |
| Layer 3 | Dagger as agent CI/CD tool | **Implemented** |

See the [architecture discussion](./ARCHITECTURE.md) for SWE-AF's overall DAG structure.

## Troubleshooting

### Dagger CLI not found
```bash
curl -fsSL https://dl.dagger.io/dagger/install.sh | sh
dagger version
```

### Docker not running
```bash
docker info
```

### Timeout on large test suites
```python
result = await app.call("run_dagger_pipeline",
    ...,
    timeout_seconds=1200  # Increase timeout
)
```

### Service connection refused
Services need a moment to start. Dagger handles this automatically, but if tests fail immediately:
```python
# Add a small wait in your test config
import time
time.sleep(2)  # Let service be ready
```
