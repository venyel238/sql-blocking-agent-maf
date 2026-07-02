"""
agent_framework.foundry — FoundryChatClient shim
==================================================
Wraps the openai SDK to provide the FoundryChatClient interface expected by
agents/base_agent.py, making the MAF-style code runnable without installing
the real agent-framework-foundry package.

The endpoint resolution mirrors base_agent._make_client():
  - FOUNDRY_PROJECT_ENDPOINT  → strip path to get base, then re-add /openai/v1/
  - LLM_BASE_URL              → used as-is (already contains /openai/v1)

Credential resolution:
  - AzureKeyCredential  → .key attribute  → passed as openai api_key
  - DefaultAzureCredential → not used here; falls back to LLM_API_KEY env var
"""

import os

from openai import OpenAI


class FoundryChatClient:
    """
    Azure AI Foundry / Azure OpenAI client backed by the openai SDK.

    Constructor args (matching the real MAF SDK):
      project_endpoint  -- the Foundry project URL (without /openai/v1 suffix)
                           OR the full openai-compatible base URL
      model             -- model/deployment name
      credential        -- AzureKeyCredential or DefaultAzureCredential
    """

    def __init__(self, project_endpoint: str, model: str, credential=None):
        self._model = model

        # Resolve API key from credential object
        api_key = ""
        if credential is not None and hasattr(credential, "key"):
            api_key = credential.key
        if not api_key:
            api_key = os.getenv("LLM_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")

        # Build the openai-compatible base URL.
        # project_endpoint arrives either as:
        #   (a) LLM_BASE_URL stripped of /openai/v1  (our _make_client() code)
        #   (b) already a full URL ending in /openai/v1 or similar
        endpoint = project_endpoint.rstrip("/")
        if "/openai/v1" in endpoint:
            # Already contains the path — use as-is
            base_url = endpoint.rstrip("/") + "/"
        else:
            # Append the standard Azure OpenAI path
            base_url = endpoint + "/openai/v1/"

        self._openai = OpenAI(
            api_key=api_key or "placeholder-key",
            base_url=base_url,
        )

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Synchronous single-turn completion (called via run_in_executor)."""
        resp = self._openai.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""
