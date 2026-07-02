"""
src/agents/analyzer/agent.py  --  Analyzer Agent
----------------------------------------------------------------------
Same logic as the LangGraph version. Receives pre-collected diagnostics
from the Detection executor and asks the LLM to synthesize them into a
narrative analysis plus a severity hint.

Changes from LangGraph version:
  - run() is now async (ChatAgent.run() is a coroutine)
  - No RunnableConfig; config comes from get_config()
  - State is BlockingState (attribute access instead of dict.get())
"""

import json
import logging
from pathlib import Path
from typing import Optional

from agent_framework import executor, WorkflowContext
from agents.base_agent import BaseAgent
from orchestrator.config import get_config
from orchestrator.state import BlockingState
from models.schemas import AnalyzerLLMResult, PLAN_XML_LLM_LIMIT

log = logging.getLogger("agent.analyzer")

_PROMPT_FILE = Path(__file__).resolve().parent / "prompt.md"
SYSTEM_PROMPT = _PROMPT_FILE.read_text(encoding="utf-8")


def _fmt_plan_for_llm(xml: Optional[str]) -> str:
    if not xml:
        return "Not available."
    if len(xml) <= PLAN_XML_LLM_LIMIT:
        return xml
    return xml[:PLAN_XML_LLM_LIMIT] + "\n... [truncated, full plan stored in RCASnapshotLog]"


class AnalyzerAgent(BaseAgent):

    async def run(self, state: BlockingState) -> dict:
        server = state.server_name
        head = state.head_blocker or {}
        spid = head.get("session_id")
        log.info("[%s] Analyzer Agent: SPID=%s", server, spid)

        errors: list[str] = list(state.errors)
        blocking_rows = state.blocking_rows
        row = next((r for r in blocking_rows if r.get("session_id") == spid), {})

        plan_cache_section = (
            json.dumps({
                "hit": state.plan_cache_hit,
                "source": state.plan_cache_source,
                "parent_object": state.plan_cache_parent_object,
                "query_hash": state.query_hash,
                "query_plan_hash": state.query_plan_hash,
                "plan_age_minutes": state.plan_age_minutes,
            }, default=str, indent=2)
            if state.plan_cache_hit
            else "No cached plan found. Head blocker may be running a WAITFOR/WHILE-loop batch with no cached plan. Consider enabling an Extended Events session (blocked_process_report + sql_batch_completed) on the target server to capture the blocking query on future occurrences."
        )

        qs_table = state.qs_plan_table
        if qs_table:
            qs_section = qs_table
        elif not state.qs_enabled:
            qs_section = "Query Store is NOT enabled on this database."
        elif state.qs_plans_found == 0:
            qs_section = "Query Store enabled but no history found for this query hash."
        else:
            qs_section = "Not available."

        current_plan_xml_section = _fmt_plan_for_llm(state.blocker_plan_xml)

        victim_spids: list[int] = head.get("victim_spids") or []
        victim_rows = [r for r in blocking_rows if r.get("session_id") in victim_spids]
        victim_hosts = ", ".join(
            set(str(r.get("host_name", "")) for r in victim_rows if r.get("host_name"))
        )
        victim_programs = ", ".join(
            set(str(r.get("program_name", "")) for r in victim_rows if r.get("program_name"))
        )
        same_app_group = "YES" if (
            row.get("host_name") and any(
                str(r.get("host_name", "")) == str(row.get("host_name", ""))
                for r in victim_rows
            )
        ) else "NO"

        user_msg = f"""\
## Head Blocker

{json.dumps(head, default=str, indent=2)}

## Session Metadata -- Application Behavior

Head blocker host_name:    {row.get("host_name", "N/A")}
Head blocker program_name: {row.get("program_name", "N/A")}
Head blocker status:       {row.get("status", "N/A")}
Head blocker command:      {row.get("command", "N/A")}
Head blocker open_tran:    {state.open_txn_count}
Head blocker txn_age_sec:  {state.txn_age_seconds}
Blocking chain depth:      {head.get("victim_count", 0)} victims
Head blocker last cached SQL: {state.plan_cache_statement_text or "(not available)"}

## Victim Host/Profile Overlap

Victim host_names:      {victim_hosts or "N/A"}
Victim program_names:   {victim_programs or "N/A"}
Blocker appears in same app group as victims? {same_app_group}

## Log & Rollback Safety

{json.dumps({
    "kill_safety_rating": state.kill_safety_rating,
    "log_used_pct": state.log_used_pct,
    "log_used_mb": state.log_used_mb,
    "estimated_rollback_sec": state.estimated_rollback_sec,
    "database_name": state.log_safety_database,
}, default=str, indent=2)}

## Plan Cache (metadata)

{plan_cache_section}

## Query Store Plan History

{qs_section}

## Current Plan XML (scan for MissingIndexes and dominant operators)

{current_plan_xml_section}

## Best Historical Plan XML

Not stored individually in state -- compare current plan XML against
the QS plan table above which contains per-plan metrics.

## Lock & Isolation Analysis

{json.dumps({
    "lock_type": state.lock_type,
    "lock_diagnosis": state.lock_diagnosis,
    "isolation_level": state.isolation_level,
    "locked_object": state.locked_object,
    "open_txn_count": state.open_txn_count,
}, default=str, indent=2)}

## Microsoft KB Scenario Classification

{json.dumps({
    "scenario_id": state.scenario_id,
    "scenario_name": state.scenario_name,
    "scenario_guidance": state.scenario_guidance,
}, default=str, indent=2)}

Synthesize these findings now. If plan XML is present, extract any
MissingIndex recommendations and include them in key_findings.
If multiple Query Store plans exist, identify the best plan and
explain the performance delta in key_findings.
"""
        try:
            raw = await self.ask_llm_json(SYSTEM_PROMPT, user_msg)
            result = AnalyzerLLMResult(**raw)
            analysis_summary = result.analysis_summary
            severity_hint = result.severity_hint
            diagnosis = result.diagnosis
        except Exception as e:
            log.error("[%s] Analyzer LLM failed: %s -- falling back to scenario guidance", server, e)
            analysis_summary = (
                f"[LLM unavailable: {e}] SPID {spid}: scenario #{state.scenario_id} "
                f"({state.scenario_name}). {state.scenario_guidance}"
            )
            severity_hint = "MEDIUM"
            diagnosis = f"LLM unavailable; scenario {state.scenario_id} guidance applied"

        log.info("[%s] Analyzer summary (severity=%s): %s", server, severity_hint, analysis_summary[:160])

        return {
            "llm_analysis":  analysis_summary,
            "severity_hint": severity_hint,
            "diagnosis":     diagnosis,
            "errors":        errors,
        }


@executor(id="analyzer")
async def analyzer_node(state: BlockingState, context: WorkflowContext) -> None:
    cfg = get_config()
    updates = await AnalyzerAgent(cfg).run(state)
    state.llm_analysis  = updates["llm_analysis"]
    state.severity_hint = updates["severity_hint"]
    state.diagnosis     = updates["diagnosis"]
    state.errors        = updates["errors"]
    await context.send_message(state)
