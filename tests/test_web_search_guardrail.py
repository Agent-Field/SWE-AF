"""Tests for swe_af.tools.web_search.maybe_apply_coder_guardrail.

The guardrail should append only when opencode's web-search env vars are
set, so the model isn't told about a tool it can't use.
"""

from __future__ import annotations

import pytest

from swe_af.tools.web_search import (
    WEB_SEARCH_CODER_GUARDRAIL,
    _websearch_enabled,
    maybe_apply_coder_guardrail,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("OPENCODE_ENABLE_EXA", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)


def test_returns_unchanged_when_no_env(monkeypatch):
    """No env vars set → guardrail not appended (model isn't told about a
    tool it can't use)."""
    base = "You are a coder."
    assert maybe_apply_coder_guardrail(base) == base
    assert _websearch_enabled() is False


def test_returns_unchanged_when_only_flag_set(monkeypatch):
    """OPENCODE_ENABLE_EXA alone is insufficient — Exa needs a key too."""
    monkeypatch.setenv("OPENCODE_ENABLE_EXA", "1")
    base = "You are a coder."
    assert maybe_apply_coder_guardrail(base) == base


def test_returns_unchanged_when_only_key_set(monkeypatch):
    """EXA_API_KEY alone is insufficient — opencode doesn't activate
    websearch without the gate flag."""
    monkeypatch.setenv("EXA_API_KEY", "fake-key")
    base = "You are a coder."
    assert maybe_apply_coder_guardrail(base) == base


def test_returns_unchanged_when_flag_not_one(monkeypatch):
    """Only ``OPENCODE_ENABLE_EXA=1`` enables — other truthy values
    (true, yes, on, ...) don't satisfy opencode's gate."""
    monkeypatch.setenv("OPENCODE_ENABLE_EXA", "true")
    monkeypatch.setenv("EXA_API_KEY", "fake-key")
    base = "You are a coder."
    assert maybe_apply_coder_guardrail(base) == base


def test_appends_when_both_env_vars_set(monkeypatch):
    """Happy path: both env vars present → guardrail appended."""
    monkeypatch.setenv("OPENCODE_ENABLE_EXA", "1")
    monkeypatch.setenv("EXA_API_KEY", "fake-key")
    assert _websearch_enabled() is True

    base = "You are a coder."
    result = maybe_apply_coder_guardrail(base)
    assert result.startswith(base)
    assert WEB_SEARCH_CODER_GUARDRAIL in result


def test_guardrail_text_is_substantive(monkeypatch):
    """The guardrail must actually steer the model — empty / one-liner
    tip wouldn't do anything. Asserts the snippet mentions both
    websearch and the restraint principle."""
    text = WEB_SEARCH_CODER_GUARDRAIL
    assert "websearch" in text.lower() or "web_search" in text.lower()
    # Must convey "don't reach for it casually"
    assert "Do NOT" in text or "do not" in text or "sparingly" in text
    # Must give concrete *when to use* examples
    assert "API" in text or "library" in text or "error message" in text
    # Reasonable length — not a wall of text, not a one-liner
    assert 200 < len(text) < 2000


def test_empty_key_treated_as_absent(monkeypatch):
    """An empty EXA_API_KEY string shouldn't pass the gate."""
    monkeypatch.setenv("OPENCODE_ENABLE_EXA", "1")
    monkeypatch.setenv("EXA_API_KEY", "")
    base = "You are a coder."
    assert maybe_apply_coder_guardrail(base) == base
