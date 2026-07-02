"""
tools/locks.py -- Lock & isolation level analysis tool
---------------------------------------------------------
Step 4 of decision analysis: identifies the most significant lock the
head blocker holds and the session's transaction isolation level.

OBJECT_SCHEMA_NAME is always called with the two-argument form
OBJECT_SCHEMA_NAME(id, database_id) so the schema lookup resolves in
the *target* database rather than the monitoring connection's current
database (typically master).  The one-argument form returns NULL for any
user-database object when the connection is in master.
"""

import logging
from typing import Callable

from pydantic import BaseModel

log = logging.getLogger("tools.locks")

LOCK_SQL = """\
SELECT TOP 10
    tl.resource_type,
    tl.request_mode,
    tl.request_status,
    -- OBJECT locks: resource_associated_entity_id IS the object_id.
    -- KEY/PAGE/RID locks: resource_associated_entity_id is a hobt_id;
    -- resolve through sys.partitions to get the actual object_id.
    -- Always pass tl.resource_database_id as the second argument so
    -- OBJECT_SCHEMA_NAME / OBJECT_NAME resolve in the target database,
    -- not in the monitoring connection's current database (master).
    CASE tl.resource_type
        WHEN 'OBJECT' THEN
            ISNULL(OBJECT_SCHEMA_NAME(tl.resource_associated_entity_id,
                                      tl.resource_database_id) + '.', '')
            + ISNULL(OBJECT_NAME(tl.resource_associated_entity_id,
                                  tl.resource_database_id), '(unknown)')
        ELSE
            ISNULL(OBJECT_SCHEMA_NAME(p.object_id, tl.resource_database_id) + '.', '')
            + ISNULL(OBJECT_NAME(p.object_id, tl.resource_database_id), '(unknown)')
    END                                                         AS locked_object,
    CASE tl.resource_type
        WHEN 'OBJECT'   THEN 'TABLE LOCK — likely missing index or lock escalation triggered'
        WHEN 'PAGE'     THEN 'PAGE LOCK — hot page contention or missing covering index'
        WHEN 'KEY'      THEN 'ROW LOCK (KEY) — normal row-level DML; check for long open transaction'
        WHEN 'RID'      THEN 'HEAP ROW LOCK — heap table has no clustered index; add one'
        WHEN 'DATABASE' THEN 'DATABASE LOCK — DDL operation or backup in progress'
        ELSE tl.resource_type
    END                                                         AS lock_diagnosis,
    CASE s.transaction_isolation_level
        WHEN 0 THEN 'Unspecified'
        WHEN 1 THEN 'READ UNCOMMITTED (NOLOCK)'
        WHEN 2 THEN 'READ COMMITTED'
        WHEN 3 THEN 'REPEATABLE READ — elevated blocking risk'
        WHEN 4 THEN 'SERIALIZABLE — high blocking risk'
        WHEN 5 THEN 'SNAPSHOT (RCSI)'
    END                                                         AS isolation_level,
    s.open_transaction_count
FROM sys.dm_tran_locks    tl
JOIN sys.dm_exec_sessions s   ON tl.request_session_id  = s.session_id
-- KEY/PAGE/RID: hobt_id -> object_id via sys.partitions
LEFT JOIN sys.partitions  p   ON p.hobt_id              = tl.resource_associated_entity_id
                              AND tl.resource_type IN ('KEY', 'PAGE', 'RID')
WHERE tl.request_session_id = ?
  AND tl.resource_type IN ('OBJECT', 'PAGE', 'KEY', 'RID', 'DATABASE', 'METADATA')
ORDER BY tl.resource_type
"""

# Lower number = higher priority when picking the "most significant" lock
_RESOURCE_TYPE_PRIORITY = {"OBJECT": 0, "PAGE": 1, "KEY": 2, "RID": 3, "DATABASE": 4}


class LocksInput(BaseModel):
    monitor_conn_str: str
    session_id: int


class LockDetail(BaseModel):
    resource_type: str = ""
    request_mode: str = ""
    request_status: str = ""
    locked_object: str = ""
    lock_diagnosis: str = ""
    isolation_level: str = ""
    open_transaction_count: int = 0


class LocksOutput(BaseModel):
    lock_type: str = ""
    locked_object: str = ""
    lock_diagnosis: str = ""
    isolation_level: str = ""
    open_txn_count: int = 0


def analyze_locks(
    input: LocksInput,
    query_sql: Callable[[str, str, list], list[dict]],
) -> LocksOutput:
    """Get lock type, locked object, and isolation level for the given session."""
    try:
        rows = query_sql(input.monitor_conn_str, LOCK_SQL, [input.session_id])
    except Exception as e:
        log.debug("Lock query failed (SPID %s): %s", input.session_id, e)
        return LocksOutput()

    if not rows:
        return LocksOutput()

    locks = [
        LockDetail(
            resource_type=str(r.get("resource_type") or ""),
            request_mode=str(r.get("request_mode") or ""),
            request_status=str(r.get("request_status") or ""),
            locked_object=str(r.get("locked_object") or ""),
            lock_diagnosis=str(r.get("lock_diagnosis") or ""),
            isolation_level=str(r.get("isolation_level") or ""),
            open_transaction_count=int(r.get("open_transaction_count") or 0),
        )
        for r in rows
    ]

    top = min(locks, key=lambda lk: _RESOURCE_TYPE_PRIORITY.get(lk.resource_type, 99))

    return LocksOutput(
        lock_type=top.resource_type,
        locked_object=top.locked_object,
        lock_diagnosis=top.lock_diagnosis,
        isolation_level=top.isolation_level,
        open_txn_count=top.open_transaction_count,
    )
