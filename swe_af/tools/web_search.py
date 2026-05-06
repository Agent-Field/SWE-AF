"""Helpers for steering reasoner behavior when opencode's web search is enabled.

Opencode ships built-in ``websearch`` and ``webfetch`` tools that activate
when ``OPENCODE_ENABLE_EXA=1`` and ``EXA_API_KEY`` are set in the deployment
env. The opencode subprocess inherits parent env via agentfield's
``run_cli`` (see ``agentfield.harness._cli``), so no SWE-AF wiring is
required to *enable* web search — set the env vars in Railway / your deploy
and reasoners running through opencode automatically gain the capability.

Tool *awareness* is also handled for free: opencode advertises its built-in
tools to the LLM via the standard tool-definition layer, so the model knows
``websearch`` exists without any system-prompt boilerplate from us.

What lives in this module is the *one* place a prompt-level addition
genuinely earns its keep: the coder reasoner. ``run_coder`` runs many turns
inside a single coding loop, and an unrestrained model will happily spend
turns searching for things it could read from the codebase. The
``WEB_SEARCH_CODER_GUARDRAIL`` snippet narrows the agent's discretion to
the cases where external lookup is actually load-bearing.

Other reasoners (architect, planner, reviewer, ...) don't get prompt
additions — they're short or single-shot, the loop-risk is negligible,
and the model's own judgment plus opencode's tool advertisement is
sufficient. Add per-reasoner restraint guidance only if a specific
reasoner is observed over-searching in practice.
"""

from __future__ import annotations

import os


WEB_SEARCH_CODER_GUARDRAIL = """

## When to use the web_search / web_fetch tools

You may have access to a `websearch` (and possibly `webfetch`) tool. Use it sparingly. Reach for it ONLY when:

- You encounter an unfamiliar API and cannot understand its behavior from the existing codebase
- An error message is opaque and the codebase + test output don't explain it
- You need to verify library/framework version compatibility for a specific call
- You need to check whether a function or pattern is deprecated

Do NOT use websearch for:
- General programming knowledge or design patterns
- Anything answerable by reading existing files in the repo
- Style or convention questions — follow what the codebase already does

Default to writing code. Reach for websearch only when a concrete blocker requires external context, then return immediately to the implementation.
"""


def _websearch_enabled() -> bool:
    """True iff opencode's built-in websearch is gated open in the deploy env."""
    return os.environ.get("OPENCODE_ENABLE_EXA") == "1" and bool(
        os.environ.get("EXA_API_KEY")
    )


def maybe_apply_coder_guardrail(system_prompt: str) -> str:
    """Append the coder-specific web-search guardrail when web search is enabled.

    The guardrail is appended only when ``OPENCODE_ENABLE_EXA=1`` and
    ``EXA_API_KEY`` are set, so the model isn't told about a tool it can't
    use. When web search isn't enabled, returns ``system_prompt`` unchanged.
    """
    if not _websearch_enabled():
        return system_prompt
    return system_prompt + WEB_SEARCH_CODER_GUARDRAIL


__all__ = [
    "WEB_SEARCH_CODER_GUARDRAIL",
    "maybe_apply_coder_guardrail",
]
