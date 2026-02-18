"""Shared fixtures for swe_af.fast tests.

Problem: importing ``swe_af.fast.app`` calls ``app.include_router(fast_router)``,
which replaces each reasoner's ``func`` with a tracking wrapper named
``"tracked_func"``.  Tests that inspect ``fast_router.reasoners`` directly
(test_init_router.py, test_executor.py) then fail because the expected function
names are no longer visible.

Fix: a function-scoped autouse fixture that reloads ``swe_af.fast`` and its
reasoner sub-modules before every test, producing a fresh ``fast_router`` whose
reasoners still carry their original names.  The ``swe_af.fast.app`` module is
*not* reloaded (it stays in sys.modules) because reloading it would call
``include_router`` again and immediately re-mangle the fresh router.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_fast_router() -> None:  # type: ignore[return]
    """Reload swe_af.fast so fast_router has original (un-tracked) func names.

    This is a no-op until ``swe_af.fast.app`` has been imported for the first
    time.  After that it becomes necessary to restore the clean state that
    tests like test_init_router.py and test_executor.py expect.
    """
    # Only reload when swe_af.fast.app is already cached (i.e. include_router
    # has already been called and may have mangled fast_router.reasoners).
    if "swe_af.fast.app" not in sys.modules:
        yield
        return

    # Save and evict all swe_af.fast.* sub-modules EXCEPT app itself.
    sub_keys = [
        k for k in list(sys.modules)
        if k.startswith("swe_af.fast") and k != "swe_af.fast.app"
    ]
    saved = {k: sys.modules.pop(k) for k in sub_keys}

    try:
        # Re-import swe_af.fast — this recreates fast_router and re-registers
        # all the @fast_router.reasoner() wrappers with original func references.
        importlib.import_module("swe_af.fast")
        # Re-import sub-modules that register reasoners on the fresh fast_router.
        for mod in (
            "swe_af.fast.executor",
            "swe_af.fast.planner",
            "swe_af.fast.verifier",
        ):
            try:
                importlib.import_module(mod)
            except ImportError:
                pass

        yield
    finally:
        # After the test, evict the freshly-loaded sub-modules so that the
        # next test gets a clean reload too.
        for k in list(sys.modules):
            if k.startswith("swe_af.fast") and k != "swe_af.fast.app":
                sys.modules.pop(k, None)
        # Restore the saved copies so the process stays consistent.
        sys.modules.update(saved)
