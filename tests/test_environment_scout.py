"""Unit tests for the swe_af.hitl environment-scout substrate.

Three pillars covered:

  1. ``swe_af.hitl.services`` — static signal-file detection.
  2. ``swe_af.hitl.credentials_store`` — process-local store with isolation
     and thread safety.
  3. ``swe_af.hitl.scout_schema`` + the wrapper loop — the LLM closure's
     pass-1/pass-2 round-trip through ``run_with_ask_user``.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe_af.hitl.ask_user import AskUserForm, AskUserFormField
from swe_af.hitl.credentials_store import (
    _STORE,
    clear_scoped_credentials,
    get_scoped_credentials,
    inject_credentials_into_env,
    store_scoped_credentials,
)
from swe_af.hitl.scout_schema import ScoutResult
from swe_af.hitl.services import (
    KNOWN_SERVICES,
    ServiceCredentialSpec,
    detect_services_from_repo,
    known_service_summary_for_prompt,
)
from swe_af.hitl.wrapper import AskUserBudget, run_with_ask_user


# ---------------------------------------------------------------------------
# services.py — detection
# ---------------------------------------------------------------------------


def test_known_services_inventory_covers_baseline():
    """We promised at least 8 services in the spec — pin that here."""
    assert len(KNOWN_SERVICES) >= 8
    names = {s.service_name for s in KNOWN_SERVICES}
    assert {"Railway", "Fly.io", "Vercel", "Supabase", "Sentry"}.issubset(names)


def test_detect_services_returns_empty_for_missing_path():
    assert detect_services_from_repo("") == []
    assert detect_services_from_repo("/this/path/does/not/exist") == []


def test_detect_services_finds_railway_and_sentry(tmp_path):
    (tmp_path / "railway.toml").write_text("[deploy]\n")
    (tmp_path / "sentry.properties").write_text("dsn=foo\n")
    found = detect_services_from_repo(str(tmp_path))
    names = [s.service_name for s in found]
    assert "Railway" in names
    assert "Sentry" in names


def test_detect_services_signal_can_be_directory(tmp_path):
    """A signal file may actually be a directory (e.g. supabase/migrations)."""
    (tmp_path / "supabase").mkdir()
    (tmp_path / "supabase" / "migrations").mkdir()
    found = detect_services_from_repo(str(tmp_path))
    assert "Supabase" in [s.service_name for s in found]


def test_known_service_summary_for_prompt_is_markdown_bullets():
    out = known_service_summary_for_prompt(KNOWN_SERVICES[:2])
    assert out.startswith("- **")
    assert "env `" in out
    assert "mint at " in out


# ---------------------------------------------------------------------------
# credentials_store.py — round-trip, filtering, isolation, thread safety
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _wipe_store():
    """Each test starts and ends with an empty store."""
    _STORE.clear()
    yield
    _STORE.clear()


def test_store_and_get_round_trip():
    store_scoped_credentials("build-A", {"RAILWAY_TOKEN": "secret"})
    assert get_scoped_credentials("build-A") == {"RAILWAY_TOKEN": "secret"}


def test_store_filters_blank_and_none_values():
    store_scoped_credentials(
        "build-A",
        {"RAILWAY_TOKEN": "ok", "EMPTY": "", "WHITESPACE": "   ", "NONE": None},  # type: ignore[dict-item]
    )
    got = get_scoped_credentials("build-A")
    assert got == {"RAILWAY_TOKEN": "ok"}


def test_store_isolation_between_builds():
    store_scoped_credentials("build-A", {"RAILWAY_TOKEN": "a"})
    store_scoped_credentials("build-B", {"RAILWAY_TOKEN": "b"})
    assert get_scoped_credentials("build-A") == {"RAILWAY_TOKEN": "a"}
    assert get_scoped_credentials("build-B") == {"RAILWAY_TOKEN": "b"}


def test_clear_only_removes_the_specified_build():
    store_scoped_credentials("build-A", {"RAILWAY_TOKEN": "a"})
    store_scoped_credentials("build-B", {"RAILWAY_TOKEN": "b"})
    clear_scoped_credentials("build-A")
    assert get_scoped_credentials("build-A") == {}
    assert get_scoped_credentials("build-B") == {"RAILWAY_TOKEN": "b"}


def test_get_returns_copy_not_reference():
    """Mutating the returned dict must not affect the stored value."""
    store_scoped_credentials("build-A", {"RAILWAY_TOKEN": "a"})
    got = get_scoped_credentials("build-A")
    got["RAILWAY_TOKEN"] = "tampered"
    got["NEW_VAR"] = "injected"
    assert get_scoped_credentials("build-A") == {"RAILWAY_TOKEN": "a"}


def test_concurrent_writes_isolate_by_execution_id():
    """Two threads writing under different keys must not race each other."""
    def writer(name: str, value: str):
        for _ in range(100):
            store_scoped_credentials(name, {"TOKEN": value})

    t1 = threading.Thread(target=writer, args=("build-A", "a"))
    t2 = threading.Thread(target=writer, args=("build-B", "b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert get_scoped_credentials("build-A") == {"TOKEN": "a"}
    assert get_scoped_credentials("build-B") == {"TOKEN": "b"}


def test_inject_credentials_returns_new_dict():
    base = {"PATH": "/usr/bin", "RAILWAY_TOKEN": "stale"}
    store_scoped_credentials("build-A", {"RAILWAY_TOKEN": "fresh"})
    merged = inject_credentials_into_env(base, "build-A")
    assert merged == {"PATH": "/usr/bin", "RAILWAY_TOKEN": "fresh"}
    # base must be untouched.
    assert base == {"PATH": "/usr/bin", "RAILWAY_TOKEN": "stale"}


def test_inject_credentials_no_scope_returns_base_only():
    base = {"PATH": "/usr/bin"}
    merged = inject_credentials_into_env(base, "")
    assert merged == base
    assert merged is not base  # still a copy


def test_inject_credentials_empty_base_works():
    store_scoped_credentials("build-A", {"RAILWAY_TOKEN": "x"})
    merged = inject_credentials_into_env(None, "build-A")
    assert merged == {"RAILWAY_TOKEN": "x"}


# ---------------------------------------------------------------------------
# scout_schema.py + wrapper closure round-trip
# ---------------------------------------------------------------------------


def _scout_result_pass1(spec_form: AskUserForm) -> ScoutResult:
    return ScoutResult(
        detected_services=[
            ServiceCredentialSpec(
                service_name="Railway",
                env_var_name="RAILWAY_TOKEN",
                mint_url="https://example",
                permissions_hint="hint",
                signal_files=["railway.toml"],
            )
        ],
        scoped_credentials={},
        skipped_services=[],
        ask_user_form=spec_form,
    )


def _scout_result_pass2(values: dict[str, str]) -> ScoutResult:
    return ScoutResult(
        detected_services=[],
        scoped_credentials=values,
        skipped_services=[],
        summary=f"Got {len(values)} credential(s).",
        ask_user_form=None,
    )


def _approval_result(values: dict[str, str]):
    obj = MagicMock()
    obj.decision = "approved"
    obj.feedback = ""
    obj.raw_response = {"values": values}
    return obj


def _silent_app():
    app = MagicMock()
    app.note = MagicMock()
    app.pause = AsyncMock()
    return app


@pytest.mark.asyncio
async def test_scout_closure_pass1_emits_form_pass2_emits_credentials():
    """Two-pass dance: scout asks once, gets the answers, returns the dict."""
    form = AskUserForm(
        title="Pick credentials",
        fields=[
            AskUserFormField(
                id="RAILWAY_TOKEN",
                type="input",
                label="Railway token",
                required=False,
            ),
        ],
    )
    reasoner = AsyncMock(
        side_effect=[
            _scout_result_pass1(form),
            _scout_result_pass2({"RAILWAY_TOKEN": "rt_xxx"}),
        ]
    )
    hax = MagicMock()
    hax.create_request = MagicMock(return_value=MagicMock(id="r1", url="u"))
    app = _silent_app()
    app.pause.return_value = _approval_result({"RAILWAY_TOKEN": "rt_xxx"})

    parsed = await run_with_ask_user(
        reasoner_fn=reasoner,
        reasoner_kwargs={"prior_user_responses": []},
        app=app,
        hax_client=hax,
        budget=AskUserBudget(remaining=3),
    )

    assert isinstance(parsed, ScoutResult)
    assert parsed.scoped_credentials == {"RAILWAY_TOKEN": "rt_xxx"}
    assert parsed.ask_user_form is None
    assert reasoner.await_count == 2

    # The second invocation should have received the prior values.
    second_call_kwargs = reasoner.await_args_list[1].kwargs
    prior = second_call_kwargs["prior_user_responses"]
    assert len(prior) == 1
    assert prior[0]["values"] == {"RAILWAY_TOKEN": "rt_xxx"}


@pytest.mark.asyncio
async def test_scout_closure_skips_pause_when_no_services_detected():
    """If the LLM judges no credentials needed, the wrapper short-circuits."""
    reasoner = AsyncMock(
        return_value=ScoutResult(
            detected_services=[],
            ask_user_form=None,
            summary="No third-party credentials needed.",
        )
    )
    app = _silent_app()

    parsed = await run_with_ask_user(
        reasoner_fn=reasoner,
        reasoner_kwargs={"prior_user_responses": []},
        app=app,
        hax_client=MagicMock(),
        budget=AskUserBudget(remaining=3),
    )

    assert parsed.scoped_credentials == {}
    reasoner.assert_awaited_once()
    app.pause.assert_not_called()


# ---------------------------------------------------------------------------
# Schema serialisation — scoped_credentials must NEVER leak through model_dump
# when excluded.
# ---------------------------------------------------------------------------


def test_scout_result_model_dump_can_exclude_scoped_credentials():
    """We exclude the field at the reasoner boundary so it never reaches the
    control-plane workflow_execution row."""
    r = ScoutResult(
        detected_services=[],
        scoped_credentials={"RAILWAY_TOKEN": "secret-do-not-log"},
        summary="ok",
    )
    safe = r.model_dump(exclude={"scoped_credentials"})
    assert "scoped_credentials" not in safe
    assert safe["summary"] == "ok"
    # The full dump still has it (caller's choice).
    full = r.model_dump()
    assert full["scoped_credentials"] == {"RAILWAY_TOKEN": "secret-do-not-log"}
