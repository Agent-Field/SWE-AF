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
