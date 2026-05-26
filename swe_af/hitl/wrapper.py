"""Reasoner-call wrapper that handles the ``ask_user_via_form`` loop.

A reasoner participates in ask-user by declaring an optional
``ask_user_form: AskUserForm | None = None`` field on its output schema.

When the reasoner emits a non-None value, this wrapper:

1. Builds a Hax form from the spec, calls ``app.pause()``, and waits for the
   human's response.
2. Re-invokes the same reasoner with the user's answers appended to a
   ``prior_user_responses`` kwarg.
3. Repeats until the reasoner emits ``ask_user_form=None`` or budget /
   iteration cap is reached.

When Hax is disabled (``hax_client is None``) or the per-build budget is
exhausted, the wrapper still calls the reasoner once but strips any
``ask_user_form`` from the result so downstream code never sees an unfulfilled
ask.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pydantic import BaseModel, Field

from swe_af.hitl.ask_user import (
    AskUserForm,
    AskUserResponse,
    request_user_input_and_pause,
)

if TYPE_CHECKING:
    from hax import HaxClient


class AskUserBudget(BaseModel):
    """Per-build cap on ``ask_user_via_form`` invocations across all reasoners."""

    remaining: int = Field(
        default=5,
        description=(
            "Number of ask-user invocations left in this build() execution. "
            "Shared across all wrapped reasoner call sites. When 0, the "
            "wrapper refuses to issue further pauses."
        ),
    )


class PriorUserResponse(BaseModel):
    """One entry in the ``prior_user_responses`` list passed to a reasoner."""

    question: str
    status: str
    values: dict[str, Any] = Field(default_factory=dict)
    feedback: str | None = None


def _clear_ask_user_form(result: BaseModel) -> BaseModel:
    """Return a copy of ``result`` with ``ask_user_form`` set to None if present."""
    if hasattr(result, "ask_user_form"):
        try:
            return result.model_copy(update={"ask_user_form": None})
        except Exception:
            return result
    return result


def _extract_ask_user_form(result: BaseModel) -> AskUserForm | None:
    """Pull the ``ask_user_form`` field off a reasoner output, if present and populated."""
    raw = getattr(result, "ask_user_form", None)
    if raw is None:
        return None
    if isinstance(raw, AskUserForm):
        return raw
    if isinstance(raw, dict):
        return AskUserForm.model_validate(raw)
    return AskUserForm.model_validate(raw)


async def run_with_ask_user(
    *,
    reasoner_fn: Callable[..., Awaitable[BaseModel]],
    reasoner_kwargs: dict[str, Any],
    app: Any,
    hax_client: HaxClient | None,
    budget: AskUserBudget,
    expires_in_hours: float = 24,
    user_id: str | None = None,
    execution_id: str | None = None,
    webhook_url: str | None = None,
    max_iterations: int = 3,
    note_label: str | None = None,
) -> BaseModel:
    """Call ``reasoner_fn`` with the ask-user pause/resume loop applied.

    Returns the final reasoner output with ``ask_user_form`` cleared.
    """
    label = note_label or getattr(reasoner_fn, "__name__", "reasoner")
    kwargs = dict(reasoner_kwargs)
    kwargs.setdefault("prior_user_responses", [])

    for iteration in range(max_iterations + 1):
        result = await reasoner_fn(**kwargs)
        spec = _extract_ask_user_form(result)

        if spec is None:
            return result

        if hax_client is None:
            app.note(
                f"{label}: LLM emitted ask_user_form but HAX is disabled — "
                f"ignoring and proceeding with current decision",
                tags=["ask_user", "skipped", "hax_disabled"],
            )
            return _clear_ask_user_form(result)

        if budget.remaining <= 0:
            app.note(
                f"{label}: ask_user budget exhausted (remaining=0) — "
                f"ignoring further asks and proceeding",
                tags=["ask_user", "skipped", "budget_exhausted"],
            )
            return _clear_ask_user_form(result)

        if iteration >= max_iterations:
            app.note(
                f"{label}: ask_user max_iterations ({max_iterations}) "
                f"reached without converging — proceeding",
                tags=["ask_user", "skipped", "max_iterations"],
            )
            return _clear_ask_user_form(result)

        budget.remaining -= 1
        app.note(
            f"{label}: pausing for ask_user_via_form "
            f"(iteration {iteration}, budget_remaining={budget.remaining})",
            tags=["ask_user", "pause", label],
        )

        response: AskUserResponse = await request_user_input_and_pause(
            app=app,
            spec=spec,
            hax_client=hax_client,
            expires_in_hours=expires_in_hours,
            user_id=user_id,
            execution_id=execution_id,
            webhook_url=webhook_url,
        )

        prior = list(kwargs.get("prior_user_responses") or [])
        prior.append(
            PriorUserResponse(
                question=spec.title,
                status=response.status,
                values=response.values,
                feedback=response.feedback,
            ).model_dump()
        )
        kwargs["prior_user_responses"] = prior

    return _clear_ask_user_form(result)
