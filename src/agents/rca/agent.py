"""
src/agents/rca/agent.py  --  RCA Agent (MAF executor)
---------------------------------------------------------
Same logic as the LangGraph version. Queries historical context from
memory/long_term.py, then calls tools/rca.generate_rca() which invokes
the LLM for root cause analysis.

Changes from LangGraph version:
  - Function is now async (ask_llm_json is a coroutine in MAF base_agent)
  - No RunnableConfig; config comes from get_config()
  - State is BlockingState (attribute access)
"""

import logging
from datetime import datetime, timezone

from agent_framework import executor, WorkflowContext
from agents.base_agent import BaseAgent
from orchestrator.config import get_config
from orchestrator.state import BlockingState
from memory.long_term import HistoryInput, HistoryOutput, query_history
from tools.rca import RCAInput, generate_rca

log = logging.getLogger("agent.rca")


@executor(id="rca")
async def rca_node(state: BlockingState, context: WorkflowContext) -> None:
    cfg = get_config()
    agent = BaseAgent(cfg)

    server = state.server_name
    head = state.head_blocker or {}
    log.info("[%s] RCA Agent: generating root cause analysis...", server)

    # ── Historical recurrence context ────────────────────────────────────────
    history_out = HistoryOutput()
    try:
        history_out = query_history(
            HistoryInput(
                log_conn_str=agent.log_conn_str,
                server_name=server,
                login_name=head.get("login_name", ""),
                blocker_database=head.get("blocker_database", ""),
            ),
            query_sql=agent.query_sql,
        )
    except Exception as e:
        log.warning("[%s] History query failed (non-fatal): %s", server, e)

    # ── Build RCA input from full state ──────────────────────────────────────
    rca_input = RCAInput(
        server_name=server,
        correlation_id=state.correlation_id,
        cycle_start_utc=state.cycle_start_utc or datetime.now(timezone.utc).isoformat(),
        head_blocker=state.head_blocker or {},
        decision=state.decision,
        risk_level=state.risk_level,
        rule_triggered=state.rule_triggered,
        kill_executed=state.kill_executed,
        kill_status=state.kill_status,
        dry_run=state.dry_run,
        decision_reason=state.decision_reason,
        detection_analysis=state.llm_analysis,
        severity_hint=state.severity_hint,
        diagnosis=state.diagnosis,
        scenario_id=state.scenario_id,
        scenario_name=state.scenario_name,
        scenario_guidance=state.scenario_guidance,
        log_used_mb=state.log_used_mb,
        log_used_pct=state.log_used_pct,
        kill_safety_rating=state.kill_safety_rating,
        estimated_rollback_sec=state.estimated_rollback_sec,
        txn_age_seconds=state.txn_age_seconds,
        log_safety_database=state.log_safety_database,
        percent_complete=state.percent_complete,
        plan_cache_hit=state.plan_cache_hit,
        plan_cache_source=state.plan_cache_source,
        plan_cache_parent_object=state.plan_cache_parent_object,
        plan_cache_statement_text=state.plan_cache_statement_text,
        query_hash=state.query_hash,
        query_plan_hash=state.query_plan_hash,
        plan_age_minutes=state.plan_age_minutes,
        qs_enabled=state.qs_enabled,
        qs_plans_found=state.qs_plans_found,
        qs_better_plan_exists=state.qs_better_plan_exists,
        qs_plan_recommendation=state.qs_plan_recommendation,
        qs_best_plan_id=state.qs_best_plan_id,
        qs_plan_table=state.qs_plan_table,
        lock_type=state.lock_type,
        lock_diagnosis=state.lock_diagnosis,
        isolation_level=state.isolation_level,
        locked_object=state.locked_object,
        open_txn_count=state.open_txn_count,
        blocker_plan_xml=state.blocker_plan_xml,
        historical_summary=history_out.historical_summary,
        recurrence_login_count=history_out.login_occurrences,
        recurrence_login_was_escalated=history_out.login_was_escalated_before,
        recurrence_database_count=history_out.database_occurrences,
        recurrence_server_total_24h=history_out.server_total_24h,
    )

    # generate_rca calls ask_llm_json synchronously; use the sync bridge
    result = generate_rca(rca_input, ask_llm_json=agent.ask_llm_json_sync)

    state.rca_report = result.to_text()
    state.rca_data   = result.model_dump()
    await context.send_message(state)
