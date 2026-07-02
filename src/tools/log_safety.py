"""
tools/log_safety.py -- Log & rollback safety analysis tool
------------------------------------------------------------
Step 1 of decision analysis: checks the head blocker's transaction
log usage and derives a kill-safety rating. Falls back to file-level
log usage (sys.master_files) if no active transaction is found in
sys.dm_tran_* (e.g. the session is idle-holding-lock).
"""

import logging
from typing import Callable, Optional

from pydantic import BaseModel

log = logging.getLogger("tools.log_safety")

LOG_SAFETY_SQL = """\
SELECT
    tst.session_id,
    DB_NAME(tdt.database_id)                                    AS database_name,
    CAST(tdt.database_transaction_log_bytes_used  / 1048576.0
         AS DECIMAL(18,2))                                      AS log_used_mb,
    CAST(tdt.database_transaction_log_bytes_reserved / 1048576.0
         AS DECIMAL(18,2))                                      AS log_reserved_mb,
    tdt.database_transaction_log_record_count                   AS log_record_count,
    DATEDIFF(SECOND, tat.transaction_begin_time, SYSUTCDATETIME())
                                                                AS txn_age_seconds,
    CAST(
        DATEDIFF(SECOND, tat.transaction_begin_time, SYSUTCDATETIME()) * 1.2
        AS DECIMAL(10,1))                                       AS estimated_rollback_sec,
    CASE
        WHEN tdt.database_transaction_log_bytes_used < 104857600   THEN 'SAFE_TO_KILL'
        WHEN tdt.database_transaction_log_bytes_used < 524288000   THEN 'WARN_LARGE_ROLLBACK'
        WHEN tdt.database_transaction_log_bytes_used < 2147483648  THEN 'RISKY_VERY_LARGE_ROLLBACK'
        ELSE 'UNSAFE_ROLLBACK_WILL_TAKE_HOURS'
    END                                                         AS kill_safety_rating,
    r.percent_complete,
    r.wait_type
FROM sys.dm_tran_session_transactions    tst
JOIN sys.dm_tran_active_transactions     tat ON tat.transaction_id = tst.transaction_id
JOIN sys.dm_tran_database_transactions   tdt ON tdt.transaction_id = tst.transaction_id
LEFT JOIN sys.dm_exec_requests           r   ON r.session_id = tst.session_id
WHERE tst.session_id = ?
  AND tdt.database_transaction_log_bytes_used > 0
"""

# Fallback log usage when no active transaction exists in dm_tran_*
LOG_FILE_SQL = """\
SELECT
    db.name                                                     AS database_name,
    CAST(SUM(CASE WHEN mf.type=1 THEN mf.size END) * 8.0 / 1024
         AS DECIMAL(18,2))                                      AS log_size_mb,
    CAST(SUM(CASE WHEN mf.type=1
              THEN FILEPROPERTY(mf.name,'SpaceUsed') END) * 8.0 / 1024
         AS DECIMAL(18,2))                                      AS log_used_mb,
    CAST(
        SUM(CASE WHEN mf.type=1 THEN FILEPROPERTY(mf.name,'SpaceUsed') END) * 100.0 /
        NULLIF(SUM(CASE WHEN mf.type=1 THEN mf.size END), 0)
        AS DECIMAL(5,1))                                        AS log_used_pct
FROM sys.databases db
JOIN sys.master_files mf ON mf.database_id = db.database_id
WHERE db.name = ?
GROUP BY db.name
"""


class LogSafetyInput(BaseModel):
    monitor_conn_str: str
    session_id: int
    # Used only if the session has no active transaction in sys.dm_tran_*
    fallback_database_name: Optional[str] = None


class LogSafetyOutput(BaseModel):
    database_name: str = "unknown"
    log_used_mb: float = 0.0
    log_used_pct: float = 0.0
    log_reserved_mb: float = 0.0
    log_record_count: int = 0
    txn_age_seconds: int = 0
    estimated_rollback_sec: float = 0.0
    kill_safety_rating: str = "SAFE_TO_KILL"
    percent_complete: Optional[float] = None
    wait_type: Optional[str] = None


def analyze_log_safety(
    input: LogSafetyInput,
    query_sql: Callable[[str, str, list], list[dict]],
) -> LogSafetyOutput:
    """
    Try sys.dm_tran_* first (active transaction). Fall back to
    sys.master_files (file-level log usage) for the session's database
    if no active transaction is found.
    """
    try:
        rows = query_sql(input.monitor_conn_str, LOG_SAFETY_SQL, [input.session_id])
        if rows:
            r = rows[0]
            return LogSafetyOutput(
                database_name=r.get("database_name") or "unknown",
                log_used_mb=float(r.get("log_used_mb") or 0),
                log_reserved_mb=float(r.get("log_reserved_mb") or 0),
                log_record_count=int(r.get("log_record_count") or 0),
                txn_age_seconds=int(r.get("txn_age_seconds") or 0),
                estimated_rollback_sec=float(r.get("estimated_rollback_sec") or 0),
                kill_safety_rating=r.get("kill_safety_rating") or "SAFE_TO_KILL",
                percent_complete=r.get("percent_complete"),
                wait_type=r.get("wait_type"),
            )
    except Exception as e:
        log.debug("dm_tran log query failed (SPID %s): %s", input.session_id, e)

    if not input.fallback_database_name:
        return LogSafetyOutput()

    try:
        rows = query_sql(input.monitor_conn_str, LOG_FILE_SQL, [input.fallback_database_name])
        if not rows:
            return LogSafetyOutput(database_name=input.fallback_database_name)
        r = rows[0]
        return LogSafetyOutput(
            database_name=r.get("database_name") or input.fallback_database_name,
            log_used_mb=float(r.get("log_used_mb") or 0),
            log_used_pct=float(r.get("log_used_pct") or 0),
        )
    except Exception as e:
        log.warning("Log file query failed (db %s): %s", input.fallback_database_name, e)
        return LogSafetyOutput(database_name=input.fallback_database_name)
