"""LLM-facing ``ask_user_via_form`` primitive.

Reasoners that may want to clarify something with the human user emit an
``AskUserForm`` as part of their structured output. The wrapper in
``swe_af.hitl.wrapper`` detects the field, calls into this module to build a
Hax form, issues ``app.pause()``, and resumes once the user submits.

The full workflow is paused (potentially for hours / days) while the form is
outstanding — same mechanism as the existing Phase 1.5 plan-approval gate in
``swe_af.app``. The reasoner's own time budget does not accrue during the
pause because agentfield's pause-clock handles the wait separately.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from hax import HaxClient


def build_hax_client_from_env() -> HaxClient | None:
    """Construct a ``HaxClient`` from ``HAX_API_KEY`` / ``HAX_SDK_URL`` env vars.

    Returns ``None`` when ``HAX_API_KEY`` is unset or empty — callers should
    treat that as "HAX disabled" and short-circuit any ask-user logic.
    """
    api_key = os.environ.get("HAX_API_KEY", "").strip()
    if not api_key:
        return None
    from hax import HaxClient

    return HaxClient(
        api_key=api_key,
        base_url=os.environ.get("HAX_SDK_URL", "http://localhost:3000") + "/api/v1",
    )


def approval_webhook_url(app: Any) -> str | None:
    """Resolve the control-plane webhook URL used for ``app.pause`` callbacks.

    Mirrors the URL the existing Phase 1.5 plan-approval gate uses
    (``{cp_base_url}/api/v1/webhooks/approval-response``). Returns ``None``
    when neither the app nor the env supplies a control-plane URL.
    """
    cp_base = (
        getattr(app, "agentfield_server", None)
        or os.environ.get("AGENTFIELD_SERVER")
        or ""
    ).rstrip("/")
    if not cp_base:
        return None
    return f"{cp_base}/api/v1/webhooks/approval-response"


FieldType = Literal[
    "input",
    "number",
    "textarea",
    "select",
    "radio",
    "checkbox",
    "checkbox_group",
    "switch",
    "slider",
    "date",
]


class AskUserFormField(BaseModel):
    """One field in a form the agent is constructing for the user."""

    id: str = Field(
        description=(
            "Unique identifier for this field; becomes the key in the "
            "submitted values dict the reasoner sees on the next invocation."
        )
    )
    type: FieldType = Field(
        description=(
            "Field widget. 'input' = single-line text; 'textarea' = multi-line; "
            "'select' / 'radio' / 'checkbox_group' require an `options` list; "
            "'checkbox' / 'switch' = boolean; 'number' / 'slider' = numeric; "
            "'date' = ISO date."
        )
    )
    label: str = Field(description="Label shown above the field in the form.")
    description: str | None = Field(
        default=None,
        description="Optional helper text shown below the label.",
    )
    required: bool = Field(default=False)
    placeholder: str | None = Field(
        default=None,
        description="Placeholder for input/textarea/number/select widgets.",
    )
    default_value: Any | None = Field(
        default=None,
        description="Pre-filled value if the user submits without changing it.",
    )
    options: list[dict[str, str]] | None = Field(
        default=None,
        description=(
            "Required for 'select', 'radio', 'checkbox_group'. Each entry is "
            "{'value': ..., 'label': ...}. The submitted value is the 'value' "
            "string (or a list of them for checkbox_group)."
        ),
    )
    min: float | None = Field(
        default=None,
        description="Minimum value for 'number' / 'slider'.",
    )
    max: float | None = Field(
        default=None,
        description="Maximum value for 'number' / 'slider'.",
    )
    step: float | None = Field(
        default=None,
        description="Step increment for 'number' / 'slider'.",
    )


class AskUserForm(BaseModel):
    """A form the agent wants the user to fill out before continuing."""

    title: str = Field(
        description=(
            "Short title displayed at the top of the form. "
            "Visible to the user as the question summary."
        )
    )
    description: str | None = Field(
        default=None,
        description=(
            "Optional longer-form context for why the agent is asking. "
            "Render as the form's subtitle."
        ),
    )
    fields: list[AskUserFormField] = Field(
        min_length=1,
        description="At least one field. Use 'radio' or 'select' for multiple-choice asks.",
    )
    submit_label: str = Field(
        default="Submit",
        description="Submit button label.",
    )


class AskUserResponse(BaseModel):
    """What the wrapper hands back to the reasoner on re-invocation."""

    status: Literal["submitted", "timeout", "cancelled", "error"]
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="Submitted form values keyed by field id. Empty on non-submit outcomes.",
    )
    feedback: str | None = Field(
        default=None,
        description="Free-text feedback from the user, if any.",
    )
    error: str | None = Field(
        default=None,
        description="Populated only on status='error'.",
    )


def format_prior_user_responses(prior: list[dict] | None) -> str:
    """Render ``prior_user_responses`` as a markdown block for the LLM prompt.

    When the wrapper re-invokes a reasoner after a paused ask, it stuffs
    accumulated responses into ``prior_user_responses``. The reasoner must
    surface them to the LLM so the LLM doesn't repeat questions already
    answered.
    """
    if not prior:
        return ""
    lines = ["## Prior Clarification From User", ""]
    for idx, entry in enumerate(prior, start=1):
        question = entry.get("question", "(no title)")
        status = entry.get("status", "unknown")
        lines.append(f"### Question {idx}: {question}")
        lines.append(f"_Status: {status}_")
        values = entry.get("values") or {}
        if values:
            lines.append("")
            lines.append("Values submitted by user:")
            for key, val in values.items():
                lines.append(f"- **{key}**: {val}")
        feedback = entry.get("feedback")
        if feedback:
            lines.append("")
            lines.append(f"User feedback: {feedback}")
        lines.append("")
    lines.append(
        "USE THESE PRIOR ANSWERS. DO NOT RE-ASK THE SAME QUESTIONS. Only "
        "emit `ask_user_form` if you need DIFFERENT clarification not already "
        "covered above."
    )
    return "\n".join(lines)


def _field_to_form_builder_call(form: Any, field: AskUserFormField) -> None:
    """Invoke the right FormBuilder method for one ``AskUserFormField``."""
    common: dict[str, Any] = {"label": field.label}
    if field.description is not None:
        common["description"] = field.description
    if field.required:
        common["required"] = True
    if field.placeholder is not None:
        common["placeholder"] = field.placeholder
    if field.default_value is not None:
        common["default_value"] = field.default_value

    ftype = field.type

    if ftype == "input":
        form.input(field.id, **common)
    elif ftype == "textarea":
        form.textarea(field.id, **common)
    elif ftype == "number":
        kwargs = dict(common)
        if field.min is not None:
            kwargs["min"] = field.min
        if field.max is not None:
            kwargs["max"] = field.max
        if field.step is not None:
            kwargs["step"] = field.step
        form.number(field.id, **kwargs)
    elif ftype == "slider":
        if field.min is None or field.max is None:
            raise ValueError(
                f"slider field '{field.id}' requires both min and max"
            )
        kwargs = dict(common)
        kwargs["min"] = field.min
        kwargs["max"] = field.max
        if field.step is not None:
            kwargs["step"] = field.step
        form.slider(field.id, **kwargs)
    elif ftype == "select":
        if not field.options:
            raise ValueError(f"select field '{field.id}' requires options")
        form.select(field.id, options=field.options, **common)
    elif ftype == "radio":
        if not field.options:
            raise ValueError(f"radio field '{field.id}' requires options")
        form.radio_group(field.id, options=field.options, **common)
    elif ftype == "checkbox_group":
        if not field.options:
            raise ValueError(
                f"checkbox_group field '{field.id}' requires options"
            )
        form.checkbox_group(field.id, options=field.options, **common)
    elif ftype == "checkbox":
        common.pop("placeholder", None)
        form.checkbox(field.id, checkbox_label=field.label, **common)
    elif ftype == "switch":
        common.pop("placeholder", None)
        form.switch(field.id, switch_label=field.label, **common)
    elif ftype == "date":
        form.date(field.id, **common)
    else:
        raise ValueError(f"unsupported AskUserFormField type: {ftype}")


def build_form_builder(spec: AskUserForm) -> Any:
    """Translate an ``AskUserForm`` spec into a ``hax.FormBuilder`` instance.

    Imports ``hax`` lazily so this module is importable in environments without
    the SDK installed (mirrors the existing pattern in ``swe_af.app``).
    """
    from hax import FormBuilder

    form = FormBuilder().title(spec.title)
    if spec.description is not None:
        form.description(spec.description)
    if spec.submit_label and spec.submit_label != "Submit":
        form.submit_label(spec.submit_label)

    for field in spec.fields:
        _field_to_form_builder_call(form, field)

    return form


HAX_CREATE_REQUEST_TIMEOUT_SECONDS = 120.0


async def _create_hax_form_request_with_timeout(
    *,
    app: Any,
    hax_client: HaxClient,
    form: Any,
    title: str,
    description: str | None,
    expires_in_seconds: int,
    user_id: str | None,
    webhook_url: str | None,
    metadata: dict[str, Any] | None,
    timeout_seconds: float = HAX_CREATE_REQUEST_TIMEOUT_SECONDS,
) -> Any:
    """Submit a hax-sdk form-builder request with a hard timeout.

    Mirrors ``swe_af.app._create_hax_request_with_timeout`` but for form-builder
    requests routed through ``hax_client.create_request(type='form-builder', ...)``
    so we can also pass ``user_id`` (``create_form_request`` itself doesn't expose
    it). Returns the ``CreatedRequest`` from hax-sdk; the caller passes
    ``request.id`` and ``request.url`` to ``app.pause``.
    """
    app.note(
        f"ask_user: submitting hax form-builder request ({title!r})",
        tags=["ask_user", "hax", "create_form_request"],
    )

    kwargs: dict[str, Any] = {
        "type": "form-builder",
        "payload": form.to_payload(),
        "title": title,
        "expires_in_seconds": expires_in_seconds,
    }
    if description is not None:
        kwargs["description"] = description
    if user_id is not None:
        kwargs["user_id"] = user_id
    if webhook_url is not None:
        kwargs["webhook_url"] = webhook_url
    if metadata is not None:
        kwargs["metadata"] = metadata

    try:
        created = await asyncio.wait_for(
            asyncio.to_thread(hax_client.create_request, **kwargs),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        app.note(
            f"ask_user: hax create_request timed out after {timeout_seconds}s",
            tags=["ask_user", "hax", "timeout"],
        )
        raise RuntimeError(
            f"hax-sdk create_request (form-builder) timed out after "
            f"{timeout_seconds}s; hax-sdk is likely wedged."
        ) from exc
    except Exception as exc:
        app.note(
            f"ask_user: hax create_request raised "
            f"{type(exc).__name__}: {exc}",
            tags=["ask_user", "hax", "error"],
        )
        raise
    app.note(
        f"ask_user: hax form request created (request_id={created.id})",
        tags=["ask_user", "hax", "submitted"],
    )
    return created


def _extract_values_from_raw(raw: Any) -> dict[str, Any]:
    """Find the form values dict inside an ApprovalResult.raw_response payload."""
    if not isinstance(raw, dict):
        return {}
    direct = raw.get("values")
    if isinstance(direct, dict):
        return dict(direct)
    response_obj = raw.get("response")
    if isinstance(response_obj, dict):
        inner = response_obj.get("values")
        if isinstance(inner, dict):
            return dict(inner)
    return {}


def _parse_approval_result_to_response(approval_result: Any) -> AskUserResponse:
    """Convert an agentfield ``ApprovalResult`` into ``AskUserResponse``.

    Mapping:
      decision='approved'        → status='submitted'
      decision='request_changes' → status='submitted' (form was filled out)
      decision='rejected'        → status='cancelled'
      decision='expired'         → status='timeout'
      decision='error'           → status='error'
      anything else              → status='error' (defensive)

    Form values are pulled from ``raw_response['values']`` or
    ``raw_response['response']['values']`` if present, with a final fallback
    to parsing ``feedback`` as JSON.
    """
    decision = getattr(approval_result, "decision", None) or ""
    feedback = getattr(approval_result, "feedback", "") or None
    raw = getattr(approval_result, "raw_response", None)

    if decision == "rejected":
        return AskUserResponse(status="cancelled", feedback=feedback)
    if decision == "expired":
        return AskUserResponse(status="timeout", feedback=feedback)
    if decision == "error":
        return AskUserResponse(
            status="error",
            feedback=feedback,
            error=feedback or "agentfield reported decision=error",
        )

    values = _extract_values_from_raw(raw)
    if not values and feedback:
        try:
            parsed = json.loads(feedback)
            if isinstance(parsed, dict):
                values = parsed
        except (ValueError, TypeError):
            pass

    if decision in {"approved", "request_changes", "submitted"}:
        return AskUserResponse(
            status="submitted",
            values=values,
            feedback=feedback,
        )

    return AskUserResponse(
        status="error",
        values=values,
        feedback=feedback,
        error=f"unknown decision: {decision!r}",
    )


async def request_user_input_and_pause(
    *,
    app: Any,
    spec: AskUserForm,
    hax_client: HaxClient,
    expires_in_hours: float = 24,
    user_id: str | None = None,
    execution_id: str | None = None,
    webhook_url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AskUserResponse:
    """Build a Hax form, pause the workflow, return the human's response.

    Pause duration is bounded by ``expires_in_hours`` (default 24h). The
    workflow is genuinely suspended on the control plane while the form is
    outstanding — the reasoner's own time budget does not accrue.
    """
    try:
        form = build_form_builder(spec)
    except Exception as exc:
        app.note(
            f"ask_user: failed to build form from spec: {exc}",
            tags=["ask_user", "form_builder", "error"],
        )
        return AskUserResponse(
            status="error",
            error=f"Failed to build form from spec: {exc}",
        )

    try:
        created = await _create_hax_form_request_with_timeout(
            app=app,
            hax_client=hax_client,
            form=form,
            title=spec.title,
            description=spec.description,
            expires_in_seconds=int(expires_in_hours * 3600),
            user_id=user_id,
            webhook_url=webhook_url,
            metadata=metadata,
        )
    except Exception as exc:
        return AskUserResponse(
            status="error",
            error=f"create_form_request failed: {exc}",
        )

    pause_kwargs: dict[str, Any] = {
        "approval_request_id": created.id,
        "approval_request_url": created.url,
        "expires_in_hours": expires_in_hours,
    }
    if execution_id:
        pause_kwargs["execution_id"] = execution_id

    try:
        approval_result = await app.pause(**pause_kwargs)
    except asyncio.TimeoutError:
        app.note(
            "ask_user: pause expired without human response",
            tags=["ask_user", "pause", "timeout"],
        )
        return AskUserResponse(status="timeout")
    except Exception as exc:
        app.note(
            f"ask_user: pause raised {type(exc).__name__}: {exc}",
            tags=["ask_user", "pause", "error"],
        )
        return AskUserResponse(
            status="error",
            error=f"pause failed: {exc}",
        )

    response = _parse_approval_result_to_response(approval_result)
    app.note(
        f"ask_user: response received "
        f"(status={response.status}, {len(response.values)} value(s))",
        tags=["ask_user", "hax", "response", response.status],
    )
    return response
