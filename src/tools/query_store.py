"""
tools/query_store.py -- Query Store plan history tool
------------------------------------------------------
Step 3 of decision analysis.

Checks if Query Store is enabled for the monitored database, then looks
up ALL known execution plans for the blocking query (matched by
query_hash supplied by tools/plan_cache.py).

Per-plan stats returned:
  - avg_duration_ms / stdev_duration_ms  (high stdev = parameter sniffing
    or plan instability / regression)
  - avg_logical_io_reads / stdev_logical_io_reads
  - count_executions
  - plan_xml (XML showplan stored in sys.query_store_plan)

The Analyzer LLM receives all plans and:
  1. Compares plans by avg_duration_ms ± stdev to find the best performer
  2. Scans plan XML for <MissingIndexes> elements and reports them
  3. Flags parameter sniffing when stdev_duration_ms is large relative
     to avg_duration_ms
  4. Recommends sp_query_store_force_plan with the winning plan_id when
     a better historical plan exists

current_plan_xml  -- XML for the plan that is currently executing
best_plan_xml     -- XML for the best historical plan (only set when it
                     differs from the current plan)
"""

import logging
from typing import Callable, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("tools.query_store")

QS_ENABLED_SQL = """\
SELECT CAST(is_query_store_on AS INT) AS qs_on
FROM sys.databases
WHERE name = ?
"""
QS_ENABLED_SQL_DEFAULT = """\
SELECT CAST(is_query_store_on AS INT) AS qs_on
FROM sys.databases
WHERE name = DB_NAME()
"""

QS_PLANS_SQL = """\
SELECT TOP 8
    q.query_id,
    p.plan_id,
    p.query_plan_hash,
    p.is_forced_plan,
    p.force_failure_count,
    CAST(rs.avg_duration           / 1000.0 AS DECIMAL(14,2)) AS avg_duration_ms,
    CAST(rs.stdev_duration         / 1000.0 AS DECIMAL(14,2)) AS stdev_duration_ms,
    CAST(rs.avg_logical_io_reads             AS DECIMAL(14,2)) AS avg_logical_io_reads,
    CAST(rs.stdev_logical_io_reads           AS DECIMAL(14,2)) AS stdev_logical_io_reads,
    rs.count_executions,
    RANK() OVER (
        PARTITION BY q.query_id
        ORDER BY rs.avg_duration ASC, rs.count_executions DESC
    )                                                          AS plan_efficiency_rank,
    -- query_plan_hash is VARBINARY(8); pyodbc returns it as a hex string.
    -- CONVERT(..., 2) renders the binary as hex without 0x prefix so the
    -- string comparison works correctly without implicit conversion errors.
    CASE
        WHEN CONVERT(NVARCHAR(20), p.query_plan_hash, 2) = ?  THEN 'CURRENT_RUNNING_PLAN'
        ELSE                                                        'HISTORICAL_PLAN'
    END                                                        AS plan_status,
    CASE
        WHEN p.is_forced_plan = 1           THEN 'PLAN_ALREADY_FORCED'
        WHEN RANK() OVER (PARTITION BY q.query_id
             ORDER BY rs.avg_duration ASC) = 1
         AND CONVERT(NVARCHAR(20), p.query_plan_hash, 2) != ? THEN 'BETTER_PLAN_EXISTS'
        WHEN RANK() OVER (PARTITION BY q.query_id
             ORDER BY rs.avg_duration ASC) = 1
         AND CONVERT(NVARCHAR(20), p.query_plan_hash, 2)  = ? THEN 'CURRENT_PLAN_IS_OPTIMAL'
        ELSE                                                        'REVIEW_MANUALLY'
    END                                                        AS plan_recommendation,
    TRY_CAST(p.query_plan AS NVARCHAR(MAX))                    AS plan_xml
FROM {DB}.sys.query_store_query               q
JOIN {DB}.sys.query_store_plan                p  ON p.query_id  = q.query_id
JOIN {DB}.sys.query_store_runtime_stats       rs ON rs.plan_id  = p.plan_id
WHERE CONVERT(NVARCHAR(20), q.query_hash, 2) = ?
  AND p.last_execution_time >= DATEADD(HOUR, -?, SYSUTCDATETIME())
ORDER BY plan_efficiency_rank, rs.count_executions DESC
"""


class QueryStoreInput(BaseModel):
    monitor_conn_str: str
    query_hash: str = ""
    query_plan_hash: str = ""
    database_name: str = ""  # DB to check QS on; empty = uses DB_NAME()
    lookback_hours: int = 24 # only consider plans executed in the last N hours



class QueryStorePlan(BaseModel):
    query_id: Optional[int] = None
    plan_id: Optional[int] = None
    query_plan_hash: str = ""
    is_forced_plan: bool = False
    force_failure_count: int = 0
    avg_duration_ms: float = 0.0
    stdev_duration_ms: float = 0.0        # high stdev = parameter sniffing / regression
    avg_logical_io_reads: float = 0.0
    stdev_logical_io_reads: float = 0.0
    count_executions: int = 0
    plan_efficiency_rank: int = 0
    plan_status: str = ""                  # CURRENT_RUNNING_PLAN | HISTORICAL_PLAN
    plan_recommendation: str = ""
    plan_xml: Optional[str] = None         # XML showplan from sys.query_store_plan


class QueryStoreOutput(BaseModel):
    qs_enabled: bool = False
    plans_found: int = 0
    better_plan_exists: bool = False
    best_plan_id: Optional[int] = None
    best_plan_avg_ms: Optional[float] = None
    qs_plan_recommendation: str = ""
    current_plan_rank: Optional[int] = None
    current_plan_xml: Optional[str] = None  # XML for the plan currently executing
    best_plan_xml: Optional[str] = None     # XML for the best plan when different from current
    all_plans: list[QueryStorePlan] = Field(default_factory=list)


def analyze_query_store(
    input: QueryStoreInput,
    query_sql: Callable[[str, str, list], list[dict]],
) -> QueryStoreOutput:
    """Check QS availability, then fetch all plans with stats + XML for the blocking query."""
    # No hashes means plan_cache found nothing — nothing to look up in QS
    if not input.query_hash or not input.query_plan_hash:
        return QueryStoreOutput(qs_enabled=False)

    # Check if Query Store is enabled for the target database
    qs_db = input.database_name or ""
    try:
        if qs_db:
            rows = query_sql(input.monitor_conn_str, QS_ENABLED_SQL, [qs_db])
        else:
            rows = query_sql(input.monitor_conn_str, QS_ENABLED_SQL_DEFAULT, [])
        qs_on = bool(rows and rows[0].get("qs_on"))
    except Exception as e:
        log.debug("QS enabled check failed: %s", e)
        qs_on = False

    if not qs_on:
        log.debug("Query Store not enabled for database %r", qs_db)
        return QueryStoreOutput(qs_enabled=False)

    # QS views are database-scoped -- use 3-part names when a DB is specified
    qs_prefix = f"{qs_db}." if qs_db else ""
    try:
        qs_sql = QS_PLANS_SQL.replace("{DB}.", qs_prefix)
        rows = query_sql(
            input.monitor_conn_str, qs_sql,
            [input.query_plan_hash, input.query_plan_hash, input.query_plan_hash,
             input.query_hash, input.lookback_hours],
        )
    except Exception as e:
        log.debug("Query Store plan query failed: %s", e)
        return QueryStoreOutput(qs_enabled=True)

    if not rows:
        return QueryStoreOutput(qs_enabled=True)

    plans = [
        QueryStorePlan(
            query_id=r.get("query_id"),
            plan_id=r.get("plan_id"),
            query_plan_hash=str(r.get("query_plan_hash") or ""),
            is_forced_plan=bool(r.get("is_forced_plan")),
            force_failure_count=int(r.get("force_failure_count") or 0),
            avg_duration_ms=float(r.get("avg_duration_ms") or 0),
            stdev_duration_ms=float(r.get("stdev_duration_ms") or 0),
            avg_logical_io_reads=float(r.get("avg_logical_io_reads") or 0),
            stdev_logical_io_reads=float(r.get("stdev_logical_io_reads") or 0),
            count_executions=int(r.get("count_executions") or 0),
            plan_efficiency_rank=int(r.get("plan_efficiency_rank") or 0),
            plan_status=str(r.get("plan_status") or ""),
            plan_recommendation=str(r.get("plan_recommendation") or ""),
            plan_xml=str(r["plan_xml"]) if r.get("plan_xml") else None,
        )
        for r in rows
    ]

    best = min(plans, key=lambda p: p.plan_efficiency_rank or 999)
    better_exists = (
        best.plan_efficiency_rank == 1
        and best.query_plan_hash != input.query_plan_hash
    )

    current_plan = next((p for p in plans if p.plan_status == "CURRENT_RUNNING_PLAN"), None)

    return QueryStoreOutput(
        qs_enabled=True,
        plans_found=len(plans),
        better_plan_exists=better_exists,
        best_plan_id=best.plan_id if better_exists else None,
        best_plan_avg_ms=best.avg_duration_ms,
        qs_plan_recommendation=best.plan_recommendation,
        current_plan_rank=current_plan.plan_efficiency_rank if current_plan else None,
        current_plan_xml=current_plan.plan_xml if current_plan else None,
        best_plan_xml=best.plan_xml if better_exists else None,
        all_plans=plans,
    )
