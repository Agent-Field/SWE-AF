"""Contract test for the ``python -m swe_af`` entry point.

The AgentField control plane assigns a free port per launch, passes it via the
``PORT`` env var, and polls readiness on that port. ``main()`` must therefore
NOT hardcode a port — it must let the SDK's ``Agent.run()`` read ``PORT`` (or
auto-select a free port when run standalone). Passing an explicit port bound the
wrong one and made the control plane's readiness check time out
("agent node did not become ready within 10s").
"""
from __future__ import annotations

import os

os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")

import swe_af.app as app_module


def test_main_does_not_hardcode_a_port(monkeypatch):
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(app_module.app, "run", fake_run)
    app_module.main()

    assert not captured["args"], "main() must not pass a positional port to app.run()"
    assert "port" not in captured["kwargs"], (
        "main() must not hardcode a port; the SDK reads PORT from the control plane"
    )
