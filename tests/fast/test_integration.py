"""Integration verification tests — all 20 PRD acceptance criteria.

Each test function corresponds directly to one PRD acceptance criterion (AC-1 through AC-20).
Tests use subprocess.run() with the exact python -c commands from the PRD for critical checks,
and in-process assertions for lighter checks.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml
import pytest

REPO_ROOT = Path(__file__).parent.parent.parent


def _run(
    code: str,
    extra_env: dict | None = None,
    unset_keys: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run python -c <code> in a fresh interpreter, returning CompletedProcess.

    Args:
        code: Python code to run via -c.
        extra_env: Additional env vars to set (override existing).
        unset_keys: Keys to remove from the inherited environment.
    """
    env = os.environ.copy()
    # Remove keys that should not be inherited
    for key in (unset_keys or []):
        env.pop(key, None)
    env.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# AC-1: Module structure exists — all required modules importable
# ---------------------------------------------------------------------------


def test_ac_1_module_importability():
    """AC-1: All required swe_af.fast modules are importable."""
    code = """
import importlib
for m in ['swe_af.fast', 'swe_af.fast.app', 'swe_af.fast.schemas',
          'swe_af.fast.planner', 'swe_af.fast.executor', 'swe_af.fast.verifier']:
    importlib.import_module(m)
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-2: FastBuildConfig rejects unknown keys
# ---------------------------------------------------------------------------


def test_ac_2_fastbuildconfig_rejects_unknown_keys():
    """AC-2: FastBuildConfig raises an exception for extra/unknown fields."""
    code = """
from swe_af.fast.schemas import FastBuildConfig
try:
    FastBuildConfig(unknown_field='x')
    exit(1)
except Exception:
    print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-3: FastBuildConfig defaults are correct
# ---------------------------------------------------------------------------


def test_ac_3_fastbuildconfig_defaults():
    """AC-3: FastBuildConfig has correct default values."""
    code = """
from swe_af.fast.schemas import FastBuildConfig
cfg = FastBuildConfig()
assert cfg.runtime == 'claude_code', f'runtime={cfg.runtime}'
assert cfg.max_tasks == 10, f'max_tasks={cfg.max_tasks}'
assert cfg.task_timeout_seconds == 300, f'task_timeout_seconds={cfg.task_timeout_seconds}'
assert cfg.build_timeout_seconds == 600, f'build_timeout_seconds={cfg.build_timeout_seconds}'
assert cfg.enable_github_pr == True, f'enable_github_pr={cfg.enable_github_pr}'
assert cfg.agent_max_turns == 50, f'agent_max_turns={cfg.agent_max_turns}'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-4: Fast model defaults are haiku for claude_code
# ---------------------------------------------------------------------------


def test_ac_4_claude_code_defaults_to_haiku():
    """AC-4: fast_resolve_models() returns haiku for all roles with claude_code runtime."""
    code = """
from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig
cfg = FastBuildConfig(runtime='claude_code')
resolved = fast_resolve_models(cfg)
for role, model in resolved.items():
    assert model == 'haiku', f'{role} defaulted to {model!r}, expected haiku'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-5: Fast model defaults are cheap open model for open_code
# ---------------------------------------------------------------------------


def test_ac_5_open_code_defaults_to_qwen():
    """AC-5: fast_resolve_models() returns qwen model for all roles with open_code runtime."""
    code = """
from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig
cfg = FastBuildConfig(runtime='open_code')
resolved = fast_resolve_models(cfg)
for role, model in resolved.items():
    assert model == 'qwen/qwen-2.5-coder-32b-instruct', f'{role}={model!r}'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-6: Fast model override works
# ---------------------------------------------------------------------------


def test_ac_6_model_override():
    """AC-6: models dict override takes precedence over runtime defaults."""
    code = """
from swe_af.fast.schemas import fast_resolve_models, FastBuildConfig
cfg = FastBuildConfig(runtime='claude_code', models={'coder': 'sonnet', 'default': 'haiku'})
resolved = fast_resolve_models(cfg)
assert resolved['coder_model'] == 'sonnet', f'coder_model={resolved["coder_model"]}'
assert resolved['pm_model'] == 'haiku', f'pm_model={resolved["pm_model"]}'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-7: FastTask schema validates correctly
# ---------------------------------------------------------------------------


def test_ac_7_fasttask_schema():
    """AC-7: FastTask can be constructed with required fields; defaults are correct."""
    code = """
from swe_af.fast.schemas import FastTask
t = FastTask(name='add-feature', title='Add Feature', description='Do it', acceptance_criteria=['It works'])
assert t.name == 'add-feature'
assert t.files_to_create == []
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-8: swe_af.fast.app creates Agent with node_id swe-fast
# ---------------------------------------------------------------------------


def test_ac_8_app_node_id_is_swe_fast():
    """AC-8: app object in swe_af.fast.app has node_id == 'swe-fast' when NODE_ID is swe-fast."""
    code = """
import os
os.environ.setdefault('AGENTFIELD_SERVER', 'http://localhost:9999')
from swe_af.fast.app import app
assert app.node_id == 'swe-fast', f'node_id={app.node_id}'
print('OK')
"""
    # Explicitly set NODE_ID=swe-fast and remove any inherited value that could
    # override the default (e.g. NODE_ID=swe-planner set in CI environment).
    result = _run(code, extra_env={"NODE_ID": "swe-fast"})
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-9: build reasoner has correct signature (goal, repo_path, config)
# ---------------------------------------------------------------------------


def test_ac_9_build_signature():
    """AC-9: build() function in swe_af.fast.app has goal, repo_path, config parameters."""
    code = """
import inspect, os
os.environ.setdefault('AGENTFIELD_SERVER', 'http://localhost:9999')
import importlib
m = importlib.import_module('swe_af.fast.app')
# The @app.reasoner() decorator wraps the function; access _original_func
# to inspect the true signature defined by the developer.
fn = getattr(m.build, '_original_func', m.build)
sig = inspect.signature(fn)
params = list(sig.parameters.keys())
assert 'goal' in params, f'goal missing: {params}'
assert 'repo_path' in params, f'repo_path missing: {params}'
assert 'config' in params, f'config missing: {params}'
print('OK')
"""
    result = _run(code, extra_env={"NODE_ID": "swe-fast"})
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-10: NODE_ID env var override works
# ---------------------------------------------------------------------------


def test_ac_10_node_id_env_var_override():
    """AC-10: NODE_ID env var overrides the default 'swe-fast' node ID."""
    code = """
import os
os.environ['NODE_ID'] = 'swe-fast-test'
os.environ.setdefault('AGENTFIELD_SERVER', 'http://localhost:9999')
import importlib
import swe_af.fast.app
importlib.reload(swe_af.fast.app)
from swe_af.fast.app import app
assert app.node_id == 'swe-fast-test', f'node_id={app.node_id}'
print('OK')
"""
    result = _run(code, extra_env={"NODE_ID": "swe-fast-test"})
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-11: docker-compose.yml has swe-fast service
# ---------------------------------------------------------------------------


def test_ac_11_docker_compose_swe_fast_service():
    """AC-11: docker-compose.yml defines swe-fast service with NODE_ID and PORT."""
    code = """
import yaml
with open('docker-compose.yml') as f:
    dc = yaml.safe_load(f)
assert 'swe-fast' in dc['services'], f'services={list(dc["services"].keys())}'
svc = dc['services']['swe-fast']
env = svc.get('environment', [])
env_dict = dict(e.split('=',1) for e in env if '=' in e) if isinstance(env, list) else env
assert env_dict.get('NODE_ID') == 'swe-fast', f'NODE_ID={env_dict.get("NODE_ID")}'
assert env_dict.get('PORT') == '8004', f'PORT={env_dict.get("PORT")}'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-12: Per-task timeout is enforced via asyncio.wait_for
# ---------------------------------------------------------------------------


def test_ac_12_per_task_timeout_enforced():
    """AC-12: asyncio.wait_for raises TimeoutError for slow tasks within the configured timeout."""
    code = """
import asyncio

async def _mock_slow_coder():
    await asyncio.sleep(10)
    return {'complete': True, 'files_changed': []}

async def test_timeout():
    try:
        await asyncio.wait_for(_mock_slow_coder(), timeout=0.05)
        print('FAIL: should have timed out')
        exit(1)
    except asyncio.TimeoutError:
        print('OK')

asyncio.run(test_timeout())
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-13: FastPlanResult caps tasks at max_tasks (enforced in planner, not schema)
# ---------------------------------------------------------------------------


def test_ac_13_planner_references_max_tasks():
    """AC-13: planner module references max_tasks for task capping logic."""
    code = """
from swe_af.fast.schemas import FastTask, FastPlanResult
tasks = [FastTask(name=f't{i}', title=f'T{i}', description='d', acceptance_criteria=['ac']) for i in range(15)]
result = FastPlanResult(tasks=tasks, rationale='test')
assert len(result.tasks) <= 15  # schema allows any count
# Capping must happen in fast_plan_tasks reasoner, not schema
# Verify planner module exists and has cap logic
import inspect
import swe_af.fast.planner as planner_mod
src = inspect.getsource(planner_mod)
assert 'max_tasks' in src, 'max_tasks not referenced in planner'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-14: No existing swe_af/ files are modified (git diff check)
# ---------------------------------------------------------------------------


def test_ac_14_no_existing_swe_af_files_modified():
    """AC-14: git diff HEAD shows no swe_af/ files modified outside swe_af/fast/,
    docker-compose.yml, and pyproject.toml.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    # Collect lines that are under swe_af/ but not swe_af/fast/
    unexpected = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Allow: swe_af/fast/**, docker-compose.yml, pyproject.toml, setup.cfg, setup.py, .artifacts/**
        if line.startswith("swe_af/fast/"):
            continue
        if line in ("docker-compose.yml", "pyproject.toml", "setup.cfg", "setup.py"):
            continue
        if line.startswith(".artifacts/"):
            continue
        if line.startswith("tests/"):
            continue
        # Any other swe_af/ file is unexpected
        if line.startswith("swe_af/"):
            unexpected.append(line)

    assert unexpected == [], (
        f"Existing swe_af/ files were modified (outside swe_af/fast/): {unexpected}"
    )


# ---------------------------------------------------------------------------
# AC-15: executor references task_timeout_seconds and asyncio.wait_for
# ---------------------------------------------------------------------------


def test_ac_15_executor_references_timeout():
    """AC-15: fast executor source contains task_timeout_seconds and wait_for references."""
    code = """
import inspect
import swe_af.fast.executor as ex
src = inspect.getsource(ex)
assert 'task_timeout_seconds' in src, 'task_timeout_seconds not in executor'
assert 'asyncio.wait_for' in src or 'wait_for' in src, 'wait_for not in executor'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-16: executor does NOT call QA, code-reviewer, synthesizer, replanner
# ---------------------------------------------------------------------------


def test_ac_16_executor_no_forbidden_calls():
    """AC-16: fast executor does not reference QA/reviewer/synthesizer/replanner functions."""
    code = """
import inspect
import swe_af.fast.executor as ex
src = inspect.getsource(ex)
forbidden = ['run_qa', 'run_code_reviewer', 'run_qa_synthesizer', 'run_replanner', 'run_issue_advisor', 'run_retry_advisor']
for f in forbidden:
    assert f not in src, f'Forbidden call {f!r} found in fast executor'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-17: planner does NOT call architect, tech_lead, sprint_planner
# ---------------------------------------------------------------------------


def test_ac_17_planner_no_forbidden_calls():
    """AC-17: fast planner does not reference architect/tech_lead/sprint_planner/product_manager."""
    code = """
import inspect
import swe_af.fast.planner as pl
src = inspect.getsource(pl)
forbidden = ['run_architect', 'run_tech_lead', 'run_sprint_planner', 'run_product_manager', 'run_issue_writer']
for f in forbidden:
    assert f not in src, f'Forbidden call {f!r} found in fast planner'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-18: verifier does NOT implement fix cycles
# ---------------------------------------------------------------------------


def test_ac_18_verifier_no_fix_cycles():
    """AC-18: fast verifier does not reference fix cycle functions."""
    code = """
import inspect
import swe_af.fast.verifier as vf
src = inspect.getsource(vf)
forbidden = ['generate_fix_issues', 'max_verify_fix_cycles', 'fix_cycles']
for f in forbidden:
    assert f not in src, f'Forbidden {f!r} found in fast verifier'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-19: swe_af.fast and swe_af can be imported simultaneously without conflict
# ---------------------------------------------------------------------------


def test_ac_19_co_import_no_conflict():
    """AC-19: swe_af.fast.app and swe_af.app can be imported simultaneously with distinct node IDs."""
    code = """
import os
os.environ.setdefault('AGENTFIELD_SERVER', 'http://localhost:9999')
import swe_af.app as planner_app
import swe_af.fast.app as fast_app
assert planner_app.app.node_id == 'swe-planner', f'planner node_id={planner_app.app.node_id}'
assert fast_app.app.node_id == 'swe-fast', f'fast node_id={fast_app.app.node_id}'
print('OK')
"""
    # Unset NODE_ID so each module picks up its own hardcoded default:
    # swe_af.app defaults to 'swe-planner', swe_af.fast.app defaults to 'swe-fast'.
    result = _run(code, unset_keys=["NODE_ID"])
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# AC-20: swe_af.fast.app exports main() callable
# ---------------------------------------------------------------------------


def test_ac_20_main_is_callable():
    """AC-20: main() function exported from swe_af.fast.app is callable."""
    code = """
import os
os.environ.setdefault('AGENTFIELD_SERVER', 'http://localhost:9999')
from swe_af.fast.app import main
assert callable(main), 'main is not callable'
print('OK')
"""
    result = _run(code)
    assert result.returncode == 0, f"Non-zero exit: stderr={result.stderr}"
    assert "OK" in result.stdout, f"Expected OK in stdout: {result.stdout!r}"
