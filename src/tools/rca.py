"""
tools/rca.py -- Root cause analysis tool (enhanced)
---------------------------------------------------
Generates a structured RCA + recommendation report for a SQL blocking
incident via LLM, with a deterministic fallback if the LLM is
unavailable or returns malformed JSON.

Receives ALL diagnostic data collected in the pipeline (plan cache,
Query Store, lock analysis, kill safety, plan XML) plus historical
recurrence context so the LLM can perform a thorough, data-driven
analysis rather than relying solely on a text summary.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field

from models.schemas import PLAN_XML_LLM_LIMIT
from tools.detection import HeadBlocker

log = logging.getLogger("tools.rca")

_PROMPT_FILE = Path(__file__).resolve().parents[1] / "agents" / "rca" / "prompt.md"
_SYSTEM_PROMPT = _PROMPT_FILE.read_text(encoding="utf-8")


class RCAInput(BaseModel):
    server_name: str
    head_blocker: HeadBlocker
    decision: str = "SKIP"
    risk_level: str = "LOW"
    rule_triggered: int = 0
    kill_executed: bool = False
    kill_status: str = "NOT_ATTEMPTED"
    dry_run: bool = True
    decision_reason: str = ""
    correlation_id: str = ""
    cycle_start_utc: Optional[str] = None

    # Analyzer context
    detection_analysis: str = ""
    severity_hint: str = "LOW"
    diagnosis: str = ""

    # Microsoft KB scenario classification
    scenario_id: int = 0
    scenario_name: str = ""
    scenario_guidance: str = ""

    # Transaction log & rollback safety
    log_used_mb: float = 0.0
    log_used_pct: float = 0.0
    kill_safety_rating: str = ""
    estimated_rollback_sec: float = 0.0
    txn_age_seconds: int = 0
    log_safety_database: str = ""
    percent_complete: Optional[float] = None  # R13: mid-rollback progress 0-100 from dm_exec_requests

    # Plan cache
    plan_cache_hit: bool = False
    plan_cache_source: str = ""
    plan_cache_parent_object: str = ""
    plan_cache_statement_text: str = ""
    query_hash: str = ""
    query_plan_hash: str = ""
    plan_age_minutes: int = 0

    # Query Store
    qs_enabled: bool = False
    qs_plans_found: int = 0
    qs_better_plan_exists: bool = False
    qs_plan_recommendation: str = ""
    qs_best_plan_id: Optional[int] = None
    qs_plan_table: str = ""

    # Lock analysis
    lock_type: str = ""
    lock_diagnosis: str = ""
    isolation_level: str = ""
    locked_object: str = ""
    open_txn_count: int = 0

    # Plan XML (truncated for LLM context)
    blocker_plan_xml: Optional[str] = None

    # Historical recurrence context
    historical_summary: str = ""
    recurrence_login_count: int = 0
    recurrence_login_was_escalated: bool = False
    recurrence_database_count: int = 0
    recurrence_server_total_24h: int = 0


class RecommendationItem(BaseModel):
    priority: str = ""
    action: str = ""
    sql: Optional[str] = None
    rationale: str = ""


class Recommendations(BaseModel):
    immediate: list[RecommendationItem] = Field(default_factory=list)
    short_term: list[RecommendationItem] = Field(default_factory=list)
    long_term: list[RecommendationItem] = Field(default_factory=list)
    monitoring: list[RecommendationItem] = Field(default_factory=list)


class RootCause(BaseModel):
    headline: str = ""
    detail: str = ""


class BusinessImpact(BaseModel):
    affected_sessions: int = 0
    duration_seconds: float = 0.0
    impact_description: str = ""


class RCAOutput(BaseModel):
    executive_summary: str = ""
    root_cause: RootCause = Field(default_factory=RootCause)
    business_impact: BusinessImpact = Field(default_factory=BusinessImpact)
    recommendations: Recommendations = Field(default_factory=Recommendations)
    severity: str = "LOW"
    severity_justification: str = ""

    def to_text(self) -> str:
        recs = self.recommendations
        lines = [
            "# Root Cause Analysis & Recommendation Report",
            "",
            "## Executive Summary",
            "",
            self.executive_summary,
            "",
            "## Root Cause",
            "",
            self.root_cause.headline,
            "",
            self.root_cause.detail,
            "",
            "## Business Impact",
            "",
            self.business_impact.impact_description,
            "",
        ]
        for section, label in [
            ("immediate",  "### Immediate Actions (P1 \u2014 Do Today)"),
            ("short_term", "### Short-Term Fixes (P2 \u2014 This Week)"),
            ("long_term",  "### Long-Term Measures (P3 \u2014 This Quarter)"),
            ("monitoring", "### Monitoring & Alerting (P4)"),
        ]:
            items = getattr(recs, section)
            if items:
                lines.append(label)
                lines.append("")
                for i, item in enumerate(items, 1):
                    lines.append(f"{i}. {item.action}")
                    if item.rationale:
                        lines.append(f"   - {item.rationale}")
                    if item.sql:
                        lines.append(f"   - SQL: `{item.sql.strip()}`")
                    lines.append("")

        lines += [
            "",
            f"**Severity:** {self.severity}",
            "",
            self.severity_justification,
        ]
        return "\n".join(lines)


def generate_rca(
    input: RCAInput,
    ask_llm_json: Callable[[str, str], dict],
) -> RCAOutput:
    user_msg = _build_user_message(input)

    try:
        raw = ask_llm_json(_SYSTEM_PROMPT, user_msg)
        result = RCAOutput(**raw)
        result = _validate_recommendations(result)
        return result
    except Exception as e:
        log.error("[%s] RCA LLM failed (%s) \u2014 using fallback", input.server_name, e)
        return _fallback(input, e)


def _build_user_message(input: RCAInput) -> str:
    head = input.head_blocker
    wait_ms = head.wait_duration_ms
    wait_sec = round(wait_ms / 1000, 1)
    cycle_start = input.cycle_start_utc or datetime.now(timezone.utc).isoformat()

    plan_xml_section = input.blocker_plan_xml
    if plan_xml_section and len(plan_xml_section) > _PLAN_XML_LLM_LIMIT:
        plan_xml_section = plan_xml_section[:_PLAN_XML_LLM_LIMIT] + "\n... [truncated]"

    plan_cache_section = (
        f"Hit: {input.plan_cache_hit}\n"
        f"Source: {input.plan_cache_source or 'N/A'}\n"
        f"Parent object: {input.plan_cache_parent_object or 'N/A'}\n"
        f"Statement text: {input.plan_cache_statement_text or 'N/A'}\n"
        f"Query hash: {input.query_hash or 'N/A'}\n"
        f"Plan age (min): {input.plan_age_minutes}\n"
    )

    qs_section = input.qs_plan_table or (
        "Query Store not enabled or no plans found."
    )

    return f"""\
## Incident Facts

Server:              {input.server_name}
Incident time (UTC): {cycle_start}
Correlation ID:      {input.correlation_id or 'N/A'}

## Head Blocker

SPID:                {head.session_id}
Login:               {head.login_name}
Host:                {head.host_name or 'N/A'}
Program:             {head.program_name or 'N/A'}
Database:            {head.blocker_database or 'N/A'}
Wait type:           {head.wait_type}
Wait duration:       {wait_ms} ms  ({wait_sec} s)
Victims blocked:     {head.victim_count}
Blocking chain:      {head.blocking_chain}
Victim logins:       {head.victim_logins or 'N/A'}
SQL text (blocking): {head.sql_text[:1200]}

## Analyzer Diagnosis

Severity hint:       {input.severity_hint}
Analysis summary:    {input.detection_analysis or 'N/A'}
Diagnosis:           {input.diagnosis or 'N/A'}

## KB Scenario Classification

Scenario:            #{input.scenario_id} {input.scenario_name or 'Unclassified'}
Guidance:            {input.scenario_guidance or 'N/A'}

## Transaction Log & Rollback Safety

Database:            {input.log_safety_database or 'N/A'}
Log used (MB):       {input.log_used_mb:.1f}
Log used (%):        {input.log_used_pct:.1f}%
Kill safety rating:  {input.kill_safety_rating or 'N/A'}
Estimated rollback:  {input.estimated_rollback_sec:.0f} s
Transaction age:     {input.txn_age_seconds} s
Rollback progress:   {f"{input.percent_complete:.1f}%" if input.percent_complete is not None else 'N/A (not rolling back)'}

## Plan Cache

{plan_cache_section}

## Query Store

Enabled:             {input.qs_enabled}
Plans found:         {input.qs_plans_found}
Better plan exists:  {input.qs_better_plan_exists}
Recommendation:      {input.qs_plan_recommendation or 'N/A'}
Best plan ID:        {input.qs_best_plan_id or 'N/A'}
Plan table:
{qs_section}

## Lock Analysis

Lock type:           {input.lock_type or 'N/A'}
Lock diagnosis:      {input.lock_diagnosis or 'N/A'}
Isolation level:     {input.isolation_level or 'N/A'}
Locked object:       {input.locked_object or 'N/A'}
Open transactions:   {input.open_txn_count}

## Plan XML (truncated to {PLAN_XML_LLM_LIMIT} chars)

{plan_xml_section or 'Not available.'}

## Agent Decision

Decision:            {input.decision}
Risk level:          {input.risk_level}
Rule triggered:      {input.rule_triggered}
Kill executed:       {input.kill_executed}
Kill status:         {input.kill_status}
Dry run:             {input.dry_run}
Decision reason:     {input.decision_reason or 'N/A'}

## Historical Context

{input.historical_summary or 'No historical data available.'}
"""


def _validate_recommendations(output: RCAOutput) -> RCAOutput:
    """Post-process the LLM's recommendations to catch common issues."""
    for section_name in ("immediate", "short_term", "long_term", "monitoring"):
        items = getattr(output.recommendations, section_name)
        for item in items:
            if item.sql:
                item.sql = _validate_tsql(item.sql)
    return output


def _validate_tsql(sql: str) -> str:
    """Basic sanity checks on LLM-generated T-SQL.

    - Strips known-hallucinated prefixes
    - Flags dangerous DDL
    - Does NOT guarantee the SQL is semantically correct — only catches
      obvious issues that would cause errors or data loss.
    """
    sql_stripped = sql.strip()

    if not sql_stripped:
        return ""

    # Strip markdown code fences if the LLM wrapped them despite instructions
    if sql_stripped.startswith("```"):
        sql_stripped = re.sub(r"^```(?:sql)?\s*", "", sql_stripped)
        sql_stripped = re.sub(r"\s*```$", "", sql_stripped)

    upper = sql_stripped.upper()

    # Flag destructive DDL — these should never appear in recommendations
    dangerous_patterns = [
        (r"\bDROP\s+(DATABASE|TABLE|VIEW|PROCEDURE|FUNCTION|INDEX)\b",
         "contains DROP statement \u2014 removed for safety"),
        (r"\bTRUNCATE\s+TABLE\b",
         "contains TRUNCATE \u2014 removed for safety"),
        (r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)",
         "DELETE without WHERE clause \u2014 removed for safety"),
        (r"\bDELETE\b(?!\s+(FROM|TOP)\b)(?!.*\bWHERE\b)",
         "bare DELETE without WHERE clause \u2014 removed for safety"),
        (r"\bUPDATE\b(?!.*\bWHERE\b)",
         "UPDATE without WHERE clause \u2014 removed for safety"),
        (r"\bBACKUP\s+DATABASE\b",
         "contains BACKUP DATABASE \u2014 removed for safety"),
    ]
    for pattern, warning in dangerous_patterns:
        if re.search(pattern, sql_stripped, re.IGNORECASE | re.DOTALL):
            log.warning("T-SQL validation: %s in:\n%s", warning, sql_stripped[:200])
            return f"-- [SAFETY: {warning}]\n-- The original suggestion was:\n-- {sql_stripped}"

    return sql_stripped


def _fallback(input: RCAInput, error: Exception) -> RCAOutput:
    head = input.head_blocker
    return RCAOutput(
        executive_summary=(
            f"RCA LLM unavailable ({error}). Manual review required. "
            f"SPID {head.session_id} ({head.login_name}) blocked {head.victim_count} "
            f"session(s) for {head.wait_duration_ms} ms on {input.server_name}."
        ),
        root_cause=RootCause(headline="Unknown \u2014 LLM unavailable", detail=""),
        business_impact=BusinessImpact(
            affected_sessions=head.victim_count,
            duration_seconds=round(head.wait_duration_ms / 1000, 1),
            impact_description=(
                f"SPID {head.session_id} ({head.login_name}) blocked "
                f"{head.victim_count} session(s) for {round(head.wait_duration_ms/1000, 1)}s "
                f"on {input.server_name}."
            ),
        ),
        recommendations=Recommendations(
            immediate=[RecommendationItem(
                priority="P1",
                action="Review BlockingEventLog in AgentLogDB for full incident details",
                sql=None,
                rationale="LLM RCA unavailable \u2014 manual DBA investigation required",
            )],
        ),
        severity=input.risk_level,
        severity_justification="Based on agent risk assessment only \u2014 LLM unavailable.",
    )
