"""
tools/sql_executor.py -- SQL Executor tool
-----------------------------------------------
The only tool that can issue a KILL command. Performs two safety
checks before executing:
  1. Re-verify the SPID is still active (not already gone)
  2. Confirm the SPID is still the head blocker (not now a victim itself)

No LLM is used here -- pure deterministic code. In dry_run=True mode it
reports what it would do but never executes the KILL.

Paired with tools/sql_validator.py, which confirms the outcome.
"""

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import pyodbc
from pydantic import BaseModel

log = logging.getLogger("tools.sql_executor")

# Robust SPID verification before issuing KILL.
# Checks (in order): SPID is alive, still the same session (login match),
# still the blocker (not now a victim), still has blocking potential
# (open transaction or active blocking).
VERIFY_SQL = """\
SELECT
    s.session_id,
    s.login_name,
    s.status,
    s.is_user_process,
    ISNULL(s.open_transaction_count, 0)                        AS open_txn_count,
    ISNULL(r.blocking_session_id, 0)                            AS blocking_session_id,
    r.wait_time                                                 AS wait_time_ms,
    r.wait_type,
    -- Does this session still have victims actively waiting on it?
    (SELECT COUNT(*)
     FROM sys.dm_exec_requests v
     WHERE v.blocking_session_id = s.session_id)                AS active_victims
FROM sys.dm_exec_sessions s
LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
WHERE s.session_id = ?
"""


class SqlExecutorInput(BaseModel):
    monitor_conn_str: str
    server_name: str = ""
    session_id: Optional[int] = None
    login_name: str = ""
    dry_run: bool = True


class SqlExecutorOutput(BaseModel):
    kill_issued: bool = False
    killed_spid: Optional[int] = None
    kill_time_utc: Optional[str] = None
    dry_run: bool = True
    skip_reason: Optional[str] = None
    issue_status: str = "NOT_ATTEMPTED"  # DRY_RUN_SIMULATED | ISSUED | FAILED: <err>


def default_execute_kill(conn_str: str, session_id: int) -> None:
    """KILL must run with autocommit=True -- SQL Server rejects it inside a transaction."""
    with pyodbc.connect(conn_str, autocommit=True, timeout=10) as conn:
        conn.cursor().execute(f"KILL {int(session_id)}")  # int() cast is the injection guard — ODBC does not support ? for KILL


def execute_kill(
    input: SqlExecutorInput,
    query_sql: Callable[[str, str, list], list[dict]],
    execute_kill_fn: Callable[[str, int], None] = default_execute_kill,
) -> SqlExecutorOutput:
    if not input.session_id:
        return SqlExecutorOutput(dry_run=input.dry_run, skip_reason="No head blocker SPID found in state.")

    # ── Safety Check 1: is the SPID still alive on the server? ────────────────
    try:
        rows = query_sql(input.monitor_conn_str, VERIFY_SQL, [input.session_id])
    except Exception as e:
        return SqlExecutorOutput(dry_run=input.dry_run, skip_reason=f"Re-verify query failed: {e}")

    if not rows:
        return SqlExecutorOutput(
            dry_run=input.dry_run,
            skip_reason=f"SPID {input.session_id} is already gone -- no kill needed.",
        )

    live = rows[0]

    # ── Safety Check 2: is this the SAME session (login match)? ──────────────
    # SPIDs are recycled by SQL Server. If the login doesn't match, the SPID
    # now belongs to a different, innocent session.
    live_login = str(live.get("login_name") or "").lower()
    expected_login = str(input.login_name or "").lower()
    if expected_login and live_login != expected_login:
        return SqlExecutorOutput(
            dry_run=input.dry_run,
            skip_reason=(
                f"SPID {input.session_id} now belongs to '{live_login}' "
                f"(expected '{expected_login}') -- session was recycled, skipping kill."
            ),
        )

    # ── Safety Check 3: is it still a user process (not a system SPID)? ───────
    if not live.get("is_user_process"):
        log.warning("[%s] SPID %s is no longer a user process -- skipping.", input.server_name, input.session_id)
        return SqlExecutorOutput(
            dry_run=input.dry_run,
            skip_reason=f"SPID {input.session_id} is not a user process -- skipping kill.",
        )

    # ── Safety Check 4: is it still blocking (not now a victim itself)? ───────
    if int(live.get("blocking_session_id") or 0) > 0:
        return SqlExecutorOutput(
            dry_run=input.dry_run,
            skip_reason=(
                f"SPID {input.session_id} is now being blocked itself (by SPID "
                f"{live['blocking_session_id']}) -- situation changed, skipping kill."
            ),
        )

    # ── Safety Check 5: does it still have blocking potential? ────────────────
    open_txns = int(live.get("open_txn_count") or 0)
    active_victims = int(live.get("active_victims") or 0)
    if open_txns == 0 and active_victims == 0:
        return SqlExecutorOutput(
            dry_run=input.dry_run,
            skip_reason=(
                f"SPID {input.session_id} has no open transactions (open_txn_count=0) "
                f"and no active victims (active_victims=0) -- blocking already resolved, "
                f"skipping kill."
            ),
        )

    # ── Issue (or simulate) the KILL ──────────────────────────────────────────
    kill_time = datetime.now(timezone.utc).isoformat()

    if input.dry_run:
        log.warning(
            "[DRY RUN] Would execute: KILL %s  (login=%s  wait_ms=%s  victims=%s  txns=%s)",
            input.session_id, input.login_name,
            live.get("wait_time_ms"), active_victims, open_txns,
        )
        issue_status = "DRY_RUN_SIMULATED"
    else:
        try:
            execute_kill_fn(input.monitor_conn_str, input.session_id)
            issue_status = "ISSUED"
            log.warning(
                "KILL issued for SPID %s (login=%s  wait_ms=%s  victims=%s  txns=%s)",
                input.session_id, input.login_name,
                live.get("wait_time_ms"), active_victims, open_txns,
            )
        except Exception as e:
            issue_status = f"FAILED: {e}"
            log.error("KILL issue failed for SPID %s: %s", input.session_id, e)

    return SqlExecutorOutput(
        kill_issued=True,
        killed_spid=input.session_id,
        kill_time_utc=kill_time,
        dry_run=input.dry_run,
        issue_status=issue_status,
    )
