"""
tests/conftest.py — pytest configuration for ms-blocking-agent tests.

Sets FOUNDRY_PROJECT_ENDPOINT placeholder before any test module imports
agents (which instantiate BaseAgent at class-definition time).
"""
import os

os.environ.setdefault(
    "FOUNDRY_PROJECT_ENDPOINT",
    "https://fake-test.services.ai.azure.com/api/projects/test",
)
os.environ.setdefault("LLM_API_KEY", "test-placeholder-key")
