"""
src/models/schemas.py -- Pydantic contracts for LLM JSON responses
--------------------------------------------------------------------
ask_llm_json() returns a plain dict parsed from the LLM's JSON output.
These models validate that dict before it's used -- a missing key,
wrong type, or out-of-range value (e.g. severity_hint="Severe") raises
a pydantic ValidationError, which callers treat the same as any other
LLM failure (deterministic fallback).

(tools/rca.py already validates its LLM response via RCAOutput(**raw)
-- these models cover the Analyzer and Determination agents.)
"""

from typing import Literal

from pydantic import BaseModel, Field

# Maximum characters of plan XML passed to any LLM prompt.
# Shared by agents/analyzer/agent.py and tools/rca.py.
PLAN_XML_LLM_LIMIT = 8000


class AnalyzerLLMResult(BaseModel):
    """Expected JSON shape from agents/analyzer_agent.py SYSTEM_PROMPT."""
    analysis_summary: str
    severity_hint: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    key_findings: list[str] = Field(default_factory=list)
    diagnosis: str = ""


class DeterminationLLMResult(BaseModel):
    """Expected JSON shape from agents/determination_agent.py SYSTEM_PROMPT."""
    decision: Literal["KILL", "ALERT_ONLY", "SKIP"]
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    reason: str
    safety_check_passed: bool
    rule_triggered: int = 0
