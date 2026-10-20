"""Replies generated offline by the bundled CLI's exact formatter functions."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk._internal.message_parser import parse_message

from swe_af.execution.fatal_error import FatalHarnessError, check_fatal_harness_error

FIXTURES = json.loads((Path(__file__).parent / "fixtures/claude_cli_2_1_9/cli-fixtures.json").read_text())


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda x: x["label"])
@pytest.mark.parametrize("path", ["raw", "wire", "parsed_wire"])
def test_cli_generated_reply_is_fatal(fixture, path):
    wire = {"type": "assistant", "message": {
        "model": "<synthetic>",
        "content": [{"type": "text", "text": fixture["reply"]}],
    }, "error": fixture.get("error", "rate_limit")}
    result = SimpleNamespace(is_error=True, error_message="Schema validation failed", messages=[])
    if path == "raw":
        result.result = fixture["reply"]
    else:
        result.messages = [wire if path == "wire" else parse_message(wire)]
    with pytest.raises(FatalHarnessError):
        check_fatal_harness_error(result)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda x: x["label"])
@pytest.mark.parametrize("template", [
    "{reply} is an example in the docs.",
    "{reply}\nPlease add a regression test.",
    'Schema validation failed. Invalid output: {{"example": "{reply}"',
])
@pytest.mark.parametrize("field", ["result", "error_message"])
def test_cli_reply_with_prose_or_in_diagnosis_is_recoverable(fixture, template, field):
    values = dict(is_error=True, error_message="Schema validation failed", messages=[])
    values[field] = template.format(reply=fixture["reply"])
    check_fatal_harness_error(SimpleNamespace(**values))
