"""SWE-AF's reasoner and agent timeouts must fit a feature-level build.

``plan`` and ``execute`` each run a whole phase inside one reasoner call, so the
AgentField SDK's 2-hour default watchdog killed builds that were still making
progress. These tests pin the node-level defaults and the env-var override
path.
"""

from __future__ import annotations

import pytest

from swe_af.execution.schemas import BuildConfig, ExecutionConfig
from swe_af.runtime.async_config import TWELVE_HOURS, SweAsyncConfig


def test_reasoner_budget_defaults_to_twelve_hours():
    config = SweAsyncConfig()

    assert config.default_execution_timeout == TWELVE_HOURS
    assert config.max_execution_timeout >= config.default_execution_timeout
    # Sanity: the SDK default (2h) is the bug this config fixes.
    assert config.default_execution_timeout > 7200


def test_environment_overrides_both_values(monkeypatch):
    monkeypatch.setenv("AGENTFIELD_ASYNC_MAX_EXECUTION_TIMEOUT", "3600")
    monkeypatch.setenv("AGENTFIELD_ASYNC_DEFAULT_EXECUTION_TIMEOUT", "1800")

    config = SweAsyncConfig.from_environment()

    assert config.max_execution_timeout == 3600
    assert config.default_execution_timeout == 1800


def test_lowered_max_also_bounds_the_default(monkeypatch):
    """A deployment that only sets MAX should not keep a 12h default."""
    monkeypatch.setenv("AGENTFIELD_ASYNC_MAX_EXECUTION_TIMEOUT", "7200")
    monkeypatch.delenv("AGENTFIELD_ASYNC_DEFAULT_EXECUTION_TIMEOUT", raising=False)

    config = SweAsyncConfig.from_environment()

    assert config.max_execution_timeout == 7200
    assert config.default_execution_timeout == 7200


def test_both_nodes_install_the_swe_async_defaults():
    import swe_af.app as planner_app
    import swe_af.fast.app as fast_app

    assert isinstance(planner_app.app.async_config, SweAsyncConfig)
    assert isinstance(fast_app.app.async_config, SweAsyncConfig)


@pytest.mark.parametrize("config_cls", [BuildConfig, ExecutionConfig])
def test_agent_timeout_covers_observed_agent_runtime(config_cls):
    """A measured QA call ran 3087s; the old 2700s cap killed it."""
    assert config_cls().agent_timeout_seconds >= 3600
