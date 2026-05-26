"""Unit tests for the ``swe_af.hitl`` ask-user-via-form primitive."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe_af.hitl.ask_user import (
    AskUserForm,
    AskUserFormField,
    _parse_approval_result_to_response,
    build_form_builder,
    request_user_input_and_pause,
)
from swe_af.hitl.wrapper import AskUserBudget, run_with_ask_user


def _approval_result(
    *,
    decision: str = "approved",
    feedback: str | None = None,
    raw_response: dict | None = None,
):
    """Build a stand-in for ``agentfield.client.ApprovalResult``."""
    obj = MagicMock()
    obj.decision = decision
    obj.feedback = feedback or ""
    obj.raw_response = raw_response
    return obj


def _silent_app() -> MagicMock:
    """An app stub that swallows ``.note(...)`` calls and exposes an async ``.pause``."""
    app = MagicMock()
    app.note = MagicMock()
    app.pause = AsyncMock()
    return app


# ---------------------------------------------------------------------------
# build_form_builder
# ---------------------------------------------------------------------------


def test_build_form_builder_input_field():
    spec = AskUserForm(
        title="Need clarification",
        fields=[
            AskUserFormField(
                id="reason", type="input", label="Why?", required=True
            ),
        ],
    )
    payload = build_form_builder(spec).to_payload()
    assert payload["title"] == "Need clarification"
    fields = payload["fields"]
    assert len(fields) == 1
    assert fields[0]["type"] == "input"
    assert fields[0]["id"] == "reason"
    assert fields[0]["label"] == "Why?"
    assert fields[0]["required"] is True


def test_build_form_builder_select_with_options():
    spec = AskUserForm(
        title="Pick one",
        fields=[
            AskUserFormField(
                id="choice",
                type="select",
                label="Choice",
                options=[
                    {"value": "a", "label": "Option A"},
                    {"value": "b", "label": "Option B"},
                ],
            ),
        ],
    )
    payload = build_form_builder(spec).to_payload()
    field = payload["fields"][0]
    assert field["type"] == "select"
    assert field["options"] == [
        {"value": "a", "label": "Option A"},
        {"value": "b", "label": "Option B"},
    ]


def test_build_form_builder_all_supported_types_smoke():
    spec = AskUserForm(
        title="All types",
        fields=[
            AskUserFormField(id="i", type="input", label="i"),
            AskUserFormField(id="t", type="textarea", label="t"),
            AskUserFormField(id="n", type="number", label="n", min=0, max=10),
            AskUserFormField(
                id="sl", type="slider", label="sl", min=0, max=100, step=5
            ),
            AskUserFormField(
                id="s", type="select", label="s",
                options=[{"value": "x", "label": "X"}],
            ),
            AskUserFormField(
                id="r", type="radio", label="r",
                options=[{"value": "x", "label": "X"}],
            ),
            AskUserFormField(
                id="cg", type="checkbox_group", label="cg",
                options=[{"value": "x", "label": "X"}],
            ),
            AskUserFormField(id="c", type="checkbox", label="c"),
            AskUserFormField(id="sw", type="switch", label="sw"),
            AskUserFormField(id="d", type="date", label="d"),
        ],
    )
    payload = build_form_builder(spec).to_payload()
    types = [f["type"] for f in payload["fields"]]
    assert types == [
        "input", "textarea", "number", "slider", "select",
        "radio-group", "checkbox-group", "checkbox", "switch", "date",
    ]


def test_build_form_builder_select_without_options_raises():
    spec = AskUserForm(
        title="Bad",
        fields=[AskUserFormField(id="x", type="select", label="x")],
    )
    with pytest.raises(ValueError, match="select field 'x' requires options"):
        build_form_builder(spec)


# ---------------------------------------------------------------------------
# _parse_approval_result_to_response
# ---------------------------------------------------------------------------


def test_parse_approved_with_values_in_raw_response():
    raw = {"values": {"reason": "bug", "priority": "high"}}
    out = _parse_approval_result_to_response(
        _approval_result(decision="approved", raw_response=raw)
    )
    assert out.status == "submitted"
    assert out.values == {"reason": "bug", "priority": "high"}


def test_parse_approved_with_values_in_raw_response_nested():
    raw = {"response": {"values": {"a": 1}}}
    out = _parse_approval_result_to_response(
        _approval_result(decision="approved", raw_response=raw)
    )
    assert out.status == "submitted"
    assert out.values == {"a": 1}


def test_parse_rejected_maps_to_cancelled():
    out = _parse_approval_result_to_response(
        _approval_result(decision="rejected", feedback="not now")
    )
    assert out.status == "cancelled"
    assert out.feedback == "not now"


def test_parse_expired_maps_to_timeout():
    out = _parse_approval_result_to_response(
        _approval_result(decision="expired")
    )
    assert out.status == "timeout"


def test_parse_request_changes_maps_to_submitted():
    raw = {"values": {"x": "y"}}
    out = _parse_approval_result_to_response(
        _approval_result(decision="request_changes", raw_response=raw)
    )
    assert out.status == "submitted"
    assert out.values == {"x": "y"}


# ---------------------------------------------------------------------------
# request_user_input_and_pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_user_input_submitted_path():
    spec = AskUserForm(
        title="Question",
        fields=[AskUserFormField(id="x", type="input", label="X")],
    )
    hax_client = MagicMock()
    created = MagicMock(id="req-1", url="https://hax/r/req-1")
    hax_client.create_request = MagicMock(return_value=created)

    app = _silent_app()
    app.pause.return_value = _approval_result(
        decision="approved", raw_response={"values": {"x": "yes"}}
    )

    out = await request_user_input_and_pause(
        app=app, spec=spec, hax_client=hax_client, expires_in_hours=1
    )
    assert out.status == "submitted"
    assert out.values == {"x": "yes"}
    # The form payload sent to hax-sdk had type='form-builder'
    call_kwargs = hax_client.create_request.call_args.kwargs
    assert call_kwargs["type"] == "form-builder"
    assert call_kwargs["title"] == "Question"
    # app.pause was called with the created request's id and url
    app.pause.assert_awaited_once()
    pause_kwargs = app.pause.await_args.kwargs
    assert pause_kwargs["approval_request_id"] == "req-1"
    assert pause_kwargs["approval_request_url"] == "https://hax/r/req-1"


@pytest.mark.asyncio
async def test_request_user_input_timeout_when_pause_times_out():
    spec = AskUserForm(
        title="Q", fields=[AskUserFormField(id="x", type="input", label="X")]
    )
    hax_client = MagicMock()
    hax_client.create_request = MagicMock(
        return_value=MagicMock(id="r", url="u")
    )
    app = _silent_app()
    app.pause.side_effect = asyncio.TimeoutError()

    out = await request_user_input_and_pause(
        app=app, spec=spec, hax_client=hax_client, expires_in_hours=1
    )
    assert out.status == "timeout"


@pytest.mark.asyncio
async def test_request_user_input_cancelled_on_rejected_decision():
    spec = AskUserForm(
        title="Q", fields=[AskUserFormField(id="x", type="input", label="X")]
    )
    hax_client = MagicMock()
    hax_client.create_request = MagicMock(
        return_value=MagicMock(id="r", url="u")
    )
    app = _silent_app()
    app.pause.return_value = _approval_result(
        decision="rejected", feedback="user said no"
    )

    out = await request_user_input_and_pause(
        app=app, spec=spec, hax_client=hax_client, expires_in_hours=1
    )
    assert out.status == "cancelled"
    assert out.feedback == "user said no"


# ---------------------------------------------------------------------------
# run_with_ask_user wrapper
# ---------------------------------------------------------------------------


def _result(action: str, ask: AskUserForm | None = None):
    """Build a stand-in for a reasoner output schema with ``ask_user_form``."""
    m = MagicMock()
    m.action = action
    m.ask_user_form = ask
    m.model_copy = lambda update: _result(
        m.action, update.get("ask_user_form", ask)
    )
    return m


@pytest.mark.asyncio
async def test_wrapper_no_ask_passthrough():
    reasoner_fn = AsyncMock(return_value=_result("DONE"))
    app = _silent_app()

    out = await run_with_ask_user(
        reasoner_fn=reasoner_fn,
        reasoner_kwargs={},
        app=app,
        hax_client=MagicMock(),
        budget=AskUserBudget(remaining=3),
    )
    assert out.action == "DONE"
    reasoner_fn.assert_awaited_once()
    app.pause.assert_not_called()


@pytest.mark.asyncio
async def test_wrapper_hax_disabled_clears_field_and_returns():
    spec = AskUserForm(
        title="Q", fields=[AskUserFormField(id="x", type="input", label="X")]
    )
    reasoner_fn = AsyncMock(return_value=_result("ASKING", ask=spec))
    app = _silent_app()

    out = await run_with_ask_user(
        reasoner_fn=reasoner_fn,
        reasoner_kwargs={},
        app=app,
        hax_client=None,  # HAX disabled
        budget=AskUserBudget(remaining=5),
    )
    assert out.ask_user_form is None
    reasoner_fn.assert_awaited_once()
    app.pause.assert_not_called()


@pytest.mark.asyncio
async def test_wrapper_budget_exhausted_clears_field_no_pause():
    spec = AskUserForm(
        title="Q", fields=[AskUserFormField(id="x", type="input", label="X")]
    )
    reasoner_fn = AsyncMock(return_value=_result("ASKING", ask=spec))
    app = _silent_app()
    budget = AskUserBudget(remaining=0)

    out = await run_with_ask_user(
        reasoner_fn=reasoner_fn,
        reasoner_kwargs={},
        app=app,
        hax_client=MagicMock(),
        budget=budget,
    )
    assert out.ask_user_form is None
    reasoner_fn.assert_awaited_once()
    app.pause.assert_not_called()
    assert budget.remaining == 0


@pytest.mark.asyncio
async def test_wrapper_one_ask_round_then_no_ask():
    spec = AskUserForm(
        title="Pick",
        fields=[AskUserFormField(id="x", type="input", label="X")],
    )
    # First call returns ask, second returns final.
    reasoner_fn = AsyncMock(
        side_effect=[
            _result("ASKING", ask=spec),
            _result("FINAL", ask=None),
        ]
    )

    hax_client = MagicMock()
    hax_client.create_request = MagicMock(
        return_value=MagicMock(id="req-99", url="https://hax/r/req-99")
    )

    app = _silent_app()
    app.pause.return_value = _approval_result(
        decision="approved", raw_response={"values": {"x": "answer"}}
    )

    budget = AskUserBudget(remaining=5)
    out = await run_with_ask_user(
        reasoner_fn=reasoner_fn,
        reasoner_kwargs={"prior_user_responses": []},
        app=app,
        hax_client=hax_client,
        budget=budget,
    )

    assert out.action == "FINAL"
    assert out.ask_user_form is None
    assert reasoner_fn.await_count == 2
    second_call = reasoner_fn.await_args_list[1]
    prior = second_call.kwargs["prior_user_responses"]
    assert len(prior) == 1
    assert prior[0]["question"] == "Pick"
    assert prior[0]["status"] == "submitted"
    assert prior[0]["values"] == {"x": "answer"}
    assert budget.remaining == 4


@pytest.mark.asyncio
async def test_wrapper_max_iterations_clears_field_after_exhaust():
    spec = AskUserForm(
        title="Q",
        fields=[AskUserFormField(id="x", type="input", label="X")],
    )
    # Reasoner ALWAYS asks again.
    reasoner_fn = AsyncMock(return_value=_result("ASKING", ask=spec))

    hax_client = MagicMock()
    hax_client.create_request = MagicMock(
        return_value=MagicMock(id="r", url="u")
    )

    app = _silent_app()
    app.pause.return_value = _approval_result(
        decision="approved", raw_response={"values": {"x": "yes"}}
    )

    budget = AskUserBudget(remaining=10)  # large; max_iterations is the cap
    out = await run_with_ask_user(
        reasoner_fn=reasoner_fn,
        reasoner_kwargs={"prior_user_responses": []},
        app=app,
        hax_client=hax_client,
        budget=budget,
        max_iterations=3,
    )
    assert out.ask_user_form is None
    # First 3 iterations pause; the 4th iteration call hits max_iterations
    # check before pausing and returns the cleared result.
    assert app.pause.await_count == 3
    assert budget.remaining == 10 - 3
