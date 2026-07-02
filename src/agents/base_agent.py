"""
agents/base_agent.py
--------------------
Parent class shared by all agent nodes.  Mirrors the LangGraph version's
interface exactly -- same SQL helpers, same ask_llm / ask_llm_json signatures
-- but replaces ChatOpenAI with Microsoft Agent Framework's FoundryChatClient
and ChatAgent, and makes the LLM calls async.

LLM provider is configured via .env:
  FOUNDRY_PROJECT_ENDPOINT  -- Azure AI Foundry project endpoint
                               e.g. https://<resource>.services.ai.azure.com/api/projects/<project>
  LLM_MODEL                 -- model/deployment name (default: gpt-4.1)
  LLM_API_KEY               -- API key (uses DefaultAzureCredential if not set)
  LLM_TIMEOUT_SECONDS       -- per-request HTTP timeout in seconds (default: 30)
  LLM_MAX_RETRIES           -- retry attempts on transient errors (default: 2)

See .env.example for a ready-made configuration template.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import re

import pyodbc
from agent_framework import ChatAgent
from agent_framework.foundry import FoundryChatClient

log = logging.getLogger("base_agent")

_LOCK_TIMEOUT_MS = 30_000


def _make_client() -> FoundryChatClient:
    """Build the shared FoundryChatClient from environment variables."""
    api_key = os.getenv("LLM_API_KEY", "")

    if api_key and "PASTE" not in api_key:
        # Key-credential auth (mirrors LangGraph LLM_API_KEY usage)
        from azure.core.credentials import AzureKeyCredential
        credential = AzureKeyCredential(api_key)
    else:
        # Managed identity / CLI auth (az login)
        from azure.identity import DefaultAzureCredential
        credential = DefaultAzureCredential()

    return FoundryChatClient(
        project_endpoint=os.getenv(
            "FOUNDRY_PROJECT_ENDPOINT",
            os.getenv("LLM_BASE_URL", "").replace("/openai/v1", ""),
        ),
        model=os.getenv("LLM_MODEL", "gpt-4.1"),
        credential=credential,
    )


class BaseAgent:
    # Shared FoundryChatClient (connection-level singleton, one per process).
    # Each ask_llm call creates a lightweight ChatAgent wrapper around it.
    _CLIENT: FoundryChatClient | None = None

    @classmethod
    def get_client(cls) -> FoundryChatClient:
        if cls._CLIENT is None:
            cls._CLIENT = _make_client()
        return cls._CLIENT

    def __init__(self, config: dict):
        self.config = config
        self.monitor_conn_str = config["monitor_conn_str"]
        self.log_conn_str = config["log_conn_str"]

    # ── SQL helpers ────────────────────────────────────────────────────────────

    def query_sql(self, conn_str: str, sql: str, params: list = None) -> list[dict]:
        """Run a SELECT query. Returns a list of row-dicts."""
        with pyodbc.connect(conn_str, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SET LOCK_TIMEOUT {_LOCK_TIMEOUT_MS}")
            cursor.execute(sql, params or [])
            columns = [col[0] for col in cursor.description]
            rows = []
            for row in cursor.fetchall():
                r = {}
                for col, val in zip(columns, row):
                    if isinstance(val, (bytes, bytearray)):
                        r[col] = val.hex()
                    else:
                        r[col] = val
                rows.append(r)
            return rows

    def execute_sql(self, conn_str: str, sql: str, params: list = None) -> None:
        """Run an INSERT / UPDATE / EXEC statement."""
        with pyodbc.connect(conn_str, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SET LOCK_TIMEOUT {_LOCK_TIMEOUT_MS}")
            cursor.execute(sql, params or [])
            conn.commit()

    # ── LLM helpers (async -- MAF ChatAgent.run() is a coroutine) ─────────────

    async def ask_llm(self, system_prompt: str, user_message: str) -> str:
        """Send a prompt to the LLM and return the raw text response."""
        agent = ChatAgent(
            chat_client=self.get_client(),
            instructions=system_prompt,
            temperature=0,
            max_tokens=4096,
        )
        result = await agent.run(user_message)
        return result.content if hasattr(result, "content") else str(result)

    async def ask_llm_json(self, system_prompt: str, user_message: str) -> dict:
        """
        Send a prompt to the LLM and parse the response as JSON.
        Handles cases where the LLM wraps JSON in markdown code fences.
        """
        full_prompt = (
            user_message
            + "\n\nIMPORTANT: Reply with valid JSON only. No markdown, no explanation outside the JSON."
        )
        raw = await self.ask_llm(system_prompt, full_prompt)

        # Strip ```json ... ``` fences if present
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fence:
            raw = fence.group(1)
        else:
            brace = re.search(r"\{.*\}", raw, re.DOTALL)
            if brace:
                raw = brace.group()

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("JSON parse failed: %s  raw=%s", e, raw[:300])
            raise

    def ask_llm_json_sync(self, system_prompt: str, user_message: str) -> dict:
        """
        Synchronous bridge for non-async tool functions (e.g. tools/rca.py).
        Runs the async ask_llm_json in a dedicated thread with its own event loop,
        which avoids "cannot run nested event loop" errors in the MAF async context.
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                self.ask_llm_json(system_prompt, user_message),
            )
            return future.result()
