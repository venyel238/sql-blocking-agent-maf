"""
tools/kill_rate.py -- Kill-rate limiter tool
---------------------------------------------
Counts real (non-dry-run) kills issued against a server in the last
hour, from AgentLogDB.dbo.KillAuditLog. Used by the Determination
Agent's R10 gate (max_kills_per_hour) to stop a runaway agent from
killing too aggressively.

Pure I/O contract over an injected query_sql callable.
"""

import logging
from typing import Callable

from pydantic import BaseModel, Field

log = logging.getLogger("tools.kill_rate")

KILL_RATE_SQL = """\
SELECT COUNT(*) AS kill_count
FROM dbo.KillAuditLog
WHERE ServerName = ?
  AND DryRun = 0
  AND KillTimeUTC >= DATEADD(HOUR, -1, SYSUTCDATETIME())
"""


class KillRateInput(BaseModel):
    log_conn_str: str
    server_name: str


class KillRateOutput(BaseModel):
    kills_last_hour: int = 0
    errors: list[str] = Field(default_factory=list)


def check_kill_rate(
    input: KillRateInput,
    query_sql: Callable[[str, str, list], list[dict]],
) -> KillRateOutput:
    try:
        rows = query_sql(input.log_conn_str, KILL_RATE_SQL, [input.server_name])
        kills = int(rows[0]["kill_count"]) if rows else 0
        return KillRateOutput(kills_last_hour=kills)
    except Exception as e:
        log.warning("[%s] kill_rate query failed: %s -- assuming 0", input.server_name, e)
        return KillRateOutput(kills_last_hour=0, errors=[f"kill_rate_query_failed: {e}"])
