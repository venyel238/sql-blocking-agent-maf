"""
tools/audit_log.py -- Audit log writer
-------------------------------------------
Writes the two audit tables in AgentLogDB:
  BlockingEventLog -- every cycle that had blocking
  KillAuditLog     -- immutable record for every kill (real or dry-run)

Pure I/O contract over an injected execute_sql callable. No LangGraph,
no file/console/email side effects (see tools/report_renderer.py and
tools/notifications.py for those).
"""

import logging
from typing import Callable, Optional

from pydantic import BaseModel, Field

from tools.detection import HeadBlocker

log = logging.getLogger("tools.audit_log")

INSERT_EVENT_SQL = """\
INSERT INTO dbo.BlockingEventLog
    (ServerName, CorrelationID, HeadBlockerSPID, HeadBlockerLogin,
     BlockerDatabase, BlockerSQLText, BlockerParentObject,
     VictimSPIDs, VictimLogins, VictimDatabases, VictimSQLText, VictimParentObjects,
     WaitDurationMs, VictimCount,
     WaitType, LockResource, LockObjectName, LockIndexName,
     DecisionTaken, DecisionReason, RiskLevel, DryRun,
     HasBlockerPlan)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_SNAPSHOT_SQL = """\
INSERT INTO dbo.RCASnapshotLog
    (KillCorrelationID, ServerName, KilledSPID, BlockerPlanXML)
VALUES (?, ?, ?, ?)
"""

INSERT_KILL_SQL = """\
INSERT INTO dbo.KillAuditLog
    (ServerName, CorrelationID, KilledSPID, KilledLogin,
     WaitDurationMs, VictimCount,
     KillStatus, RiskLevel, LLMReasoning, RCAReport, DryRun)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class AuditLogInput(BaseModel):
    log_conn_str: str
    server_name: str
    correlation_id: str = ""
    head_blocker: HeadBlocker
    blocker_plan_xml: Optional[str] = None   # written to RCASnapshotLog when present


class AuditLogOutput(BaseModel):
    event_logged: bool = False
    kill_logged: bool = False
    errors: list[str] = Field(default_factory=list)


def write_blocking_event(
    log_conn_str: str,
    server_name: str,
    correlation_id: str,
    head: "HeadBlocker",
    decision: str,
    decision_reason: str,
    risk_level: str,
    dry_run: bool,
    has_blocker_plan: bool,
    execute_sql: Callable[[str, str, list], None],
) -> list[str]:
    """Write BlockingEventLog immediately — first thing in Notification,
    so the incident is never lost even if a downstream step crashes."""
    victim_spids_str   = ", ".join(str(s) for s in head.victim_spids)
    victim_logins_str  = ", ".join(head.victim_logins)
    victim_dbs_str     = ", ".join(dict.fromkeys(head.victim_databases))
    victim_sql_str     = "\n---\n".join(head.victim_sql_texts)
    victim_parents_str = ", ".join(p for p in head.victim_parent_objects if p)

    try:
        execute_sql(
            log_conn_str, INSERT_EVENT_SQL,
            [
                server_name, correlation_id,
                head.session_id, head.login_name,
                head.blocker_database[:128],
                head.sql_text[:4000],
                head.blocker_parent_object[:512] or None,
                victim_spids_str[:1000],
                victim_logins_str[:2000],
                victim_dbs_str[:500],
                victim_sql_str[:8000],
                victim_parents_str[:1000] or None,
                head.wait_duration_ms, head.victim_count,
                head.wait_type[:60],
                head.lock_resource[:500],
                head.lock_object_name[:256] or None,
                head.lock_index_name[:256] or None,
                decision,
                decision_reason[:2000],
                risk_level,
                1 if dry_run else 0,
                1 if has_blocker_plan else 0,
            ],
        )
        log.info("[%s] BlockingEventLog written (decision=%s)", server_name, decision)
    except Exception as e:
        log.error("[%s] Failed to write BlockingEventLog: %s", server_name, e)
        return [f"log_event_failed: {e}"]
    return []


def write_kill_audit(
    log_conn_str: str,
    server_name: str,
    correlation_id: str,
    killed_spid: int,
    login_name: str,
    wait_duration_ms: int,
    victim_count: int,
    kill_status: str,
    risk_level: str,
    llm_reasoning: str,
    dry_run: bool,
    execute_sql: Callable[[str, str, list], None],
) -> list[str]:
    """Write KillAuditLog immediately after a kill attempt (before crash risk)."""
    errors = []
    try:
        execute_sql(
            log_conn_str, INSERT_KILL_SQL,
            [
                server_name, correlation_id,
                killed_spid, login_name,
                wait_duration_ms, victim_count,
                kill_status[:200],
                risk_level,
                llm_reasoning[:8000],
                "",  # rca_report not available yet
                1 if dry_run else 0,
            ],
        )
        log.info("[%s] KillAuditLog written (SPID=%s  status=%s)", server_name, killed_spid, kill_status)
    except Exception as e:
        log.error("[%s] Failed to write KillAuditLog: %s", server_name, e)
        errors.append(f"log_kill_failed: {e}")
    return errors


def write_audit_log(input: AuditLogInput, execute_sql: Callable[[str, str, list], None]) -> AuditLogOutput:
    """Write RCASnapshotLog (plan XML). BlockingEventLog is written by
    write_blocking_event() called first in notification_node, and
    KillAuditLog is written by nodes/action_node.py after kill execution."""
    out = AuditLogOutput()
    head = input.head_blocker

    # Write plan XML to RCASnapshotLog whenever we have one (kill or alert)
    if input.blocker_plan_xml:
        try:
            execute_sql(
                input.log_conn_str, INSERT_SNAPSHOT_SQL,
                [
                    input.correlation_id,
                    input.server_name,
                    input.head_blocker.session_id,
                    input.blocker_plan_xml,
                ],
            )
            log.info("[%s] RCASnapshotLog: blocker plan written (SPID=%s  source=plan_xml)",
                     input.server_name, input.head_blocker.session_id)
        except Exception as e:
            log.error("[%s] Failed to write plan to RCASnapshotLog: %s", input.server_name, e)
            out.errors.append(f"plan_snapshot_failed: {e}")

    return out
