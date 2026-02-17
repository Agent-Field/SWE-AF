# Contributing

Thanks for contributing to SWE-AF.

## Prerequisites

- Python 3.12+
- AgentField control plane (`af`)
- Access to an AI runtime used by your run (`claude_code` or `open_code`)

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Development workflow

1. Start AgentField control plane:

```bash
af
```

2. In a second terminal, start this node:

```bash
python3 -m swe_af
```

3. Run tests before opening a PR:

```bash
python3 -m unittest discover -s tests -v
```

## Repository hygiene

- Do not commit generated Python caches or Rust `target/` outputs.
- Keep example artifacts that document completed example runs.
- Keep changes scoped; avoid unrelated formatting churn.

## Pull requests

Each PR should include:

- What changed and why.
- Any behavior changes to agent orchestration.
- Test evidence (unit tests and/or command output).
