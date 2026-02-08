import unittest

from agent_ai.client import AgentAIConfig
from agent_ai.factory import build_provider_client
from execution.schemas import BuildConfig, ExecutionConfig


class AgentAIProviderTests(unittest.TestCase):
    def test_config_defaults(self) -> None:
        self.assertEqual(BuildConfig().ai_provider, "claude")
        self.assertEqual(ExecutionConfig().ai_provider, "claude")

    def test_codex_provider_factory(self) -> None:
        cfg = AgentAIConfig(provider="codex")
        client = build_provider_client(cfg)
        self.assertEqual(client.__class__.__name__, "CodexProviderClient")


if __name__ == "__main__":
    unittest.main()
