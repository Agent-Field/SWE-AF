"""Keep the optional OpenCode MCP preset anonymous and opt-in."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_parallel_is_disabled_and_anonymous_by_default():
    server = json.loads((ROOT / "opencode.json").read_text())["mcp"]["parallel"]
    assert server["enabled"] is False
    assert server["oauth"] is False
    assert server["type"] == "remote"
    assert server["url"] == "https://search.parallel.ai/mcp"
    assert server["headers"] == {
        "User-Agent": "SWE-AF/0.1.0 (https://github.com/Agent-Field/SWE-AF)"
    }


def test_documented_override_enables_the_same_server():
    readme = (ROOT / "README.md").read_text()
    match = re.search(r"export OPENCODE_CONFIG_CONTENT='([^'\n]+)'", readme)
    assert match is not None
    override = json.loads(match[1])
    configured = json.loads((ROOT / "opencode.json").read_text())["mcp"]["parallel"]
    assert override == {"mcp": {"parallel": {**configured, "enabled": True}}}
