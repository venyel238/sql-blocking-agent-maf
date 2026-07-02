"""
src/agents/notifier/agent.py  --  Notification (tool executor)
-----------------------------------------------------------------------
Same logic as the LangGraph version. Always runs last on EVERY cycle path.
Writes audit records, renders HTML/MD reports, sends DBA email when required.

Changes from LangGraph version:
  - Function is now async (framework consistency)
  - No RunnableConfig; config comes from get_config()
  - State is BlockingState (attribute access)
  - Calls context.yield_output(state) to expose final state to main.py
"""

import logging
from pathlib import Path

from agent_framework import executor, WorkflowContext
from agents.base_agent import BaseAgent
from orchestrator.config import get_config
from orchestrator.state import BlockingState
from tools.audit_log import AuditLogInput, write_audit_log, write_blocking_event
from tools.detection import HeadBlocker
from tools.notifications import DbaEmailInput, default_send_email, send_dba_approval_email
from tools.rca import RCAOutput
from tools.report_renderer import ReportInput, render_reports, save_reports

log = logging.getLogger("node.notification")

REPORTS_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "reports"


@executor(id="notification")
async def notification_node(state: BlockingState, context: WorkflowContext) -> None:
    cfg = get_config()
    agent = BaseAgent(cfg)

    server   = state.server_name
    decision = state.decision
    errors   = list(state.errors)
    rca_data = state.rca_data

    log.info("[%s] Notification node: writing audit records...", server)

    if not state.has_blocking:
        log.info("[%s] No blocking this cycle -- nothing to log.", server)
        state.errors = errors
        state.html_report_path = None
        await context.yield_output(state)
        return

    head = HeadBlocker(**(state.head_blocker or {}))

    # ── DURABLE AUDIT: write BlockingEventLog FIRST ──────────────────────────
    event_errors = write_blocking_event(
        log_conn_str=agent.log_conn_str,
        server_name=server,
        correlation_id=state.correlation_id,
        head=head,
        decision=decision,
        decision_reason=str(state.decision_reason),
        risk_level=state.risk_level,
        dry_run=state.dry_run,
        has_blocker_plan=bool(state.blocker_plan_xml),
        execute_sql=agent.execute_sql,
    )
    errors.extend(event_errors)

    # ── Plan snapshot (RCASnapshotLog) ───────────────────────────────────────
    plan_errors = write_audit_log(
        AuditLogInput(
            log_conn_str=agent.log_conn_str,
            server_name=server,
            correlation_id=state.correlation_id,
            head_blocker=head,
            decision=decision,
            decision_reason=str(state.decision_reason),
            risk_level=state.risk_level,
            dry_run=state.dry_run,
            kill_executed=state.kill_executed,
            killed_spid=state.killed_spid,
            kill_status=str(state.kill_status),
            llm_reasoning=str(state.llm_reasoning),
            rca_report=state.rca_report,
            blocker_plan_xml=state.blocker_plan_xml,
        ),
        execute_sql=agent.execute_sql,
    )
    errors.extend(plan_errors.errors)

    html_path = None
    if rca_data is not None:
        blocked_texts = [
            str(r.get("sql_text", ""))[:800]
            for r in state.blocking_rows
            if int(r.get("blocking_session_id") or 0) > 0 and r.get("sql_text")
        ]
        report_input = ReportInput(
            server_name=server,
            head_blocker=head,
            decision=decision,
            risk_level=state.risk_level,
            rca=RCAOutput(**rca_data),
            correlation_id=state.correlation_id,
            cycle_start_utc=state.cycle_start_utc,
            dry_run=state.dry_run,
            log_used_mb=state.log_used_mb,
            log_used_pct=state.log_used_pct,
            rule_triggered=state.rule_triggered,
            kill_status=str(state.kill_status),
            kill_time_utc=state.kill_time_utc,
            kill_executed=state.kill_executed,
            decision_reason=str(state.decision_reason),
            blocked_texts=blocked_texts,
        )
        report = render_reports(report_input)

        try:
            html_path, _md_path = save_reports(report, server, head.session_id, decision, REPORTS_ROOT)
            log.info("[%s] HTML report saved: %s", server, html_path)
        except Exception as e:
            log.error("[%s] Failed to save report files: %s", server, e)
            errors.append(f"report_save_failed: {e}")

        log.warning("\n%s", "\n".join(report.summary_lines))
        if html_path:
            log.warning("[%s] HTML report: %s", server, html_path)

        if state.dba_approval_required:
            email_input = DbaEmailInput(
                server_name=server,
                head_blocker=head,
                rca_report=state.rca_report,
                log_used_mb=state.log_used_mb,
                log_size_kill_threshold_gb=cfg.get("log_size_kill_threshold_gb", 20),
                smtp_host=cfg.get("smtp_host", ""),
                smtp_port=cfg.get("smtp_port", 587),
                smtp_from=cfg.get("smtp_from", ""),
                smtp_user=cfg.get("smtp_user"),
                smtp_password=cfg.get("smtp_password"),
                dba_email=cfg.get("dba_email", "evhdba@evolent.com"),
            )
            email_out = send_dba_approval_email(email_input, send_email=default_send_email(email_input))
            if not email_out.sent and email_out.skip_reason != "smtp_not_configured":
                errors.append(f"dba_email_failed: {email_out.skip_reason}")

    state.errors = errors
    state.html_report_path = str(html_path) if html_path else None

    # Yield the final state so workflow.run() returns it to main.py
    await context.yield_output(state)
