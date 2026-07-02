"""
src/memory/long_term.py  --  Historical incident context for the RCA agent
---------------------------------------------------------------------
Queries BlockingEventLog and KillAuditLog to provide recurrence and
pattern context so the RCA LLM can assess whether an incident is a
first-time occurrence, a repeat offence, or part of a wider pattern.

Pure query contract over an injected query_sql callable.
"""

import logging
from typing import Callable, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("memory.long_term")

LOOKBACK_HOURS_DEFAULT = 72

LOGIN_HISTORY_SQL = """
SELECT
    COUNT(*)                                             AS cnt,
    MAX(EventTimeUTC)                                    AS last_seen_utc,
    MAX(CASE WHEN DecisionTaken IN ('KILL','ALERT_ONLY')
             THEN 1 ELSE 0 END)                          AS was_escalated
FROM dbo.BlockingEventLog
WHERE ServerName = ?
  AND HeadBlockerLogin = ?
  AND EventTimeUTC >= DATEADD(HOUR, -?, SYSUTCDATETIME())
"""

DB_HISTORY_SQL = """
SELECT COUNT(*) AS cnt
FROM dbo.BlockingEventLog
WHERE ServerName = ?
  AND BlockerDatabase = ?
  AND EventTimeUTC >= DATEADD(HOUR, -?, SYSUTCDATETIME())
"""

SERVER_TOTAL_SQL = """
SELECT COUNT(*) AS total_incidents_last_24h
FROM dbo.BlockingEventLog
WHERE ServerName = ?
  AND EventTimeUTC >= DATEADD(HOUR, -24, SYSUTCDATETIME())
"""

TOP_BLOCKERS_SQL = """
SELECT TOP 5 HeadBlockerLogin, COUNT(*) AS cnt
FROM dbo.BlockingEventLog
WHERE ServerName = ?
  AND EventTimeUTC >= DATEADD(HOUR, -?, SYSUTCDATETIME())
GROUP BY HeadBlockerLogin
ORDER BY COUNT(*) DESC
"""

RECENT_KILLS_SQL = """
SELECT TOP 5
    KillTimeUTC, KilledSPID, KilledLogin, KillStatus, DryRun
FROM dbo.KillAuditLog
WHERE ServerName = ?
  AND KillTimeUTC >= DATEADD(HOUR, -?, SYSUTCDATETIME())
ORDER BY KillTimeUTC DESC
"""


class HistoryInput(BaseModel):
    log_conn_str: str
    server_name: str
    login_name: str = ""
    blocker_database: str = ""
    lookback_hours: int = LOOKBACK_HOURS_DEFAULT


class HistoryOutput(BaseModel):
    login_occurrences: int = 0
    login_last_seen_utc: Optional[str] = None
    login_was_escalated_before: bool = False
    database_occurrences: int = 0
    server_total_24h: int = 0
    top_blockers: list[dict] = Field(default_factory=list)
    recent_kills: list[dict] = Field(default_factory=list)
    historical_summary: str = ""


def query_history(
    input: HistoryInput,
    query_sql: Callable,
) -> HistoryOutput:
    log.info("[%s] Querying historical context for login '%s'...",
             input.server_name, input.login_name)

    out = HistoryOutput()

    try:
        # Server-wide queries — always run regardless of login_name

        # Total server incidents in last 24h
        rows = query_sql(input.log_conn_str, SERVER_TOTAL_SQL, [input.server_name])
        if rows:
            out.server_total_24h = int(rows[0].get("total_incidents_last_24h") or 0)

        # Top 5 blocker logins
        rows = query_sql(input.log_conn_str, TOP_BLOCKERS_SQL,
                         [input.server_name, input.lookback_hours])
        if rows:
            out.top_blockers = [{"login": r.get("HeadBlockerLogin"), "count": int(r.get("cnt") or 0)} for r in rows]

        # Recent kills
        rows = query_sql(input.log_conn_str, RECENT_KILLS_SQL,
                         [input.server_name, input.lookback_hours])
        if rows:
            out.recent_kills = [
                {"time": str(r.get("KillTimeUTC") or ""), "spid": int(r.get("KilledSPID") or 0),
                 "login": str(r.get("KilledLogin") or ""), "status": str(r.get("KillStatus") or ""),
                 "dry_run": bool(r.get("DryRun"))}
                for r in rows
            ]

        # Login-specific queries — only when login_name is known
        if input.login_name:
            rows = query_sql(input.log_conn_str, LOGIN_HISTORY_SQL,
                             [input.server_name, input.login_name, input.lookback_hours])
            if rows:
                r = rows[0]
                out.login_occurrences = int(r.get("cnt") or 0)
                out.login_last_seen_utc = str(r.get("last_seen_utc") or "")
                out.login_was_escalated_before = bool(r.get("was_escalated"))

            if input.blocker_database:
                rows = query_sql(input.log_conn_str, DB_HISTORY_SQL,
                                 [input.server_name, input.blocker_database, input.lookback_hours])
                if rows:
                    out.database_occurrences = int(rows[0].get("cnt") or 0)
        else:
            log.info("[%s] No login_name -- skipping login/database history queries", input.server_name)

    except Exception as e:
        log.warning("[%s] History query failed: %s", input.server_name, e)
        out.historical_summary = "Historical data unavailable (query error)."
        return out

    # Build natural-language summary
    parts = []
    if out.login_occurrences == 0:
        parts.append(f"Login '{input.login_name}' has no prior blocking incidents in the last {input.lookback_hours}h.")
    else:
        parts.append(
            f"Login '{input.login_name}' has {out.login_occurrences} prior blocking incident(s) "
            f"in the last {input.lookback_hours}h "
            f"(last seen: {out.login_last_seen_utc or 'N/A'})."
        )
        if out.login_was_escalated_before:
            parts.append("This login has been escalated (KILL/ALERT_ONLY) in the past.")

    if input.blocker_database and out.database_occurrences > 0:
        parts.append(f"Database '{input.blocker_database}' has {out.database_occurrences} prior incident(s).")

    if out.server_total_24h > 1:
        parts.append(f"Server-wide: {out.server_total_24h} total incidents in the last 24h.")

    if out.top_blockers:
        top_str = "; ".join(f"'{b['login']}' ({b['count']}x)" for b in out.top_blockers[:3])
        parts.append(f"Top blocker logins: {top_str}.")

    if out.recent_kills:
        parts.append(f"Recent kills: {len(out.recent_kills)} in last {input.lookback_hours}h.")

    out.historical_summary = " ".join(parts) if parts else "No historical context available."
    log.info("[%s] History: %s", input.server_name, out.historical_summary[:200])

    return out
