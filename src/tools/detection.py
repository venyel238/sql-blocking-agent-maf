"""
tools/detection.py -- Detection tool: typed input/output contract
--------------------------------------------------------------------
Pure, framework-agnostic function that checks SQL Server for active
blocking sessions and identifies the head blocker.

Identifying the head blocker, victims, and blocking chain is plain
graph root-finding over `session_id` / `blocking_session_id` pairs --
deterministic, no LLM needed.

No LangGraph / state-dict coupling here -- this can be called from a
CLI, wrapped in an API endpoint, or exposed via an MCP tool. The
LangGraph adapter lives in src/agents/detector/agent.py.
"""

import logging
import time
from typing import Callable, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("tools.detection")

PRE_CHECK_SQL = """\
SELECT TOP 1 1 AS has_blocking
FROM sys.dm_exec_requests
WHERE blocking_session_id > 0
"""

# Reusable OUTER APPLY template for resolving the lock target
# (object name + index name) from sys.dm_tran_locks.
#
# Used twice in BLOCKING_SQL — once for victim WAIT locks (correlated to
# dm_exec_requests) and once for idle-blocker GRANT locks (correlated to
# dm_exec_sessions).  The two calls differ only in the session column
# reference and the request_status filter, captured by {session_col} and
# {status_filter}.
#
# OBJECT_SCHEMA_NAME / OBJECT_NAME always receive tl.resource_database_id
# as the second argument so the lookup resolves in the target database,
# not in the monitoring connection's current database (master).
# sys.partitions is per-database, so cross-database KEY/PAGE/RID locks
# return NULL for the index name but still resolve the object name via
# the two-argument OBJECT_NAME.
_LOCK_RESOLVE_APPLY = """\
OUTER APPLY (
    SELECT TOP 1
        CASE tl.resource_type
            WHEN 'OBJECT' THEN
                ISNULL(OBJECT_SCHEMA_NAME(tl.resource_associated_entity_id,
                                          tl.resource_database_id) + '.', '')
                + ISNULL(OBJECT_NAME(tl.resource_associated_entity_id,
                                     tl.resource_database_id), '')
            ELSE
                ISNULL(OBJECT_SCHEMA_NAME(p.object_id, tl.resource_database_id) + '.', '')
                + ISNULL(OBJECT_NAME(p.object_id, tl.resource_database_id), '')
        END AS lock_object_name,
        CASE tl.resource_type
            WHEN 'KEY'  THEN ix.name
            WHEN 'PAGE' THEN ix.name
            WHEN 'RID'  THEN ix.name
            ELSE NULL
        END AS lock_index_name
    FROM sys.dm_tran_locks tl
    LEFT JOIN sys.partitions p
           ON p.hobt_id = tl.resource_associated_entity_id
          AND tl.resource_type IN ('KEY', 'PAGE', 'RID')
    LEFT JOIN sys.indexes ix
           ON ix.object_id = p.object_id AND ix.index_id = p.index_id
    WHERE tl.request_session_id = {session_col}
      AND {status_filter}
    ORDER BY CASE tl.resource_type
                 WHEN 'KEY'  THEN 1 WHEN 'RID' THEN 2
                 WHEN 'PAGE' THEN 3 ELSE 4 END
) lk"""

# Materialise the two variants up front so BLOCKING_SQL stays readable.
_VICTIM_LOCK_APPLY = _LOCK_RESOLVE_APPLY.format(
    session_col="r.session_id",
    status_filter="tl.request_status = 'WAIT'",
)
_IDLE_LOCK_APPLY = _LOCK_RESOLVE_APPLY.format(
    session_col="s.session_id",
    status_filter="tl.request_status = 'GRANT'\n      AND tl.resource_type  != 'DATABASE'",
)

BLOCKING_SQL = f"""\
-- Victims: sessions actively waiting because they are blocked.
SELECT
    r.session_id,
    r.blocking_session_id,
    r.wait_type,
    r.wait_time          AS wait_time_ms,
    r.status,
    r.command,
    s.login_name,
    s.program_name,
    s.host_name,
    DB_NAME(r.database_id) AS database_name,
    SUBSTRING(
        st.text,
        (r.statement_start_offset / 2) + 1,
        ((CASE r.statement_end_offset
              WHEN -1 THEN DATALENGTH(st.text)
              ELSE r.statement_end_offset
          END - r.statement_start_offset) / 2) + 1
    ) AS sql_text,
    ISNULL(wt.resource_description, '') AS resource_description,
    -- Parent object: schema.name [TYPE] when SQL runs inside a proc/func/trigger
    CASE
        WHEN st.objectid IS NOT NULL THEN
            ISNULL(OBJECT_SCHEMA_NAME(st.objectid, r.database_id) + '.', '')
            + ISNULL(OBJECT_NAME(st.objectid, r.database_id), '')
            + ISNULL(' [' + o.type_desc + ']', '')
        ELSE ''
    END AS parent_object,
    -- Resolved lock target (table + index) the victim is waiting on.
    -- OBJECT locks resolve via OBJECT_NAME; KEY/PAGE/RID resolve via
    -- sys.partitions (hobt_id -> object_id + index_id). Cross-database
    -- locks return NULL for index_name but resolve the object name.
    ISNULL(lk.lock_object_name, '') AS lock_object_name,
    ISNULL(lk.lock_index_name,  '') AS lock_index_name
FROM sys.dm_exec_requests  r
JOIN sys.dm_exec_sessions   s  ON r.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) st
LEFT  JOIN sys.objects o ON o.object_id = st.objectid
OUTER APPLY (
    SELECT TOP 1 resource_description
    FROM sys.dm_os_waiting_tasks
    WHERE session_id = r.session_id AND blocking_session_id > 0
) wt
{_VICTIM_LOCK_APPLY}
WHERE r.blocking_session_id > 0                      -- victims: being blocked
   OR r.session_id IN (                               -- active blockers with a request
       SELECT blocking_session_id
       FROM   sys.dm_exec_requests
       WHERE  blocking_session_id > 0
   )

UNION ALL

-- Head blockers: idle sessions holding locks from an uncommitted transaction
SELECT
    s.session_id,
    0                    AS blocking_session_id,
    'HOLDING_LOCK'       AS wait_type,
    DATEDIFF(MILLISECOND, s.last_request_start_time, GETDATE()) AS wait_time_ms,
    s.status,
    'HOLDING_LOCK'       AS command,
    s.login_name,
    s.program_name,
    s.host_name,
    DB_NAME(s.database_id) AS database_name,
    '(idle -- holding locks from uncommitted transaction)' AS sql_text,
    ''                   AS resource_description,
    ''                   AS parent_object,
    -- For idle blockers resolve the GRANT-mode locks they hold
    ISNULL(lk.lock_object_name, '') AS lock_object_name,
    ISNULL(lk.lock_index_name,  '') AS lock_index_name
FROM sys.dm_exec_sessions s
{_IDLE_LOCK_APPLY}
WHERE s.session_id IN (
    SELECT DISTINCT blocking_session_id
    FROM   sys.dm_exec_requests
    WHERE  blocking_session_id > 0
)
AND s.session_id NOT IN (SELECT session_id FROM sys.dm_exec_requests)
AND s.is_user_process = 1
"""


class DetectionInput(BaseModel):
    """Everything detect_blocking() needs to run -- no LangGraph state required."""
    server_name: str
    monitor_conn_str: str


class HeadBlocker(BaseModel):
    session_id: int
    login_name: str = ""
    host_name: str = ""          # client hostname
    program_name: str = ""       # application/program name
    sql_text: str = ""           # head blocker's own SQL (or "(idle)" for scenario 6)
    blocker_database: str = ""   # database the head blocker session is connected to
    wait_duration_ms: int = 0
    victim_count: int = 0
    victim_spids: list[int] = Field(default_factory=list)
    blocking_chain: str = ""
    # Victim-side detail -- every blocked session's login, database, SQL text
    victim_logins: list[str] = Field(default_factory=list)
    victim_databases: list[str] = Field(default_factory=list)
    victim_sql_texts: list[str] = Field(default_factory=list)
    # Lock resource -- what the victim is waiting on
    wait_type: str = ""          # e.g. LCK_M_U, LCK_M_X, LCK_M_S
    lock_resource: str = ""      # raw resource_description from dm_os_waiting_tasks
    lock_object_name: str = ""   # resolved table/object name e.g. "dbo.Orders"
    lock_index_name: str = ""    # resolved index name e.g. "PK_Orders", "" for heap/OBJECT locks
    # Parent objects -- schema.name [TYPE] when SQL runs inside a proc/function/trigger
    blocker_parent_object: str = ""        # head blocker's parent object (empty for ad-hoc / idle)
    victim_parent_objects: list[str] = Field(default_factory=list)  # one per victim


class DetectionOutput(BaseModel):
    has_blocking: bool
    blocking_rows: list[dict] = Field(default_factory=list)
    head_blocker: Optional[HeadBlocker] = None
    errors: list[str] = Field(default_factory=list)


def detect_blocking(
    input: DetectionInput,
    query_sql: Callable[[str, str], list[dict]],
) -> DetectionOutput:
    """
    Check `input.monitor_conn_str` for active blocking and identify the
    head blocker via deterministic graph root-finding over
    session_id/blocking_session_id pairs.

    query_sql(conn_str, sql) -> list[dict]   -- e.g. BaseAgent.query_sql
    """
    log.info("[%s] Checking for blocking...", input.server_name)

    # Lightweight pre-check: ~2ms single-row query against dm_exec_requests.
    # Skips the expensive multi-join BLOCKING_SQL on the 90%+ of poll cycles
    # where no session is actively blocked.
    try:
        pre = query_sql(input.monitor_conn_str, PRE_CHECK_SQL)
        if not pre:
            log.debug("[%s] Pre-check: no blocking. Skipping full query.", input.server_name)
            return DetectionOutput(has_blocking=False)
    except Exception as e:
        log.warning("[%s] Pre-check query error (falling through to full query): %s", input.server_name, e)

    # One retry on transient connection errors (e.g. SQL Server error 596
    # "session in kill state" when a KILL is mid-flight during detection).
    for attempt in range(2):
        try:
            rows = query_sql(input.monitor_conn_str, BLOCKING_SQL)
            break
        except Exception as e:
            if attempt == 0:
                log.warning("[%s] Detection query transient error (retrying in 2s): %s", input.server_name, e)
                time.sleep(2)
            else:
                log.error("[%s] Detection query error: %s", input.server_name, e)
                return DetectionOutput(has_blocking=False, errors=[f"detection_query_error: {e}"])

    if not rows:
        log.info("[%s] No blocking found this cycle.", input.server_name)
        return DetectionOutput(has_blocking=False)

    return _identify_head_blocker(rows)


def _identify_head_blocker(rows: list[dict]) -> DetectionOutput:
    """
    Deterministically identify the head blocker, its victims, and the
    blocking chain from raw session_id/blocking_session_id rows.

    A "victim" is any row with blocking_session_id > 0. For each
    victim, walk the blocking_session_id chain up to its root (the
    session that is not itself blocked by anything in this row set).
    Victims are grouped by root, and the root with the most victims
    becomes the head blocker.
    """
    by_id = {int(r["session_id"]): r for r in rows}

    def find_root(sid: int) -> int:
        seen = set()
        cur = sid
        while True:
            seen.add(cur)
            row = by_id.get(cur)
            bsid = int(row.get("blocking_session_id") or 0) if row else 0
            if bsid == 0 or bsid not in by_id or bsid in seen:
                return cur
            cur = bsid

    victims = [r for r in rows if int(r.get("blocking_session_id") or 0) > 0]
    if not victims:
        return DetectionOutput(has_blocking=False, blocking_rows=rows)

    groups: dict[int, list[dict]] = {}
    for v in victims:
        root = find_root(int(v["session_id"]))
        groups.setdefault(root, []).append(v)

    head_sid = max(groups, key=lambda sid: len(groups[sid]))
    group = groups[head_sid]
    head_row = by_id.get(head_sid, {})
    victim_spids = [int(v["session_id"]) for v in group]

    # Build lock_resource / wait_type from first victim that has resource info.
    # resource_description from dm_os_waiting_tasks already includes the type
    # prefix, e.g. "KEY: (72057594038648832):(3a8f5d1c2e7b)".
    lock_resource = ""
    wait_type_str = ""
    for v in group:
        rd = str(v.get("resource_description", "") or "").strip()
        if rd:
            lock_resource = rd
        wt = str(v.get("wait_type", "") or "").strip()
        if wt and wt not in ("HOLDING_LOCK", ""):
            wait_type_str = wt
        if lock_resource and wait_type_str:
            break

    if not wait_type_str:
        wait_type_str = str(group[0].get("wait_type", "") or "")

    head_blocker = HeadBlocker(
        session_id=head_sid,
        login_name=str(head_row.get("login_name", "")),
        host_name=str(head_row.get("host_name", "") or ""),
        program_name=str(head_row.get("program_name", "") or ""),
        sql_text=str(head_row.get("sql_text", ""))[:2000],
        blocker_database=str(head_row.get("database_name", "") or ""),
        wait_duration_ms=max((int(v.get("wait_time_ms") or 0) for v in group), default=0),
        victim_count=len(group),
        victim_spids=victim_spids,
        blocking_chain=f"SPID {head_sid} blocking {len(group)} session(s): {victim_spids}",
        victim_logins=[str(v.get("login_name", "") or "") for v in group],
        victim_databases=[str(v.get("database_name", "") or "") for v in group],
        victim_sql_texts=[str(v.get("sql_text", "") or "")[:800] for v in group if v.get("sql_text")],
        wait_type=wait_type_str,
        lock_resource=lock_resource[:500],
        # Prefer the victim's WAIT-lock resolution; fall back to the blocker's
        # GRANT-lock resolution (populated for idle/scenario-6 blockers).
        lock_object_name=next(
            (str(v.get("lock_object_name") or "") for v in group if v.get("lock_object_name")),
            str(head_row.get("lock_object_name", "") or ""),
        )[:256],
        lock_index_name=next(
            (str(v.get("lock_index_name") or "") for v in group if v.get("lock_index_name")),
            str(head_row.get("lock_index_name", "") or ""),
        )[:256],
        blocker_parent_object=str(head_row.get("parent_object", "") or "")[:512],
        victim_parent_objects=[str(v.get("parent_object", "") or "")[:512] for v in group],
    )

    log.info(
        "Detection result: has_blocking=True  victims=%d  head_spid=%s  wait_ms=%d",
        head_blocker.victim_count, head_blocker.session_id, head_blocker.wait_duration_ms,
    )

    return DetectionOutput(
        has_blocking=True,
        blocking_rows=rows,
        head_blocker=head_blocker,
    )
