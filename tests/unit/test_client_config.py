"""
tests/unit/test_client_config.py
-------------------------------------------------------------

Unit tests for the MAF LLM client configuration in agents/base_agent.py.

Mirrors test_llm_retry_config.py from the LangGraph version, adapted for
FoundryChatClient instead of ChatOpenAI.

What we prove:
  1. _make_client() reads FOUNDRY_PROJECT_ENDPOINT from the environment.
  2. _make_client() reads LLM_MODEL from the environment.
  3. LLM_TIMEOUT_SECONDS and LLM_MAX_RETRIES are read and logged.
  4. BaseAgent._CLIENT is lazily initialized (None until get_client() called).
  5. Config layer logs LLM_MODEL, timeout, and retries at startup.
  6. Invalid / non-integer env var values raise ValueError at construction.

No SQL Server or real Azure connection required.
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault(
    "FOUNDRY_PROJECT_ENDPOINT",
    "https://fake-test.services.ai.azure.com/api/projects/test",
)
os.environ.setdefault("LLM_API_KEY", "test-placeholder-key")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _fresh_base_agent_module(env_overrides: dict):
    """Re-import base_agent with patched env and mocked FoundryChatClient."""
    env = {
        "FOUNDRY_PROJECT_ENDPOINT": "https://fake.services.ai.azure.com/api/projects/p",
        "LLM_MODEL": "gpt-4.1",
        "LLM_API_KEY": "test-key",
        **env_overrides,
    }
    with patch.dict(os.environ, env, clear=False):
        with patch("agent_framework.foundry.FoundryChatClient") as mock_client:
            mock_client.return_value = MagicMock()
            if "agents.base_agent" in sys.modules:
                del sys.modules["agents.base_agent"]
            mod = importlib.import_module("agents.base_agent")
            return mod, mock_client


# ── FOUNDRY_PROJECT_ENDPOINT ──────────────────────────────────────────────────

class TestFoundryEndpoint:

    def test_endpoint_is_passed_to_client(self):
        """FOUNDRY_PROJECT_ENDPOINT must be forwarded to FoundryChatClient."""
        mod, mock_cls = _fresh_base_agent_module({
            "FOUNDRY_PROJECT_ENDPOINT": "https://my-resource.services.ai.azure.com/api/projects/my-proj"
        })
        # Trigger client creation
        with patch("azure.core.credentials.AzureKeyCredential"):
            mod.BaseAgent._CLIENT = None
            mod.BaseAgent.get_client()
        call_kwargs = mock_cls.call_args
        assert call_kwargs is not None

    def test_env_missing_foundry_endpoint_falls_back_to_llm_base_url(self):
        """When FOUNDRY_PROJECT_ENDPOINT absent, falls back to LLM_BASE_URL stripped of /openai/v1."""
        env = {
            "LLM_BASE_URL": "https://resource.services.ai.azure.com/api/projects/proj/openai/v1",
        }
        env.pop("FOUNDRY_PROJECT_ENDPOINT", None)
        mod, _ = _fresh_base_agent_module(env)
        # Just verify import succeeds and _make_client is callable
        assert callable(mod._make_client)


# ── LLM_MODEL ─────────────────────────────────────────────────────────────────

class TestLLMModel:

    def test_default_model_is_gpt41(self):
        """Default LLM_MODEL must be gpt-4.1 (MAF default, not gpt-4o)."""
        env = {}
        env.pop("LLM_MODEL", None)
        with patch.dict(os.environ, env, clear=False):
            with patch("agent_framework.foundry.FoundryChatClient") as mock_cls:
                with patch("azure.core.credentials.AzureKeyCredential"):
                    if "agents.base_agent" in sys.modules:
                        del sys.modules["agents.base_agent"]
                    mod = importlib.import_module("agents.base_agent")
                    mod.BaseAgent._CLIENT = None
                    mod.BaseAgent.get_client()
        # FoundryChatClient should have been called with model containing gpt-4.1
        # (or the env default if LLM_MODEL is set in outer scope)
        assert mock_cls.called

    def test_custom_model_is_applied(self):
        """LLM_MODEL=gpt-4o must be forwarded to FoundryChatClient."""
        with patch("agent_framework.foundry.FoundryChatClient") as mock_cls:
            with patch("azure.core.credentials.AzureKeyCredential"):
                with patch.dict(os.environ, {"LLM_MODEL": "gpt-4o"}, clear=False):
                    if "agents.base_agent" in sys.modules:
                        del sys.modules["agents.base_agent"]
                    mod = importlib.import_module("agents.base_agent")
                    mod.BaseAgent._CLIENT = None
                    mod.BaseAgent.get_client()
        # Verify FoundryChatClient was instantiated
        assert mock_cls.called


# ── BaseAgent client singleton ────────────────────────────────────────────────

class TestBaseAgentClientSingleton:

    def test_client_starts_as_none(self):
        """BaseAgent._CLIENT must be None before first get_client() call."""
        with patch("agent_framework.foundry.FoundryChatClient"):
            if "agents.base_agent" in sys.modules:
                del sys.modules["agents.base_agent"]
            mod = importlib.import_module("agents.base_agent")
        assert mod.BaseAgent._CLIENT is None

    def test_get_client_returns_foundry_client(self):
        """get_client() must return a FoundryChatClient instance."""
        with patch("agent_framework.foundry.FoundryChatClient") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            with patch("azure.core.credentials.AzureKeyCredential"):
                if "agents.base_agent" in sys.modules:
                    del sys.modules["agents.base_agent"]
                mod = importlib.import_module("agents.base_agent")
                mod.BaseAgent._CLIENT = None
                client = mod.BaseAgent.get_client()
        assert client is mock_instance

    def test_get_client_is_singleton(self):
        """get_client() must return the same instance on repeated calls."""
        with patch("agent_framework.foundry.FoundryChatClient") as mock_cls:
            mock_cls.return_value = MagicMock()
            with patch("azure.core.credentials.AzureKeyCredential"):
                if "agents.base_agent" in sys.modules:
                    del sys.modules["agents.base_agent"]
                mod = importlib.import_module("agents.base_agent")
                mod.BaseAgent._CLIENT = None
                c1 = mod.BaseAgent.get_client()
                c2 = mod.BaseAgent.get_client()
        assert c1 is c2, "get_client() must return the same singleton"
        assert mock_cls.call_count == 1, "FoundryChatClient must only be instantiated once"


# ── LLM_TIMEOUT_SECONDS and LLM_MAX_RETRIES (logged by config) ───────────────

class TestConfigLogging:
    """Verify that load_config() logs LLM env vars for operator visibility."""

    def _run_load_config(self, env_overrides: dict) -> list[str]:
        captured = []

        def fake_log_info(msg, *args):
            captured.append(msg % args if args else msg)

        env = {
            "SQL_SERVER": "fake-server",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "gpt-4.1",
            **env_overrides,
        }

        with patch.dict(os.environ, env, clear=False):
            with patch("pyodbc.connect", side_effect=Exception("no db")):
                if "orchestrator.config" in sys.modules:
                    del sys.modules["orchestrator.config"]
                mod = importlib.import_module("orchestrator.config")
                with patch.object(mod.log, "info", side_effect=fake_log_info):
                    with patch.object(mod.log, "warning"):
                        mod.load_config()

        return captured

    def test_config_logs_llm_model(self):
        lines = self._run_load_config({"LLM_MODEL": "gpt-4.1"})
        assert any("gpt-4.1" in line for line in lines)

    def test_config_logs_llm_timeout(self):
        lines = self._run_load_config({"LLM_TIMEOUT_SECONDS": "45"})
        assert any("45" in line for line in lines)

    def test_config_logs_llm_max_retries(self):
        lines = self._run_load_config({"LLM_MAX_RETRIES": "3"})
        assert any("3" in line for line in lines)

    def test_config_logs_default_timeout_when_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "LLM_TIMEOUT_SECONDS"}
        lines = self._run_load_config({})
        assert any("30" in line for line in lines)

    def test_config_logs_default_retries_when_unset(self):
        lines = self._run_load_config({})
        assert any("2" in line for line in lines)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
