"""
tools/sql_validator.py -- SQL Validator tool
-----------------------------------------------
Confirms the outcome of a KILL issued by tools/sql_executor.py by
querying sys.dm_exec_sessions / sys.dm_exec_requests for the SPID:
  - SPID no longer exists                -> CONFIRMED_GONE
  - SPID exists with status = 'rollback' -> ROLLING_BACK (kill succeeded,
    rollback in progress -- this is expected and not a failure)
  - SPID still exists and is not in
    rollback                             -> STILL_PRESENT (kill failed)

No LLM is used here -- pure deterministic code.
"""

import logging
import time
from typing import Callable, Optional

from pydantic import BaseModel

log = logging.getLogger("tools.sql_validator")

CHECK_SESSION_SQL = """\
SELECT
    s.session_id,
    s.login_name,
    ISNULL(r.status, s.status) AS status
FROM sys.dm_exec_sessions s
LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
WHERE s.session_id = ?
"""


class SqlValidatorInput(BaseModel):
    monitor_conn_str: str
    session_id: Optional[int] = None
    expected_login: str = ""  # if set, treat SPID with different login as CONFIRMED_GONE


class SqlValidatorOutput(BaseModel):
    validation_status: str = "NOT_CHECKED"  # CONFIRMED_GONE | ROLLING_BACK | STILL_PRESENT | NOT_CHECKED
    kill_status: str = "NOT_ATTEMPTED"


def validate_kill(
    input: SqlValidatorInput,
    query_sql: Callable[[str, str, list], list[dict]],
) -> SqlValidatorOutput:
    if not input.session_id:
        return SqlValidatorOutput()

    try:
        rows = query_sql(input.monitor_conn_str, CHECK_SESSION_SQL, [input.session_id])
    except Exception as e:
        log.error("Validation query failed for SPID %s: %s", input.session_id, e)
        return SqlValidatorOutput(validation_status="NOT_CHECKED", kill_status=f"VALIDATION_FAILED: {e}")

    if not rows:
        log.info("SPID %s confirmed gone.", input.session_id)
        return SqlValidatorOutput(validation_status="CONFIRMED_GONE", kill_status="SUCCESS")

    # If expected_login is given, check that the found session still belongs to the
    # original session owner.  A login mismatch means SQL Server recycled the SPID
    # to a new, innocent session -- the original kill succeeded.
    if input.expected_login:
        found_login = str(rows[0].get("login_name") or "").lower()
        if found_login != input.expected_login.lower():
            log.info(
                "SPID %s now owned by '%s' (expected '%s') -- SPID was recycled, kill succeeded.",
                input.session_id, found_login, input.expected_login,
            )
            return SqlValidatorOutput(validation_status="CONFIRMED_GONE", kill_status="SUCCESS")

    status = str(rows[0].get("status") or "").lower()
    if "rollback" in status or status in ("dormant", "background"):
        # "dormant"    -- transient state while the session finishes rollback and releases locks.
        # "background" -- SQL Server transitions a killed user session to an internal cleanup
        #                 worker with is_user_process=0 and status='background'. The user
        #                 session is gone; cleanup is completing asynchronously.
        log.info("SPID %s is rolling back / dormant / background cleanup -- kill succeeded.", input.session_id)
        return SqlValidatorOutput(validation_status="ROLLING_BACK", kill_status="SUCCESS")

    # "sleeping" can be a transient state immediately after KILL is issued,
    # before the session transitions to "rollback". Retry once after 2 seconds.
    if status == "sleeping":
        log.info("SPID %s still sleeping post-KILL -- retrying in 2s.", input.session_id)
        time.sleep(2)
        try:
            rows = query_sql(input.monitor_conn_str, CHECK_SESSION_SQL, [input.session_id])
        except Exception as e:
            log.error("Retry validation query failed for SPID %s: %s", input.session_id, e)
            return SqlValidatorOutput(validation_status="NOT_CHECKED", kill_status=f"VALIDATION_FAILED: {e}")

        if not rows:
            log.info("SPID %s confirmed gone on retry.", input.session_id)
            return SqlValidatorOutput(validation_status="CONFIRMED_GONE", kill_status="SUCCESS")

        status = str(rows[0].get("status") or "").lower()
        if "rollback" in status or status == "dormant":
            log.info("SPID %s rolling back on retry -- kill succeeded.", input.session_id)
            return SqlValidatorOutput(validation_status="ROLLING_BACK", kill_status="SUCCESS")

    log.warning("SPID %s is still present after KILL (status=%s).", input.session_id, status)
    return SqlValidatorOutput(
        validation_status="STILL_PRESENT",
        kill_status=f"FAILED: SPID {input.session_id} still active (status={status})",
    )
