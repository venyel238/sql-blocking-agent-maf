"""
src/agents/action/agent.py  --  Action (tool executor: SQL Executor + SQL Validator)
--------------------------------------------------------------------------
Same logic as the LangGraph version. Only runs when the Determination Agent
decided "KILL". Orchestrates execute_kill() then validate_kill(), then writes
an immutable KillAuditLog entry.

Changes from LangGraph version:
  - Function is now async (framework consistency)
  - No RunnableConfig; config comes from get_config()
  - State is BlockingState (attribute access)
"""

import logging

from agent_framework import executor, WorkflowContext
from agents.base_agent import BaseAgent
from orchestrator.config import get_config
from orchestrator.state import BlockingState
from tools.audit_log import write_kill_audit
from tools.sql_executor import SqlExecutorInput, execute_kill
from tools.sql_validator import SqlValidatorInput, validate_kill

log = logging.getLogger("agent.action")


@executor(id="action")
async def action_node(state: BlockingState, context: WorkflowContext) -> None:
    cfg = get_config()
    agent = BaseAgent(cfg)

    server = state.server_name
    head = state.head_blocker or {}
    spid = head.get("session_id")
    login = head.get("login_name", "")

    log.info("[%s] Action (SQL Executor): preparing to kill SPID=%s login=%s", server, spid, login)

    exec_result = execute_kill(
        SqlExecutorInput(
            monitor_conn_str=agent.monitor_conn_str,
            server_name=server,
            session_id=spid,
            login_name=login,
            dry_run=cfg.get("dry_run", True),
        ),
        query_sql=agent.query_sql,
    )

    state.kill_executed = exec_result.kill_issued
    state.killed_spid   = exec_result.killed_spid
    state.kill_time_utc = exec_result.kill_time_utc

    if exec_result.skip_reason:
        log.info("[%s] Action skipped: %s", server, exec_result.skip_reason)
        state.kill_status = "SKIPPED"
        state.decision_reason = state.decision_reason + f" | Action skip: {exec_result.skip_reason}"
        await context.send_message(state)
        return

    state.dry_run = exec_result.dry_run

    if exec_result.dry_run:
        state.kill_status = "DRY_RUN_SIMULATED"
    elif exec_result.issue_status.startswith("FAILED"):
        state.kill_status = exec_result.issue_status
    else:
        log.info("[%s] Action (SQL Validator): confirming SPID=%s outcome", server, spid)
        val_result = validate_kill(
            SqlValidatorInput(
                monitor_conn_str=agent.monitor_conn_str,
                session_id=spid,
                expected_login=login,
            ),
            query_sql=agent.query_sql,
        )
        state.kill_status    = val_result.kill_status
        state.kill_validation = val_result.validation_status
        log.info("[%s] Validation: %s -> kill_status=%s", server, val_result.validation_status, val_result.kill_status)

    # ── IMMUTABLE AUDIT: write KillAuditLog NOW ───────────────────────────────
    audit_errors = write_kill_audit(
        log_conn_str=agent.log_conn_str,
        server_name=server,
        correlation_id=state.correlation_id,
        killed_spid=exec_result.killed_spid,
        login_name=head.get("login_name", ""),
        wait_duration_ms=head.get("wait_duration_ms", 0),
        victim_count=head.get("victim_count", 0),
        kill_status=state.kill_status,
        risk_level=state.risk_level,
        llm_reasoning=state.llm_reasoning,
        dry_run=exec_result.dry_run,
        execute_sql=agent.execute_sql,
    )

    state.errors = list(state.errors) + audit_errors
    await context.send_message(state)
