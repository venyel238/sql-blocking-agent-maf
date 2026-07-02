"""
tools/plan_cache.py -- Execution plan cache lookup tool
-----------------------------------------------------------
Step 2 of decision analysis: pulls execution plan metadata AND the raw
XML showplan for the head blocker's session.

Two strategies, tried in order:

  1. Active-request plan  -- session has a current dm_exec_requests row
     with a plan_handle.  Returns full metadata + XML.

  2. Query-stats-cache plan -- session is idle (KB scenario 6: sleeping
     with an open uncommitted transaction) so dm_exec_requests has no
     row.  Falls back to dm_exec_connections.most_recent_sql_handle ->
     dm_exec_query_stats -> dm_exec_query_plan.  Uses OUTER APPLY so
     query_hash / query_plan_hash survive plan eviction.

If neither strategy yields a plan (e.g. WAITFOR / WHILE-loop blocker
that has no dm_exec_requests row and no cached plan), the tool reports
a miss. The Analyzer Agent will suggest enabling an Extended Events
session (blocked_process_report + sql_batch_completed) on the target
server to capture the blocking query on the next occurrence.

query_hash / query_plan_hash (strategies 1 or 2) feed into
query_store.py so that tool can look up historical plans in the
Query Store.

plan_xml (either strategy) is used by the Analyzer LLM for:
  - Missing index identification (<MissingIndexes> elements in the XML)
  - Plan shape / operator analysis
  - Archival storage in RCASnapshotLog.BlockerPlanXML
"""

import logging
from typing import Callable, Optional

from pydantic import BaseModel

log = logging.getLogger("tools.plan_cache")

ACTIVE_PLAN_SQL = """\
SELECT TOP 1
    cp.usecounts                                                AS plan_use_count,
    r.query_hash,
    r.query_plan_hash,
    r.cpu_time                                                  AS cpu_ms,
    r.logical_reads,
    0                                                           AS plan_age_minutes,
    SUBSTRING(t.text,
        (r.statement_start_offset / 2) + 1,
        ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(t.text)
          ELSE r.statement_end_offset END - r.statement_start_offset) / 2) + 1)
                                                                AS statement_text,
    TRY_CAST(qp.query_plan AS NVARCHAR(MAX))                    AS plan_xml,
    CASE
        WHEN t.objectid IS NOT NULL THEN
            ISNULL(OBJECT_SCHEMA_NAME(t.objectid, t.dbid) + '.', '')
            + ISNULL(OBJECT_NAME(t.objectid, t.dbid), '')
            + ISNULL(' [' + o.type_desc + ']', '')
        ELSE ''
    END                                                         AS parent_object
FROM sys.dm_exec_requests      r
JOIN sys.dm_exec_cached_plans  cp ON cp.plan_handle = r.plan_handle
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle)              t
CROSS APPLY sys.dm_exec_query_plan(r.plan_handle)           qp
LEFT JOIN sys.objects          o  ON o.object_id = t.objectid
WHERE r.session_id = ?
"""

# Idle / scenario-6 blocker: no dm_exec_requests row; use the connection's
# most recently executed plan from the query stats cache.
# Uses OUTER APPLY so query_hash/query_plan_hash survive plan eviction.
IDLE_PLAN_SQL = """\
SELECT TOP 1
    TRY_CAST(qp.query_plan AS NVARCHAR(MAX))                    AS plan_xml,
    qs.query_hash,
    qs.query_plan_hash,
    qs.total_logical_reads / NULLIF(qs.execution_count, 0)      AS logical_reads,
    qs.total_worker_time   / NULLIF(qs.execution_count, 0)      AS cpu_ms,
    qs.execution_count                                          AS plan_use_count,
    DATEDIFF(MINUTE, qs.creation_time, GETUTCDATE())            AS plan_age_minutes,
    SUBSTRING(t.text, 1, 500)                                   AS statement_text,
    CASE
        WHEN t.objectid IS NOT NULL THEN
            ISNULL(OBJECT_SCHEMA_NAME(t.objectid, t.dbid) + '.', '')
            + ISNULL(OBJECT_NAME(t.objectid, t.dbid), '')
            + ISNULL(' [' + o.type_desc + ']', '')
        ELSE ''
    END                                                         AS parent_object
FROM sys.dm_exec_connections   c
JOIN sys.dm_exec_query_stats   qs ON qs.sql_handle = c.most_recent_sql_handle
OUTER APPLY sys.dm_exec_query_plan(qs.plan_handle)  qp
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle)     t
LEFT JOIN sys.objects          o  ON o.object_id = t.objectid
WHERE c.session_id   = ?
  AND qs.last_execution_time >= DATEADD(HOUR, -?, GETUTCDATE())
ORDER BY qs.last_execution_time DESC
"""

class PlanCacheInput(BaseModel):
    monitor_conn_str: str
    session_id: int
    lookback_hours: int = 24                 # only consider plans used in the last N hours


class PlanCacheOutput(BaseModel):
    hit: bool = False
    plan_use_count: int = 0
    query_hash: str = ""
    query_plan_hash: str = ""
    cpu_ms: int = 0
    logical_reads: int = 0
    plan_age_minutes: int = 0
    statement_text: str = ""
    parent_object: str = ""         # "dbo.usp_UpdateOrderQty [SQL_STORED_PROCEDURE]" or ""
    plan_xml: Optional[str] = None  # raw XML showplan; None if unavailable or evicted
    source: str = ""                # "active_request" | "query_stats_cache" | ""


def _make_pc_output(r: dict, source: str) -> PlanCacheOutput:
    """Build a PlanCacheOutput from a result row dict."""
    return PlanCacheOutput(
        hit=True,
        plan_use_count=int(r.get("plan_use_count") or 0),
        query_hash=str(r.get("query_hash") or ""),
        query_plan_hash=str(r.get("query_plan_hash") or ""),
        cpu_ms=int(r.get("cpu_ms") or 0),
        logical_reads=int(r.get("logical_reads") or 0),
        plan_age_minutes=int(r.get("plan_age_minutes") or 0),
        statement_text=str(r.get("statement_text") or ""),
        parent_object=str(r.get("parent_object") or ""),
        plan_xml=str(r["plan_xml"]) if r.get("plan_xml") else None,
        source=source,
    )


def analyze_plan_cache(
    input: PlanCacheInput,
    query_sql: Callable[[str, str, list], list[dict]],
) -> PlanCacheOutput:
    """Pull execution plan metadata + XML from plan cache for the given session.

    Two strategies:
      1. Active-request plan  -- session has a current dm_exec_requests row.
         Returns immediately only if query_hash is non-empty.
         Otherwise falls through to strategy 2.
      2. Query-stats-cache plan -- idle/sleeping blocker with open transaction.
    """
    # Strategy 1 -- active request
    try:
        rows = query_sql(input.monitor_conn_str, ACTIVE_PLAN_SQL, [input.session_id])
        if rows:
            r = rows[0]
            qh = str(r.get("query_hash") or "")
            if qh:
                return _make_pc_output(r, "active_request")
            # query_hash is empty (WAITFOR/WHILE-loop blocker) -- fall through to S2
    except Exception as e:
        log.debug("Plan cache active-request query failed (SPID %s): %s", input.session_id, e)

    # Strategy 2 -- idle / scenario-6 blocker (filtered by lookback_hours)
    try:
        rows = query_sql(input.monitor_conn_str, IDLE_PLAN_SQL,
                         [input.session_id, input.lookback_hours])
        if rows:
            r = rows[0]
            qh = str(r.get("query_hash") or "")
            if qh:
                return _make_pc_output(r, "query_stats_cache")
    except Exception as e:
        log.debug("Plan cache idle-plan query failed (SPID %s): %s", input.session_id, e)

    # Neither strategy yielded a plan with a valid query_hash.
    # Common for WAITFOR / WHILE-loop head blockers that have no cached plan.
    # The Analyzer Agent will suggest enabling XEvents to capture the query.
    return PlanCacheOutput()
