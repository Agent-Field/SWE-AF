"""Fatal API error detection for non-retryable harness failures.

When the underlying API returns a fatal error (billing exhaustion, invalid
credentials, disabled account), retrying is pointless and wastes time. This
module provides:

- ``FatalHarnessError`` — a distinct exception type that short-circuits all
  retry layers.
- ``check_fatal_harness_error()`` — inspects a HarnessResult's error_message
  and raises ``FatalHarnessError`` immediately on match.

Ref: https://github.com/Agent-Field/SWE-AF/issues/49
"""

from __future__ import annotations

import re

# Patterns that indicate a non-retryable API failure.
# Matched case-insensitively against error_message strings.
_FATAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"credit balance is too low",
        r"insufficient.{0,20}credits?",
        r"billing.{0,20}(expired|inactive|suspended)",
        r"invalid.{0,10}api.?key",
        r"invalid.{0,10}x-api-key",
        r"(your )?api key is not valid",
        r"authentication failed",
        r"account has been disabled",
        r"account.{0,10}is disabled",
        r"unauthorized",
        r"quota.{0,20}exceeded",
        # Codex model/auth mismatches: retrying with the same model + auth mode
        # fails identically. Treating these as fatal surfaces the real reason
        # (instead of a silent empty build that burns the retry cap) — e.g. the
        # default `-codex` model under ChatGPT-account auth (#82 Gap 3).
        r"not supported when using codex with a chatgpt account",
        r"requires a newer version of codex",
    )
)


class FatalHarnessError(RuntimeError):
    """Raised when the harness encounters a non-retryable API error.

    This exception is designed to propagate through all retry layers
    (schema retries, SDK execution retries, pipeline retries) without
    being caught by generic ``except Exception`` handlers that would
    otherwise silently retry.
    """

    def __init__(self, message: str) -> None:
        super().__init__(f"Fatal API error (non-retryable): {message}")
        self.original_message = message


class EmptyHarnessCompletionError(RuntimeError):
    """Raised when a harness call completes but produces no output at all.

    Distinct from a *schema-invalid* completion (output present but unparseable
    into the target schema). An empty completion — the harness returned with no
    parsed object and no raw text, typically in ~1s — almost always means the
    provider/model pairing is wrong: e.g. a model id prefixed for a *different*
    runtime (an ``openrouter/…`` model handed to the codex CLI's OpenAI
    backend, which doesn't know it) or missing/invalid auth for the selected
    provider. Collapsing this into the generic "failed to produce a valid
    <artifact>" message makes it indistinguishable from a genuine schema-quality
    failure, so this error names the provider and model explicitly to point at
    the real root cause.
    """

    def __init__(self, *, role: str, provider: str, model: str, detail: str = "") -> None:
        message = (
            f"{role} harness returned an empty completion "
            f"(provider={provider}, model={model}) — check provider "
            f"auth/model compatibility"
        )
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.role = role
        self.provider = provider
        self.model = model
        self.original_message = detail


def is_fatal_error(error_message: str) -> bool:
    """Return True if *error_message* matches a known fatal API error pattern."""
    if not error_message:
        return False
    return any(p.search(error_message) for p in _FATAL_PATTERNS)


def check_fatal_harness_error(result) -> None:
    """Inspect a HarnessResult and raise ``FatalHarnessError`` if fatal.

    Should be called immediately after ``router.harness()`` returns,
    *before* any ``result.parsed is None`` check, so the real error
    message surfaces instead of a misleading generic one.

    Parameters
    ----------
    result:
        A ``HarnessResult`` (or any object with ``is_error`` and
        ``error_message`` attributes).

    Raises
    ------
    FatalHarnessError
        If the result indicates a non-retryable API failure.
    """
    if not getattr(result, "is_error", False):
        return
    msg = getattr(result, "error_message", "") or ""
    if is_fatal_error(msg):
        raise FatalHarnessError(msg)


def _harness_output_text(result) -> str:
    """Best-effort raw completion text from a HarnessResult-like object.

    Reads ``result`` (the raw completion string) first, falling back to the
    ``text`` convenience property. Returns ``""`` when neither is populated.
    """
    raw = getattr(result, "result", None)
    if not raw:
        raw = getattr(result, "text", None)
    return raw or ""


def check_empty_harness_completion(
    result, *, role: str, provider: str, model: str
) -> None:
    """Raise ``EmptyHarnessCompletionError`` when a harness produced no output.

    Call *after* ``check_fatal_harness_error`` and *before* the caller's
    ``parsed is None`` schema-quality check. This fires only for the "empty
    completion" shape — neither a parsed object nor any raw text — which is the
    signature of a provider/model mismatch (a model id meant for a different
    runtime, or bad auth) rather than a schema-quality problem. When raw text
    *is* present but couldn't be parsed, this is a no-op and the caller's
    generic schema-invalid error (which should also name provider+model) takes
    over.

    Parameters
    ----------
    result:
        A ``HarnessResult`` (or any object exposing ``parsed`` / ``result`` /
        ``text`` / ``error_message``).
    role:
        Human label for the failing reasoner (e.g. ``"PM"``, ``"Architect"``).
    provider:
        The harness provider the call used (e.g. ``"codex"``, ``"opencode"``).
    model:
        The model id the call was made with.
    """
    if getattr(result, "parsed", None):
        return
    if _harness_output_text(result).strip():
        return
    detail = (getattr(result, "error_message", "") or "").strip()
    raise EmptyHarnessCompletionError(
        role=role, provider=provider, model=model, detail=detail
    )
