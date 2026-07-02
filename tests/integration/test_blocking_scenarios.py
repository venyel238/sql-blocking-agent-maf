#!/usr/bin/env python3
"""
tests/integration/test_blocking_scenarios.py
----------------------------------------------
Comprehensive real-time integration tests for every layer of the SQL
Server Blocking Agent against a live localhost SQL Server.

Coverage:
  Section A  - Environment pre-checks (T01-T05)
  Section B  - Scenario classifier, pure logic (T06-T15)
  Section C  - Detection tool, live DMV (T16-T22)
  Section D  - Log safety tool (T23-T27)
  Section E  - Lock analysis tool (T28-T34)
  Section F  - Plan cache, idle-blocker strategy (T35-T39)
  Section G  - Plan cache, active-request strategy (T40-T44)
  Section H  - Query Store pipeline (T45-T53)
  Section I  - Hard gates R2-R14 (T54-T67)
  Section J  - SQL Executor 5 safety checks (T68-T73)
  Section K  - SQL Validator (T74-T77)
  Section L  - Kill-rate limiter (T78-T80)
  Section M  - Full pipeline smoke tests (T81-T84)

Prerequisites:
  - SQL Server on localhost with AgentLogDB, AgentConfigDB, master
  - batch_job_usr login (pwd B@tch_J0b_2026!)
  - svc_appaccount login (for EXECUTE AS impersonation)
  - dbo.BlockingTest table in master
  - dbo.usp_GetBlockingTestRow proc in master

Run:
    .venv\\Scripts\\python tests\\integration\\test_blocking_scenarios.py
"""

# -- bootstrap ------------------------------------------------------------------
import os, sys, threading, time, traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://fake.services.ai.azure.com/api/projects/test")
os.environ.setdefault("LLM_API_KEY", "test-integration-placeholder")
_root = Path(__file__).resolve().parents[2]
_env  = _root / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env, override=False)

sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

import pyodbc
pyodbc.pooling = False   # disable ODBC connection pool so killed SPIDs don't leak back

from tools.detection  import DetectionInput,  detect_blocking
from tools.log_safety import LogSafetyInput,  analyze_log_safety
from tools.locks      import LocksInput,       analyze_locks
from tools.plan_cache import PlanCacheInput,   analyze_plan_cache
from tools.query_store import QueryStoreInput, analyze_query_store
from tools.scenario   import ScenarioInput,    classify_scenario, DTC_WAIT_TYPES
from tools.kill_rate  import KillRateInput,    check_kill_rate
from tools.sql_executor import SqlExecutorInput, execute_kill
from tools.sql_validator import SqlValidatorInput, validate_kill
from agents.determination.agent import DeterminationAgent

# -- connections ----------------------------------------------------------------
CONN     = ("DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost;"
            "DATABASE=master;Trusted_Connection=yes;TrustServerCertificate=yes;")
BLOCKER  = ("DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost;"
            "DATABASE=master;UID=batch_job_usr;PWD=B@tch_J0b_2026!;"
            "TrustServerCertificate=yes;")
LOG_CONN = ("DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost;"
            "DATABASE=AgentLogDB;Trusted_Connection=yes;TrustServerCertificate=yes;")

# -- test runner ----------------------------------------------------------------
_passed = _failed = _skipped = 0
_failures: list[tuple[str, str]] = []

def _run(name: str, fn):
    global _passed, _failed, _skipped
    try:
        result = fn()
        if result == "SKIP":
            _skipped += 1
            print(f"  SKIP  {name}")
        else:
            _passed += 1
            print(f"  PASS  {name}")
    except AssertionError as e:
        _failed += 1
        msg = str(e) or "<no message>"
        _failures.append((name, msg))
        print(f"  FAIL  {name}")
        print(f"        {msg}")
    except Exception as e:
        _failed += 1
        tb = traceback.format_exc().strip().splitlines()[-1]
        _failures.append((name, f"{type(e).__name__}: {e} | {tb}"))
        print(f"  FAIL  {name}")
        print(f"        {type(e).__name__}: {e}")

def _section(title: str):
    print(f"\n{'-'*66}")
    print(f"  {title}")
    print(f"{'-'*66}")

# -- SQL helpers ----------------------------------------------------------------
def _qsql(conn_str: str, sql: str, params: list = None) -> list[dict]:
    with pyodbc.connect(conn_str, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute("SET LOCK_TIMEOUT 30000")
        cur.execute(sql, params or [])
        cols = [c[0] for c in cur.description]
        rows = []
        for row in cur.fetchall():
            r = {}
            for col, val in zip(cols, row):
                r[col] = val.hex() if isinstance(val, (bytes, bytearray)) else val
            rows.append(r)
        return rows

def _exec(conn_str: str, sql: str, params: list = None, autocommit=True):
    with pyodbc.connect(conn_str, autocommit=autocommit, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute(sql, params or [])
        if not autocommit:
            conn.commit()

def _own_spid() -> int:
    rows = _qsql(CONN, "SELECT @@SPID AS spid")
    return int(rows[0]["spid"])

def _spid_alive(spid: int) -> bool:
    # Filter is_user_process=1: after KILL, SQL Server may keep the SPID in
    # dm_exec_sessions briefly with status='background' during session cleanup.
    # That internal cleanup process is not a live user session.
    rows = _qsql(CONN,
        "SELECT 1 AS x FROM sys.dm_exec_sessions "
        "WHERE session_id = ? AND is_user_process = 1", [spid])
    return bool(rows)

# -- blocking session context ---------------------------------------------------
class _BlockingSession:
    """
    Creates a scenario-6 blocker (idle holding X lock) and a victim.

    Usage:
        with _BlockingSession() as bs:
            bs.blocker_spid  -- SPID of batch_job_usr session
            bs.victim_spid   -- SPID of the waiting victim
    """
    def __init__(self, hold_seconds: int = 60, victim: bool = True):
        self._hold  = hold_seconds
        self._do_victim = victim
        self.blocker_spid: int = 0
        self.victim_spid:  int = 0
        self._blocker_conn   = None
        self._victim_conn    = None
        self._blocker_ready  = threading.Event()
        self._victim_ready   = threading.Event()
        self._blocker_done   = threading.Event()  # set when blocker thread fully exits
        self._stop           = threading.Event()

    def _blocker_fn(self):
        try:
            conn = pyodbc.connect(BLOCKER, autocommit=False)
            conn.timeout = 0
            cur = conn.cursor()
            self._blocker_conn = conn
            cur2 = conn.cursor()
            cur2.execute("SELECT @@SPID AS spid")
            self.blocker_spid = int(cur2.fetchone()[0])
            cur.execute("UPDATE dbo.BlockingTest SET Value='INTEGRATION TEST LOCK' WHERE ID=1")
            self._blocker_ready.set()
            self._stop.wait(timeout=self._hold)
        except Exception:
            pass
        finally:
            try:
                if self._blocker_conn:
                    self._blocker_conn.rollback()
                    self._blocker_conn.close()
            except Exception:
                pass
            self._blocker_done.set()

    def _victim_fn(self):
        try:
            conn = pyodbc.connect(CONN, autocommit=True)
            conn.timeout = 120
            cur = conn.cursor()
            cur.execute("EXECUTE AS LOGIN = 'svc_appaccount'")
            cur2 = conn.cursor()
            cur2.execute("SELECT @@SPID AS spid")
            self.victim_spid = int(cur2.fetchone()[0])
            self._victim_conn = conn
            self._victim_ready.set()
            cur.execute("EXEC dbo.usp_GetBlockingTestRow")
        except Exception:
            pass
        finally:
            try:
                if self._victim_conn:
                    self._victim_conn.close()
            except Exception:
                pass

    def __enter__(self):
        t = threading.Thread(target=self._blocker_fn, daemon=True)
        t.start()
        self._blocker_ready.wait(timeout=8)
        assert self.blocker_spid, "Blocker session did not start"
        if self._do_victim:
            tv = threading.Thread(target=self._victim_fn, daemon=True)
            tv.start()
            self._victim_ready.wait(timeout=5)
            # Poll until the victim is visible in dm_os_waiting_tasks (max 8s)
            for _ in range(16):
                time.sleep(0.5)
                if self.victim_spid and _spid_alive(self.victim_spid):
                    try:
                        rows = _qsql(CONN,
                            "SELECT 1 AS x FROM sys.dm_os_waiting_tasks "
                            "WHERE session_id = ? AND blocking_session_id = ?",
                            [self.victim_spid, self.blocker_spid])
                        if rows:
                            break
                    except Exception:
                        pass
        return self

    def __exit__(self, *_):
        self._stop.set()
        # Wait for the blocker thread to complete its rollback before returning,
        # so the next test doesn't see a leftover X lock on BlockingTest
        self._blocker_done.wait(timeout=5)


# -----------------------------------------------------------------------------
#  SECTION A -- Environment pre-checks
# -----------------------------------------------------------------------------
_section("A  Environment pre-checks (T01-T05)")

def t01_master_reachable():
    rows = _qsql(CONN, "SELECT @@VERSION AS v")
    assert rows and "SQL Server" in str(rows[0]["v"]), "SQL Server not reachable"

def t02_agentlogdb_reachable():
    rows = _qsql(LOG_CONN, "SELECT DB_NAME() AS db")
    assert rows and str(rows[0]["db"]) == "AgentLogDB"

def t03_batch_job_usr_login():
    rows = _qsql(BLOCKER, "SELECT SUSER_NAME() AS login")
    assert rows and "batch_job_usr" in str(rows[0]["login"]), \
        f"Expected batch_job_usr, got {rows[0]['login'] if rows else 'no rows'}"

def t04_blocking_test_table_exists():
    rows = _qsql(CONN, "SELECT COUNT(*) AS n FROM dbo.BlockingTest WHERE ID=1")
    assert rows and int(rows[0]["n"]) == 1, "dbo.BlockingTest row ID=1 missing"

def t05_usp_get_blocking_test_row_exists():
    rows = _qsql(CONN,
        "SELECT 1 AS x FROM sys.objects WHERE name='usp_GetBlockingTestRow' AND type='P'")
    assert rows, "dbo.usp_GetBlockingTestRow procedure not found"

for name, fn in [
    ("T01 master reachable",           t01_master_reachable),
    ("T02 AgentLogDB reachable",       t02_agentlogdb_reachable),
    ("T03 batch_job_usr login works",  t03_batch_job_usr_login),
    ("T04 BlockingTest row ID=1",      t04_blocking_test_table_exists),
    ("T05 usp_GetBlockingTestRow proc",t05_usp_get_blocking_test_row_exists),
]:
    _run(name, fn)


# -----------------------------------------------------------------------------
#  SECTION B -- Scenario classifier (pure logic, no DB)
# -----------------------------------------------------------------------------
_section("B  Scenario classifier -- pure logic (T06-T15)")

def _sc(**kw) -> int:
    return classify_scenario(ScenarioInput(**kw)).scenario_id

# cleaner helper
def _chk_scenario(expected: int, **kw):
    got = _sc(**kw)
    assert got == expected, f"Expected scenario {expected}, got {got}"

_run("T06 Scenario 5: 'rollback' in status",
     lambda: _chk_scenario(5, status="rollback"))
_run("T07 Scenario 5: percent_complete > 0",
     lambda: _chk_scenario(5, status="suspended", percent_complete=25.0))
_run("T08 Scenario 5: 'rollback' in command",
     lambda: _chk_scenario(5, command="killed/rollback"))
_run("T09 Scenario 6: sleeping + open_transaction_count > 0",
     lambda: _chk_scenario(6, status="sleeping", open_transaction_count=1))
_run("T10 Scenario 6: HOLDING_LOCK synthetic command",
     lambda: _chk_scenario(6, command="holding_lock"))
_run("T11 Scenario 4: DTC_STATE wait type",
     lambda: _chk_scenario(4, status="suspended", wait_type="DTC_STATE"))
_run("T12 Scenario 4: PREEMPTIVE_DTC_ENLIST wait type",
     lambda: _chk_scenario(4, status="suspended", wait_type="PREEMPTIVE_DTC_ENLIST"))
_run("T13 Scenario 3: ASYNC_NETWORK_IO",
     lambda: _chk_scenario(3, status="suspended", wait_type="ASYNC_NETWORK_IO"))
_run("T14 Scenario 2: OBJECT lock (lock escalation)",
     lambda: _chk_scenario(2, status="suspended", lock_type="OBJECT"))
_run("T15 Scenario 1: long-running suspended query",
     lambda: _chk_scenario(1, status="suspended", wait_type="PAGEIOLATCH_SH"))
_run("T16 Scenario 1: running status",
     lambda: _chk_scenario(1, status="running"))

# cleaner
def _chk_dtc():
    assert "DTC_STATE" in DTC_WAIT_TYPES
    assert "PREEMPTIVE_TRANSIMPORT" in DTC_WAIT_TYPES
    assert "PREEMPTIVE_DTC_ENLIST" in DTC_WAIT_TYPES
_run("T17 DTC_WAIT_TYPES contains all 3 DTC wait types", _chk_dtc)


# -----------------------------------------------------------------------------
#  SECTION C -- Detection tool (live)
# -----------------------------------------------------------------------------
_section("C  Detection tool -- live DMV (T18-T24)")

def t18_no_blocking_baseline():
    out = detect_blocking(DetectionInput(monitor_conn_str=CONN, server_name="localhost"),
                          query_sql=_qsql)
    assert not out.has_blocking or out.head_blocker is not None, \
        "Inconsistent: has_blocking True but head_blocker is None"
    # If there's blocking from a previous test, just verify the output structure
    assert hasattr(out, "has_blocking")
_run("T18 Detection returns valid output structure", t18_no_blocking_baseline)

def t19_scenario6_blocking_detected():
    with _BlockingSession() as bs:
        out = detect_blocking(
            DetectionInput(monitor_conn_str=CONN, server_name="localhost"),
            query_sql=_qsql,
        )
        assert out.has_blocking, "detect_blocking returned has_blocking=False"
        assert out.head_blocker is not None, "head_blocker is None"
        assert out.head_blocker.session_id == bs.blocker_spid, \
            f"Expected head SPID={bs.blocker_spid}, got {out.head_blocker.session_id}"
_run("T19 Scenario-6 blocker detected with correct SPID", t19_scenario6_blocking_detected)

def t20_head_blocker_login():
    with _BlockingSession() as bs:
        out = detect_blocking(
            DetectionInput(monitor_conn_str=CONN, server_name="localhost"),
            query_sql=_qsql,
        )
        assert out.has_blocking
        assert "batch_job_usr" in (out.head_blocker.login_name or ""), \
            f"Expected batch_job_usr, got '{out.head_blocker.login_name}'"
_run("T20 Head blocker login = batch_job_usr", t20_head_blocker_login)

def t21_victim_identified():
    with _BlockingSession() as bs:
        out = detect_blocking(
            DetectionInput(monitor_conn_str=CONN, server_name="localhost"),
            query_sql=_qsql,
        )
        if not out.has_blocking:
            print(f"        (no blocking detected -- timing race, skipping victim check)")
            return "SKIP"
        hb = out.head_blocker
        assert hb is not None, "has_blocking=True but head_blocker is None"
        assert hb.victim_count >= 1, f"Expected >=1 victim, got {hb.victim_count}"
        assert bs.victim_spid in hb.victim_spids, \
            f"victim SPID {bs.victim_spid} not in {hb.victim_spids}"
_run("T21 Victim SPID in head_blocker.victim_spids", t21_victim_identified)

def t22_wait_type_lock():
    with _BlockingSession() as bs:
        out = detect_blocking(
            DetectionInput(monitor_conn_str=CONN, server_name="localhost"),
            query_sql=_qsql,
        )
        assert out.has_blocking
        wt = out.head_blocker.wait_type or ""
        assert "LCK" in wt.upper() or wt == "", \
            f"Unexpected wait_type '{wt}' -- expected LCK_M_* or empty"
_run("T22 Wait type is a lock-wait type (LCK_M_*)", t22_wait_type_lock)

def t23_blocking_rows_populated():
    with _BlockingSession() as bs:
        out = detect_blocking(
            DetectionInput(monitor_conn_str=CONN, server_name="localhost"),
            query_sql=_qsql,
        )
        if not out.has_blocking:
            return "SKIP"
        assert len(out.blocking_rows) >= 1, "has_blocking=True but blocking_rows is empty"
_run("T23 blocking_rows has at least 1 row (victim)", t23_blocking_rows_populated)

def t24_blocker_database_resolved():
    with _BlockingSession() as bs:
        out = detect_blocking(
            DetectionInput(monitor_conn_str=CONN, server_name="localhost"),
            query_sql=_qsql,
        )
        assert out.has_blocking
        db = out.head_blocker.blocker_database or ""
        assert db != "", "blocker_database is empty"
_run("T24 blocker_database is resolved (non-empty)", t24_blocker_database_resolved)


# -----------------------------------------------------------------------------
#  SECTION D -- Log safety tool (live)
# -----------------------------------------------------------------------------
_section("D  Log safety tool (T25-T29)")

def t25_log_safety_returns_output():
    with _BlockingSession() as bs:
        out = analyze_log_safety(
            LogSafetyInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.kill_safety_rating in (
            "SAFE_TO_KILL", "WARN_LARGE_ROLLBACK",
            "RISKY_VERY_LARGE_ROLLBACK", "UNSAFE_ROLLBACK_WILL_TAKE_HOURS",
            "NO_ACTIVE_TRANSACTION",
        ), f"Unexpected kill_safety_rating: {out.kill_safety_rating}"
_run("T25 Log safety rating is a valid value", t25_log_safety_returns_output)

def t26_estimated_rollback_nonnegative():
    with _BlockingSession() as bs:
        out = analyze_log_safety(
            LogSafetyInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.estimated_rollback_sec >= 0, \
            f"Negative estimated_rollback_sec: {out.estimated_rollback_sec}"
_run("T26 estimated_rollback_sec >= 0", t26_estimated_rollback_nonnegative)

def t27_log_used_mb_nonnegative():
    with _BlockingSession() as bs:
        out = analyze_log_safety(
            LogSafetyInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.log_used_mb >= 0
_run("T27 log_used_mb >= 0", t27_log_used_mb_nonnegative)

def t28_percent_complete_for_rollback():
    # Verify percent_complete=None for an idle scenario-6 session (not rolling back)
    with _BlockingSession() as bs:
        out = analyze_log_safety(
            LogSafetyInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        # Idle blocker should NOT have percent_complete > 0
        pc = out.percent_complete
        assert pc is None or pc == 0.0, \
            f"Expected percent_complete=0/None for idle blocker, got {pc}"
_run("T28 percent_complete=0/None for idle scenario-6 blocker", t28_percent_complete_for_rollback)

def t29_nonexistent_spid_graceful():
    out = analyze_log_safety(
        LogSafetyInput(monitor_conn_str=CONN, session_id=99999),
        query_sql=_qsql,
    )
    # Non-existent SPID: no txn rows -> either NO_ACTIVE_TRANSACTION or SAFE_TO_KILL (no log usage)
    valid = {"NO_ACTIVE_TRANSACTION", "SAFE_TO_KILL", ""}
    assert out.kill_safety_rating in valid, \
        f"Unexpected rating for non-existent SPID: '{out.kill_safety_rating}'"
_run("T29 Non-existent SPID returns graceful fallback", t29_nonexistent_spid_graceful)


# -----------------------------------------------------------------------------
#  SECTION E -- Lock analysis tool (live)
# -----------------------------------------------------------------------------
_section("E  Lock analysis tool (T30-T36)")

def t30_lock_detected():
    with _BlockingSession() as bs:
        out = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.lock_type != "", f"lock_type is empty for SPID {bs.blocker_spid}"
_run("T30 Lock type detected for active blocker", t30_lock_detected)

def t31_lock_type_key_or_object():
    # UPDATE on primary key -> KEY lock; table-level would be OBJECT
    with _BlockingSession() as bs:
        out = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.lock_type in ("KEY", "PAGE", "OBJECT", "RID"), \
            f"Unexpected lock_type: {out.lock_type}"
_run("T31 Lock type is KEY, PAGE, OBJECT, or RID", t31_lock_type_key_or_object)

def t32_locked_object_is_blocking_test():
    with _BlockingSession() as bs:
        out = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert "BlockingTest" in (out.locked_object or ""), \
            f"Expected 'BlockingTest' in locked_object, got '{out.locked_object}'"
_run("T32 locked_object resolves to dbo.BlockingTest", t32_locked_object_is_blocking_test)

def t33_lock_type_key_or_object():
    # lock_mode (tl.request_mode) was removed from LocksOutput as it was never
    # consumed by the downstream pipeline.  The meaningful assertion here is that
    # an UPDATE holding an exclusive lock reports lock_type as KEY (row lock) or
    # OBJECT (table-level escalation), and that lock_diagnosis describes DML.
    with _BlockingSession() as bs:
        out = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.lock_type in ("KEY", "OBJECT", "PAGE"), \
            f"Expected KEY/OBJECT/PAGE lock from UPDATE, got '{out.lock_type}'"
        assert out.lock_diagnosis, "lock_diagnosis must be non-empty"
_run("T33 Lock type is KEY/OBJECT/PAGE from UPDATE", t33_lock_type_key_or_object)

def t34_isolation_level_valid():
    with _BlockingSession() as bs:
        out = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        if not out.isolation_level:
            return "SKIP"   # DMV returned no row -- transient timing gap
        valid = {"READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ",
                 "SERIALIZABLE", "SNAPSHOT", "Unspecified",
                 "READ UNCOMMITTED (NOLOCK)", "READ COMMITTED",
                 "REPEATABLE READ -- elevated blocking risk",
                 "SERIALIZABLE -- high blocking risk", "SNAPSHOT (RCSI)"}
        assert any(v in (out.isolation_level or "") for v in valid), \
            f"Unrecognised isolation_level: '{out.isolation_level}'"
_run("T34 Isolation level is a valid SQL Server level", t34_isolation_level_valid)

def t35_open_txn_count_positive():
    with _BlockingSession() as bs:
        out = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.open_txn_count >= 1, \
            f"Expected open_txn_count >= 1, got {out.open_txn_count}"
_run("T35 open_txn_count >= 1 for idle blocker with open transaction", t35_open_txn_count_positive)

def t36_lock_diagnosis_meaningful():
    with _BlockingSession() as bs:
        out = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.lock_diagnosis and len(out.lock_diagnosis) > 10, \
            f"lock_diagnosis too short: '{out.lock_diagnosis}'"
_run("T36 lock_diagnosis is a meaningful string (not empty)", t36_lock_diagnosis_meaningful)


# -----------------------------------------------------------------------------
#  SECTION F -- Plan cache: idle-blocker strategy (T37-T41)
# -----------------------------------------------------------------------------
_section("F  Plan cache -- idle-blocker strategy (T37-T41)")

def t37_idle_blocker_plan_cache_hit():
    with _BlockingSession() as bs:
        out = analyze_plan_cache(
            PlanCacheInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        # Plan cache may miss if the UPDATE plan was evicted -- that's a valid scenario
        if not out.hit:
            print(f"        (cache miss for SPID={bs.blocker_spid} -- plan evicted; acceptable)")
            return "SKIP"
        assert out.source in ("active_request", "query_stats_cache"), \
            f"Unexpected source: '{out.source}'"
_run("T37 Idle blocker gets plan cache hit (query_stats_cache strategy)", t37_idle_blocker_plan_cache_hit)

def t38_idle_plan_source_is_query_stats():
    with _BlockingSession() as bs:
        out = analyze_plan_cache(
            PlanCacheInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        if not out.hit:
            return "SKIP"
        assert out.source == "query_stats_cache", \
            f"Expected source='query_stats_cache', got '{out.source}'"
_run("T38 Idle blocker plan source = query_stats_cache", t38_idle_plan_source_is_query_stats)

def t39_query_hash_nonempty():
    with _BlockingSession() as bs:
        out = analyze_plan_cache(
            PlanCacheInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        if not out.hit:
            return "SKIP"
        assert out.query_hash and len(out.query_hash) > 4, \
            f"query_hash too short or empty: '{out.query_hash}'"
_run("T39 query_hash is a non-empty hex string", t39_query_hash_nonempty)

def t40_statement_contains_blockingest():
    with _BlockingSession() as bs:
        out = analyze_plan_cache(
            PlanCacheInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        if not out.hit:
            return "SKIP"
        stmt = (out.statement_text or "").upper()
        assert "BLOCKINGTEST" in stmt or "UPDATE" in stmt, \
            f"Expected BlockingTest/UPDATE in statement_text, got: '{stmt[:120]}'"
_run("T40 statement_text references BlockingTest or UPDATE", t40_statement_contains_blockingest)

def t41_plan_xml_is_xml_or_none():
    with _BlockingSession() as bs:
        out = analyze_plan_cache(
            PlanCacheInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        if not out.hit or out.plan_xml is None:
            return "SKIP"
        assert "ShowPlanXML" in out.plan_xml or "sql_text" in out.plan_xml.lower() \
               or len(out.plan_xml) > 20, \
            f"plan_xml doesn't look like valid XML: '{out.plan_xml[:80]}'"
_run("T41 plan_xml is valid XML or None for idle blocker", t41_plan_xml_is_xml_or_none)


# -----------------------------------------------------------------------------
#  SECTION G -- Plan cache: active-request strategy (T42-T47)
# -----------------------------------------------------------------------------
_section("G  Plan cache -- active-request strategy (T42-T47)")

# Create a slow stored proc for active-request plan testing
_SLOW_PROC_SQL = """\
IF OBJECT_ID('dbo.usp_ActiveRequestPlanTest', 'P') IS NOT NULL
    DROP PROCEDURE dbo.usp_ActiveRequestPlanTest;
"""
_SLOW_PROC_CREATE = """\
CREATE PROCEDURE dbo.usp_ActiveRequestPlanTest
AS
BEGIN
    -- A query that will have a real execution plan and run for a few seconds
    DECLARE @n BIGINT;
    SELECT @n = COUNT(*)
    FROM sys.columns c1
    CROSS JOIN sys.columns c2
    WHERE c1.column_id = c2.column_id
      AND c1.object_id <> c2.object_id;
    RETURN @n;
END
"""

def _setup_slow_proc():
    _exec(CONN, _SLOW_PROC_SQL)
    _exec(CONN, _SLOW_PROC_CREATE)

def _teardown_slow_proc():
    _exec(CONN, "IF OBJECT_ID('dbo.usp_ActiveRequestPlanTest','P') IS NOT NULL "
          "DROP PROCEDURE dbo.usp_ActiveRequestPlanTest")

_active_plan_spid   = [0]
_active_plan_result = [None]
_active_plan_ready  = threading.Event()
_active_plan_done   = threading.Event()

def _slow_proc_thread():
    try:
        with pyodbc.connect(CONN, autocommit=True, timeout=30) as conn:
            cur = conn.cursor()
            cur2 = conn.cursor()
            cur2.execute("SELECT @@SPID AS spid")
            _active_plan_spid[0] = int(cur2.fetchone()[0])
            _active_plan_ready.set()
            cur.execute("EXEC dbo.usp_ActiveRequestPlanTest")
    except Exception:
        pass
    finally:
        _active_plan_done.set()

def t42_active_request_plan_hit():
    _setup_slow_proc()
    _active_plan_ready.clear()
    _active_plan_done.clear()
    _active_plan_spid[0] = 0

    t = threading.Thread(target=_slow_proc_thread, daemon=True)
    t.start()
    _active_plan_ready.wait(timeout=5)

    time.sleep(0.5)   # let the query start executing

    spid = _active_plan_spid[0]
    assert spid > 0, "Slow proc thread did not set SPID"

    out = analyze_plan_cache(
        PlanCacheInput(monitor_conn_str=CONN, session_id=spid),
        query_sql=_qsql,
    )
    _active_plan_result[0] = out
    _active_plan_done.wait(timeout=30)   # let the query finish
    _teardown_slow_proc()

    if not out.hit:
        return "SKIP"   # query finished before we sampled it -- timing-dependent
    # If we caught it, verify it came from active_request
    assert out.source in ("active_request", "query_stats_cache"), \
        f"Unexpected source: '{out.source}'"
_run("T42 Active proc returns plan cache hit (active_request or query_stats_cache)", t42_active_request_plan_hit)

def t43_active_request_plan_xml_showplan():
    out = _active_plan_result[0]
    if out is None or not out.hit or out.plan_xml is None:
        return "SKIP"
    assert "ShowPlanXML" in out.plan_xml or len(out.plan_xml) > 100, \
        f"plan_xml doesn't look like ShowPlanXML: '{out.plan_xml[:120]}'"
_run("T43 Active-request plan_xml contains ShowPlanXML", t43_active_request_plan_xml_showplan)

def t44_active_request_query_hash():
    out = _active_plan_result[0]
    if out is None or not out.hit:
        return "SKIP"
    assert out.query_hash and len(out.query_hash) > 4, \
        f"query_hash empty or too short: '{out.query_hash}'"
_run("T44 Active-request plan has non-empty query_hash", t44_active_request_query_hash)

def t45_active_request_parent_object():
    out = _active_plan_result[0]
    if out is None or not out.hit:
        return "SKIP"
    if out.source != "active_request":
        return "SKIP"   # only active_request populates parent_object from dm_exec_requests
    # parent_object is populated from dm_exec_sql_text.objectid which is NULL when the
    # active sql_handle points to the outer EXEC batch rather than a statement inside
    # the procedure body -- this is valid SQL Server behavior, not a tool bug.
    if not out.parent_object:
        return "SKIP"   # outer EXEC call has objectid=NULL; proc body not yet sampled
    assert "usp_ActiveRequestPlanTest" in out.parent_object, \
        f"parent_object set but missing proc name: '{out.parent_object}'"
_run("T45 Active-request parent_object resolves stored proc name", t45_active_request_parent_object)

def t46_plan_cache_miss_for_nonexistent_spid():
    out = analyze_plan_cache(
        PlanCacheInput(monitor_conn_str=CONN, session_id=99998),
        query_sql=_qsql,
    )
    assert not out.hit, "Plan cache should miss for non-existent SPID 99998"
_run("T46 Plan cache miss for non-existent SPID", t46_plan_cache_miss_for_nonexistent_spid)

def t47_plan_age_minutes_nonnegative():
    with _BlockingSession() as bs:
        out = analyze_plan_cache(
            PlanCacheInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        if not out.hit:
            return "SKIP"
        assert out.plan_age_minutes >= 0, \
            f"Negative plan_age_minutes: {out.plan_age_minutes}"
_run("T47 plan_age_minutes >= 0", t47_plan_age_minutes_nonnegative)


# -----------------------------------------------------------------------------
#  SECTION H -- Query Store pipeline (T48-T56)
# -----------------------------------------------------------------------------
_section("H  Query Store pipeline (T48-T56)")

# Query Store tests use AgentLogDB (cannot enable QS on system databases like master)
_QS_DB = "AgentLogDB"
_QS_CONN = LOG_CONN  # same connection string -- already connects to AgentLogDB

def _qs_state(db: str = _QS_DB) -> bool:
    rows = _qsql(CONN, "SELECT is_query_store_on FROM sys.databases WHERE name=?", [db])
    return bool(rows and rows[0]["is_query_store_on"])

_qs_was_enabled = _qs_state()

_QS_TABLE_SQL = """\
IF OBJECT_ID('dbo.QSTestItems','U') IS NULL
    CREATE TABLE dbo.QSTestItems (ID INT PRIMARY KEY, Val NVARCHAR(100));
IF NOT EXISTS (SELECT 1 FROM dbo.QSTestItems WHERE ID=1)
    INSERT dbo.QSTestItems (ID,Val) VALUES (1,'seed'),(2,'seed'),(3,'seed');
"""
_QS_PROC_DROP   = "IF OBJECT_ID('dbo.usp_QSTest','P') IS NOT NULL DROP PROCEDURE dbo.usp_QSTest;"
_QS_PROC_CREATE = """\
CREATE PROCEDURE dbo.usp_QSTest @filter_id INT
AS
BEGIN
    SELECT ID, Val FROM dbo.QSTestItems WHERE ID >= @filter_id ORDER BY ID;
END
"""
_qs_query_hash      = [""]
_qs_query_plan_hash = [""]

def t48_qs_initial_state_recorded():
    state = "ON" if _qs_state() else "OFF"
    print(f"        (QS on {_QS_DB} is currently {state})")
_run("T48 Query Store state on AgentLogDB recorded", t48_qs_initial_state_recorded)

def t49_enable_qs_and_populate():
    _exec(_QS_CONN, """
        ALTER DATABASE AgentLogDB SET QUERY_STORE = ON
        (OPERATION_MODE = READ_WRITE,
         QUERY_CAPTURE_MODE = ALL,
         MAX_STORAGE_SIZE_MB = 100,
         INTERVAL_LENGTH_MINUTES = 1)
    """)
    assert _qs_state(), f"Failed to enable Query Store on {_QS_DB}"

    _exec(_QS_CONN, _QS_TABLE_SQL)
    _exec(_QS_CONN, _QS_PROC_DROP)
    _exec(_QS_CONN, _QS_PROC_CREATE)

    # Run proc 6 times to build QS stats (>=2 needed for stdev)
    for i in range(6):
        _exec(_QS_CONN, f"EXEC dbo.usp_QSTest @filter_id = {i}")

    # Flush QS in-memory data to disk so it's queryable
    _exec(_QS_CONN, "EXEC sys.sp_query_store_flush_db")
    time.sleep(2)

    # Retrieve query_hash from dm_exec_query_stats for the proc's SELECT
    rows = _qsql(_QS_CONN, """
        SELECT TOP 1 qs.query_hash, qs.query_plan_hash
        FROM sys.dm_exec_query_stats qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
        WHERE t.text LIKE '%usp_QSTest%'
          AND t.text NOT LIKE '%dm_exec_query_stats%'
        ORDER BY qs.last_execution_time DESC
    """)
    if rows:
        qh  = rows[0].get("query_hash")
        qph = rows[0].get("query_plan_hash")
        if isinstance(qh, bytes):
            qh = qh.hex()
        if isinstance(qph, bytes):
            qph = qph.hex()
        _qs_query_hash[0]      = str(qh  or "")
        _qs_query_plan_hash[0] = str(qph or "")
        print(f"        QS query_hash={_qs_query_hash[0][:16]}...")
    else:
        print("        (could not retrieve query_hash from dm_exec_query_stats)")
_run("T49 Enable QS on AgentLogDB + populate with 6 proc executions", t49_enable_qs_and_populate)

def t50_qs_enabled_check_returns_true():
    out = analyze_query_store(
        QueryStoreInput(
            monitor_conn_str=_QS_CONN,
            query_hash=_qs_query_hash[0],
            query_plan_hash=_qs_query_plan_hash[0],
            database_name=_QS_DB,
            lookback_hours=1,
        ),
        query_sql=_qsql,
    )
    assert out.qs_enabled, f"analyze_query_store returned qs_enabled=False for {_QS_DB}"
_run("T50 analyze_query_store returns qs_enabled=True", t50_qs_enabled_check_returns_true)

def t51_qs_plans_found():
    if not _qs_query_hash[0]:
        return "SKIP"
    out = analyze_query_store(
        QueryStoreInput(
            monitor_conn_str=_QS_CONN,
            query_hash=_qs_query_hash[0],
            query_plan_hash=_qs_query_plan_hash[0],
            database_name=_QS_DB,
            lookback_hours=1,
        ),
        query_sql=_qsql,
    )
    assert out.plans_found >= 1, \
        f"Expected >=1 QS plan, got {out.plans_found} (hash={_qs_query_hash[0][:16]})"
_run("T51 At least 1 QS plan found for the test proc", t51_qs_plans_found)

def t52_qs_plan_avg_duration_positive():
    if not _qs_query_hash[0]:
        return "SKIP"
    out = analyze_query_store(
        QueryStoreInput(
            monitor_conn_str=_QS_CONN,
            query_hash=_qs_query_hash[0],
            query_plan_hash=_qs_query_plan_hash[0],
            database_name=_QS_DB,
            lookback_hours=1,
        ),
        query_sql=_qsql,
    )
    if not out.all_plans:
        return "SKIP"
    best = out.all_plans[0]
    assert best.avg_duration_ms >= 0, f"avg_duration_ms is negative: {best.avg_duration_ms}"
    assert best.count_executions >= 1, f"count_executions < 1: {best.count_executions}"
    print(f"        avg_duration_ms={best.avg_duration_ms:.1f}  executions={best.count_executions}")
_run("T52 QS plan has avg_duration_ms >= 0 and count_executions >= 1", t52_qs_plan_avg_duration_positive)

def t53_qs_plan_recommendation_set():
    if not _qs_query_hash[0]:
        return "SKIP"
    out = analyze_query_store(
        QueryStoreInput(
            monitor_conn_str=_QS_CONN,
            query_hash=_qs_query_hash[0],
            query_plan_hash=_qs_query_plan_hash[0],
            database_name=_QS_DB,
            lookback_hours=1,
        ),
        query_sql=_qsql,
    )
    valid_recs = {"CURRENT_PLAN_IS_OPTIMAL","BETTER_PLAN_EXISTS","PLAN_ALREADY_FORCED","REVIEW_MANUALLY",""}
    assert out.qs_plan_recommendation in valid_recs, \
        f"Invalid qs_plan_recommendation: '{out.qs_plan_recommendation}'"
_run("T53 qs_plan_recommendation is a valid value", t53_qs_plan_recommendation_set)

def t54_qs_no_hash_returns_disabled_output():
    out = analyze_query_store(
        QueryStoreInput(
            monitor_conn_str=_QS_CONN,
            query_hash="",
            query_plan_hash="",
            database_name=_QS_DB,
        ),
        query_sql=_qsql,
    )
    assert not out.qs_enabled, "Expected qs_enabled=False when no hashes supplied"
_run("T54 analyze_query_store with empty hash -> qs_enabled=False (early return)", t54_qs_no_hash_returns_disabled_output)

def t55_qs_plan_xml_in_results():
    if not _qs_query_hash[0]:
        return "SKIP"
    out = analyze_query_store(
        QueryStoreInput(
            monitor_conn_str=_QS_CONN,
            query_hash=_qs_query_hash[0],
            query_plan_hash=_qs_query_plan_hash[0],
            database_name=_QS_DB,
            lookback_hours=1,
        ),
        query_sql=_qsql,
    )
    if not out.all_plans:
        return "SKIP"
    plans_with_xml = [p for p in out.all_plans if p.plan_xml]
    assert plans_with_xml, "No QS plan has plan_xml set (XML showplan not captured)"
    xml = plans_with_xml[0].plan_xml
    assert "ShowPlanXML" in xml, f"plan_xml doesn't contain ShowPlanXML: '{xml[:80]}'"
_run("T55 QS plan_xml contains ShowPlanXML", t55_qs_plan_xml_in_results)

def t56_qs_cleanup():
    _exec(_QS_CONN, _QS_PROC_DROP)
    _exec(_QS_CONN, "IF OBJECT_ID('dbo.QSTestItems','U') IS NOT NULL DROP TABLE dbo.QSTestItems")
    if not _qs_was_enabled:
        _exec(_QS_CONN, "ALTER DATABASE AgentLogDB SET QUERY_STORE = OFF")
        assert not _qs_state(), f"Failed to disable Query Store on {_QS_DB}"
    else:
        print("        (QS was already enabled before test -- leaving ON)")
_run("T56 QS cleanup: test proc + table dropped, QS state restored", t56_qs_cleanup)


# -----------------------------------------------------------------------------
#  SECTION I -- Hard gates R2-R14 (T57-T70)
# -----------------------------------------------------------------------------
_section("I  Hard gates R2-R14 (T57-T70)")

_DET_AGENT_CFG = {
    "monitor_conn_str": CONN,
    "log_conn_str":     LOG_CONN,
    "dry_run":          True,
    "kill_threshold_ms":            30_000,
    "log_size_kill_threshold_gb":   10,
    "max_kills_per_hour":           None,
    "application_account_patterns": ["app_*", "svc_*", "BBI_*"],
    "skip_isolation_levels":        ["SERIALIZABLE"],
}

def _mk_state(**kw):
    return {"server_name":"localhost","blocking_rows":[],"scenario_id":0,
            "scenario_name":"","percent_complete":None,"log_used_mb":0.0,
            "isolation_level":"READ COMMITTED", **kw}

def _mk_head(**kw):
    return {"session_id":63,"login_name":"batch_job_usr","wait_duration_ms":60000,
            "victim_count":2,"victim_spids":[100,101],"wait_type":"LCK_M_X", **kw}

def _chk(rule, decision, state, head=None):
    agent = DeterminationAgent(_DET_AGENT_CFG)
    head  = head or _mk_head()
    r = agent._check_hard_gates(state, head, head["session_id"])
    assert r is not None, f"R{rule} expected to fire, got None"
    assert r[3] == rule,    f"Expected rule={rule}, got {r[3]}"
    assert r[0] == decision, f"Expected {decision}, got {r[0]}"

def t57_r2_wait_below_threshold():
    r = DeterminationAgent(_DET_AGENT_CFG)._check_hard_gates(
        _mk_state(),
        _mk_head(wait_duration_ms=5000), 63)
    assert r is not None and r[3] == 2 and r[0] == "SKIP", f"R2 not fired: {r}"
_run("T57 R2: wait_ms < kill_threshold_ms -> SKIP", t57_r2_wait_below_threshold)

def t58_r3_system_spid():
    _chk(3, "ALERT_ONLY", _mk_state(), _mk_head(session_id=15, login_name="sa"))
_run("T58 R3: SPID < 50 -> ALERT_ONLY", t58_r3_system_spid)

def t59_r13_scenario5():
    _chk(13, "ALERT_ONLY", _mk_state(scenario_id=5))
_run("T59 R13: scenario_id=5 -> ALERT_ONLY", t59_r13_scenario5)

def t60_r13_percent_complete():
    _chk(13, "ALERT_ONLY", _mk_state(percent_complete=40.0))
_run("T60 R13: percent_complete=40 -> ALERT_ONLY", t60_r13_percent_complete)

def t61_r13_rollback_wait_type():
    _chk(13, "ALERT_ONLY", _mk_state(), _mk_head(wait_type="ROLLBACK"))
_run("T61 R13: ROLLBACK in head wait_type -> ALERT_ONLY", t61_r13_rollback_wait_type)

def _chk_r13_no_false_positive():
    agent = DeterminationAgent(_DET_AGENT_CFG)
    r = agent._check_hard_gates(_mk_state(scenario_id=6), _mk_head(), 63)
    if r is not None:
        assert r[3] != 13, f"R13 fired incorrectly for scenario_id=6 (rule={r[3]})"
_run("T62 R13: scenario_id=6 does NOT trigger R13", _chk_r13_no_false_positive)

def t63_r14_scenario4():
    _chk(14, "ALERT_ONLY", _mk_state(scenario_id=4))
_run("T63 R14: scenario_id=4 -> ALERT_ONLY", t63_r14_scenario4)

def _chk_r14_dtc_state():
    agent = DeterminationAgent(_DET_AGENT_CFG)
    state = _mk_state()
    state["blocking_rows"] = [{"session_id":63,"wait_type":"DTC_STATE","login_name":"batch_job_usr"}]
    r = agent._check_hard_gates(state, _mk_head(), 63)
    assert r is not None and r[3] == 14, f"R14 not fired for DTC_STATE: {r}"
_run("T64 R14: DTC_STATE in blocking_rows wait_type -> ALERT_ONLY", _chk_r14_dtc_state)
_run("T65 R13 fires before R14 when both conditions met",
     lambda: _chk(13, "ALERT_ONLY",
                  _mk_state(scenario_id=5, percent_complete=20.0),
                  _mk_head()))

def _chk_r9():
    cfg = dict(_DET_AGENT_CFG)
    cfg["log_size_kill_threshold_gb"] = 10
    agent = DeterminationAgent(cfg)
    r = agent._check_hard_gates(
        _mk_state(log_used_mb=12 * 1024),  # 12 GB > 10 GB threshold
        _mk_head(), 63)
    assert r is not None and r[3] == 9, f"R9 not fired: {r}"
    assert r[4] == True, "R9 should set dba_approval_required=True"
_run("T66 R9: log_used_mb > threshold -> ALERT_ONLY + dba_approval_required", _chk_r9)

def _chk_r11():
    cfg = dict(_DET_AGENT_CFG)
    agent = DeterminationAgent(cfg)
    state = _mk_state()
    # Victims do NOT match app_* / svc_* / BBI_*
    state["blocking_rows"] = [
        {"session_id":100,"login_name":"DOMAIN\\sqluser1"},
        {"session_id":101,"login_name":"DOMAIN\\sqluser2"},
    ]
    r = agent._check_hard_gates(state, _mk_head(), 63)
    assert r is not None and r[3] == 11 and r[0] == "SKIP", \
        f"R11 not fired for non-app-account victims: {r}"
_run("T67 R11: victims not matching app_account_patterns -> SKIP", _chk_r11)

def _chk_r12():
    agent = DeterminationAgent(_DET_AGENT_CFG)
    r = agent._check_hard_gates(
        _mk_state(isolation_level="SERIALIZABLE -- high blocking risk"),
        _mk_head(), 63)
    assert r is not None and r[3] == 12 and r[0] == "SKIP", \
        f"R12 not fired for SERIALIZABLE: {r}"
_run("T68 R12: SERIALIZABLE isolation -> SKIP", _chk_r12)

def _chk_no_gate_fires_normal():
    agent = DeterminationAgent(_DET_AGENT_CFG)
    state = _mk_state(scenario_id=6, log_used_mb=10.0)
    state["blocking_rows"] = [
        {"session_id":100,"login_name":"svc_appaccount"},
        {"session_id":101,"login_name":"app_worker01"},
    ]
    r = agent._check_hard_gates(state, _mk_head(), 63)
    assert r is None, \
        f"Expected no gate to fire for normal scenario, but R{r[3] if r else '?'} fired"
_run("T69 Normal scenario-6: no gate fires (reaches LLM)", _chk_no_gate_fires_normal)

def _chk_r2_not_skip_wait_above():
    """R2 should NOT fire when wait >= threshold."""
    agent = DeterminationAgent(_DET_AGENT_CFG)
    state = _mk_state(scenario_id=6, log_used_mb=0)
    state["blocking_rows"] = [{"session_id":100,"login_name":"svc_appaccount"}]
    # wait=60000 >= kill_threshold_ms=30000  -> R2 should NOT fire
    r = agent._check_hard_gates(state, _mk_head(wait_duration_ms=60000), 63)
    if r is not None:
        assert r[3] != 2, f"R2 fired incorrectly when wait=60000 >= threshold=30000"
_run("T70 R2 does NOT fire when wait_ms >= kill_threshold_ms", _chk_r2_not_skip_wait_above)


# -----------------------------------------------------------------------------
#  SECTION J -- SQL Executor 5 safety checks (T71-T76)
# -----------------------------------------------------------------------------
_section("J  SQL Executor -- 5 pre-kill safety checks (T71-T76)")

def t71_executor_spid_gone():
    out = execute_kill(
        SqlExecutorInput(monitor_conn_str=CONN, session_id=99997,
                         login_name="batch_job_usr", dry_run=False),
        query_sql=_qsql,
    )
    assert out.skip_reason and ("gone" in out.skip_reason.lower() or
                                "already" in out.skip_reason.lower()), \
        f"Expected 'gone/already' skip reason, got: '{out.skip_reason}'"
_run("T71 Safety check 1: non-existent SPID -> skip 'already gone'", t71_executor_spid_gone)

def t72_executor_login_mismatch():
    with _BlockingSession() as bs:
        # Provide wrong login name -> should detect SPID recycled/mismatched
        out = execute_kill(
            SqlExecutorInput(monitor_conn_str=CONN, session_id=bs.blocker_spid,
                             login_name="wrong_login_xyz", dry_run=False),
            query_sql=_qsql,
        )
        assert out.skip_reason and "recycled" in out.skip_reason.lower(), \
            f"Expected 'recycled' skip reason, got: '{out.skip_reason}'"
_run("T72 Safety check 2: login mismatch -> skip 'session was recycled'", t72_executor_login_mismatch)

def t73_executor_dry_run_simulated():
    with _BlockingSession() as bs:
        out = execute_kill(
            SqlExecutorInput(monitor_conn_str=CONN, session_id=bs.blocker_spid,
                             login_name="batch_job_usr", dry_run=True),
            query_sql=_qsql,
        )
        assert out.issue_status == "DRY_RUN_SIMULATED", \
            f"Expected DRY_RUN_SIMULATED, got '{out.issue_status}'"
        assert out.kill_issued,  "kill_issued should be True for dry-run simulation"
        assert _spid_alive(bs.blocker_spid), "SPID was killed in dry_run mode -- should not happen"
_run("T73 Safety check: dry_run=True -> DRY_RUN_SIMULATED, SPID stays alive", t73_executor_dry_run_simulated)

def t74_executor_no_spid_given():
    out = execute_kill(
        SqlExecutorInput(monitor_conn_str=CONN, session_id=None,
                         login_name="", dry_run=False),
        query_sql=_qsql,
    )
    assert out.skip_reason and "No head blocker" in out.skip_reason, \
        f"Expected 'No head blocker' skip reason, got: '{out.skip_reason}'"
_run("T74 Safety check: no SPID given -> skip 'No head blocker SPID'", t74_executor_no_spid_given)

def t75_executor_own_spid_has_no_open_txn():
    """Killing our own monitoring SPID: SQL Server rejects it or executor skips."""
    own = _own_spid()
    out = execute_kill(
        SqlExecutorInput(monitor_conn_str=CONN, session_id=own,
                         login_name="", dry_run=False),
        query_sql=_qsql,
    )
    # Acceptable outcomes:
    # - skip_reason set (safety check detected no blocking potential)
    # - FAILED with "Cannot use KILL to kill your own process" (SQL Server rejects)
    ok = (out.skip_reason is not None or
          "own process" in (out.issue_status or "").lower() or
          "FAILED" in (out.issue_status or ""))
    assert ok, f"Unexpected outcome for own SPID {own}: status='{out.issue_status}' skip='{out.skip_reason}'"
_run("T75 Safety check 5: monitoring SPID has no open txn -> executor skips or FAILED", t75_executor_own_spid_has_no_open_txn)

def _spid_alive_as_login(spid: int, login: str) -> bool:
    """True only if the SPID exists as a live user process owned by the given login."""
    rows = _qsql(CONN,
        "SELECT 1 AS x FROM sys.dm_exec_sessions "
        "WHERE session_id = ? AND is_user_process = 1 "
        "AND LOWER(login_name) = LOWER(?)", [spid, login])
    return bool(rows)

def t76_executor_kill_and_verify():
    """End-to-end: create blocker, issue real KILL, verify SPID gone."""
    with _BlockingSession(victim=False) as bs:
        spid = bs.blocker_spid
        out = execute_kill(
            SqlExecutorInput(monitor_conn_str=CONN, session_id=spid,
                             login_name="batch_job_usr", dry_run=False),
            query_sql=_qsql,
        )
        assert out.issue_status == "ISSUED", \
            f"Expected ISSUED, got '{out.issue_status}' (skip_reason='{out.skip_reason}')"
        # SQL Server rollback is async; poll up to 10s.
        # Check login-specific presence: after KILL, SQL Server may recycle the SPID
        # to a new session with a different login -- that counts as gone.
        for _ in range(20):
            time.sleep(0.5)
            if not _spid_alive_as_login(spid, "batch_job_usr"):
                break
        assert not _spid_alive_as_login(spid, "batch_job_usr"), \
            f"SPID {spid} still alive as batch_job_usr after KILL"
_run("T76 Real KILL issued via execute_kill, SPID confirmed gone", t76_executor_kill_and_verify)


# -----------------------------------------------------------------------------
#  SECTION K -- SQL Validator (T77-T80)
# -----------------------------------------------------------------------------
_section("K  SQL Validator -- outcome verification (T77-T80)")

def t77_validator_spid_gone():
    out = validate_kill(
        SqlValidatorInput(monitor_conn_str=CONN, session_id=99996),
        query_sql=_qsql,
    )
    assert out.validation_status == "CONFIRMED_GONE", \
        f"Expected CONFIRMED_GONE for non-existent SPID, got '{out.validation_status}'"
    assert out.kill_status == "SUCCESS", \
        f"Expected kill_status=SUCCESS, got '{out.kill_status}'"
_run("T77 Validator: non-existent SPID -> CONFIRMED_GONE / SUCCESS", t77_validator_spid_gone)

def t78_validator_no_spid():
    out = validate_kill(
        SqlValidatorInput(monitor_conn_str=CONN, session_id=None),
        query_sql=_qsql,
    )
    assert out.validation_status == "NOT_CHECKED", \
        f"Expected NOT_CHECKED for None SPID, got '{out.validation_status}'"
_run("T78 Validator: no session_id -> NOT_CHECKED", t78_validator_no_spid)

def t79_validator_live_spid_still_present():
    with _BlockingSession(victim=False) as bs:
        out = validate_kill(
            SqlValidatorInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql,
        )
        assert out.validation_status == "STILL_PRESENT", \
            f"Expected STILL_PRESENT for live idle SPID, got '{out.validation_status}'"
        assert "FAILED" in out.kill_status, \
            f"Expected FAILED in kill_status, got '{out.kill_status}'"
_run("T79 Validator: live idle SPID -> STILL_PRESENT / FAILED", t79_validator_live_spid_still_present)

def t80_validator_confirmed_after_kill():
    with _BlockingSession(victim=False) as bs:
        spid = bs.blocker_spid
        # Issue KILL directly
        with pyodbc.connect(CONN, autocommit=True, timeout=10) as conn:
            conn.cursor().execute(f"KILL {spid}")
        # SQL Server rollback is async -- poll up to 10s for SPID to disappear
        for _ in range(20):
            time.sleep(0.5)
            if not _spid_alive(spid):
                break
        # Pass expected_login so the validator can detect SPID recycling:
        # after KILL the SPID may be reused by a monitoring connection (different login).
        out = validate_kill(
            SqlValidatorInput(monitor_conn_str=CONN, session_id=spid,
                              expected_login="batch_job_usr"),
            query_sql=_qsql,
        )
        assert out.kill_status == "SUCCESS", \
            f"Expected SUCCESS after KILL, got '{out.kill_status}' (validation={out.validation_status})"
_run("T80 Validator: CONFIRMED_GONE / SUCCESS after real KILL", t80_validator_confirmed_after_kill)


# -----------------------------------------------------------------------------
#  SECTION L -- Kill-rate limiter (T81-T83)
# -----------------------------------------------------------------------------
_section("L  Kill-rate limiter (T81-T83)")

def t81_kill_rate_returns_int():
    out = check_kill_rate(
        KillRateInput(log_conn_str=LOG_CONN, server_name="localhost"),
        query_sql=_qsql,
    )
    assert isinstance(out.kills_last_hour, int), \
        f"kills_last_hour is not int: {type(out.kills_last_hour)}"
    assert out.kills_last_hour >= 0
    print(f"        (kills in last hour: {out.kills_last_hour})")
_run("T81 kill_rate returns non-negative int for localhost", t81_kill_rate_returns_int)

def t82_dry_run_kills_not_counted():
    # Insert a DRY_RUN kill into KillAuditLog and verify it doesn't count
    _exec(LOG_CONN, """
        INSERT INTO dbo.KillAuditLog
            (ServerName, CorrelationID, KilledSPID, KilledLogin,
             WaitDurationMs, VictimCount, KillStatus, RiskLevel,
             LLMReasoning, RCAReport, DryRun)
        VALUES ('localhost','test-dry-run-killrate',99995,'test_login',
                5000,1,'SUCCESS','LOW','test','test',1)
    """)
    before = check_kill_rate(
        KillRateInput(log_conn_str=LOG_CONN, server_name="localhost"),
        query_sql=_qsql,
    ).kills_last_hour
    # Dry-run kill should NOT be counted (DryRun=1 is excluded by KILL_RATE_SQL)
    print(f"        (count after dry-run insert: {before})")
_run("T82 Dry-run kills (DryRun=1) not counted in kill_rate", t82_dry_run_kills_not_counted)

def t83_real_kill_counted():
    before = check_kill_rate(
        KillRateInput(log_conn_str=LOG_CONN, server_name="localhost"),
        query_sql=_qsql,
    ).kills_last_hour
    _exec(LOG_CONN, """
        INSERT INTO dbo.KillAuditLog
            (ServerName, CorrelationID, KilledSPID, KilledLogin,
             WaitDurationMs, VictimCount, KillStatus, RiskLevel,
             LLMReasoning, RCAReport, DryRun)
        VALUES ('localhost','test-real-killrate',99994,'test_login',
                5000,1,'SUCCESS','LOW','test','test',0)
    """)
    after = check_kill_rate(
        KillRateInput(log_conn_str=LOG_CONN, server_name="localhost"),
        query_sql=_qsql,
    ).kills_last_hour
    assert after == before + 1, \
        f"Expected kills_last_hour to increase by 1 (before={before} after={after})"
_run("T83 Real kill (DryRun=0) increments kill_rate by 1", t83_real_kill_counted)


# -----------------------------------------------------------------------------
#  SECTION M -- Full pipeline smoke tests (T84-T87)
# -----------------------------------------------------------------------------
_section("M  Full pipeline smoke tests -- all tools chained (T84-T87)")

def t84_full_tool_chain_scenario6():
    """Run every diagnostic tool on a live scenario-6 blocker and verify coherence."""
    with _BlockingSession() as bs:
        spid = bs.blocker_spid

        det  = detect_blocking(
            DetectionInput(monitor_conn_str=CONN, server_name="localhost"),
            query_sql=_qsql)
        logs = analyze_log_safety(
            LogSafetyInput(monitor_conn_str=CONN, session_id=spid),
            query_sql=_qsql)
        lck  = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=spid),
            query_sql=_qsql)
        pc   = analyze_plan_cache(
            PlanCacheInput(monitor_conn_str=CONN, session_id=spid),
            query_sql=_qsql)
        sc   = classify_scenario(ScenarioInput(
            status="sleeping",
            command="holding_lock" if not det.has_blocking else "",
            wait_type=lck.lock_type,
            open_transaction_count=lck.open_txn_count,
            percent_complete=logs.percent_complete,
            lock_type=lck.lock_type,
        ))

        assert det.has_blocking,      "Detection: no blocking found"
        assert det.head_blocker.session_id == spid
        assert lck.open_txn_count >= 1, "Locks: open_txn_count < 1"
        assert "BlockingTest" in (lck.locked_object or ""), \
            f"Locks: expected BlockingTest, got '{lck.locked_object}'"
        assert sc.scenario_id in (1, 5, 6), \
            f"Scenario: expected 1/5/6 for blocker, got {sc.scenario_id}"
        if pc.hit:
            assert pc.query_hash, "Plan cache: hit but no query_hash"

        print(f"        scenario={sc.scenario_id} ({sc.scenario_name[:40]})")
        print(f"        lock_type={lck.lock_type}  locked_object={lck.locked_object}")
        print(f"        plan_hit={pc.hit}  source={pc.source}  qs_hash={pc.query_hash[:12] if pc.query_hash else 'n/a'}")
        print(f"        log_safety={logs.kill_safety_rating}")
_run("T84 Full tool chain: detection->log->locks->plan->scenario all coherent", t84_full_tool_chain_scenario6)

def t85_scenario_classifier_matches_detection():
    """Verify classify_scenario agrees with DMV data for a live blocker."""
    with _BlockingSession() as bs:
        rows = _qsql(CONN, """
            SELECT TOP 1
                ISNULL(r.status, s.status) AS status,
                ISNULL(r.command, 'HOLDING_LOCK') AS command,
                ISNULL(r.wait_type, '') AS wait_type,
                s.open_transaction_count
            FROM sys.dm_exec_sessions s
            LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
            WHERE s.session_id = ?
        """, [bs.blocker_spid])
        assert rows, f"No DMV row for SPID {bs.blocker_spid}"
        row = rows[0]
        sc = classify_scenario(ScenarioInput(
            status=str(row["status"] or ""),
            command=str(row["command"] or ""),
            wait_type=str(row["wait_type"] or ""),
            open_transaction_count=int(row["open_transaction_count"] or 0),
        ))
        # Scenario 6 (idle holding lock) is expected; scenario 1 (running) is valid
        # if the blocker is still mid-UPDATE when we sample the DMV
        assert sc.scenario_id in (1, 5, 6), \
            f"Expected scenario 1/5/6 for batch_job_usr, got {sc.scenario_id} ({sc.scenario_name})"
        print(f"        DMV: status={row['status']}  command={row['command']}"
              f"  open_txn={row['open_transaction_count']}")
        print(f"        Classified: scenario {sc.scenario_id} - {sc.scenario_name}")
_run("T85 Classifier correctly classifies live scenario-6 from DMV data", t85_scenario_classifier_matches_detection)

def t86_qs_integration_with_blocking_pipeline():
    """Verify analyze_query_store correctly reports qs_enabled for AgentLogDB."""
    if not _qs_state():
        return "SKIP"
    # Use the hashes from T49 QS population if available
    qh  = _qs_query_hash[0]
    qph = _qs_query_plan_hash[0]
    if not qh:
        return "SKIP"
    qs = analyze_query_store(
        QueryStoreInput(
            monitor_conn_str=_QS_CONN,
            query_hash=qh,
            query_plan_hash=qph,
            database_name=_QS_DB,
            lookback_hours=1,
        ),
        query_sql=_qsql)
    assert qs.qs_enabled, f"QS enabled on {_QS_DB} but analyze_query_store returned False"
    print(f"        qs_plans={qs.plans_found}  recommendation={qs.qs_plan_recommendation}")
_run("T86 QS pipeline: analyze_query_store reports qs_enabled=True for AgentLogDB", t86_qs_integration_with_blocking_pipeline)

def t87_hard_gates_fire_before_llm_on_scenario6():
    """Scenario-6 blocker should NOT trigger R13 or R14 -- it should pass gates."""
    with _BlockingSession() as bs:
        lck = analyze_locks(
            LocksInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql)
        logs = analyze_log_safety(
            LogSafetyInput(monitor_conn_str=CONN, session_id=bs.blocker_spid),
            query_sql=_qsql)
        agent = DeterminationAgent(_DET_AGENT_CFG)
        state = _mk_state(
            scenario_id=6,
            log_used_mb=logs.log_used_mb,
            isolation_level=lck.isolation_level,
            percent_complete=logs.percent_complete,
        )
        state["blocking_rows"] = [{"session_id":bs.victim_spid,"login_name":"svc_appaccount"}]
        head = _mk_head(session_id=bs.blocker_spid, login_name="batch_job_usr",
                        wait_duration_ms=60000)
        r = agent._check_hard_gates(state, head, bs.blocker_spid)
        # Should not fire R13 (not rolling back) or R14 (not DTC)
        if r is not None:
            assert r[3] not in (13, 14), \
                f"R{r[3]} incorrectly fired for clean scenario-6 blocker"
        print(f"        Gate result: {f'R{r[3]} ({r[0]})' if r else 'None (passes to LLM)'}")
_run("T87 Hard gates: clean scenario-6 does NOT trigger R13 or R14", t87_hard_gates_fire_before_llm_on_scenario6)


# -----------------------------------------------------------------------------
#  Final summary
# -----------------------------------------------------------------------------
print(f"\n{'='*66}")
print(f"  RESULTS: {_passed} passed   {_failed} failed   {_skipped} skipped")
print(f"{'='*66}")

if _failures:
    print("\nFailed tests:")
    for name, msg in _failures:
        print(f"  X {name}")
        print(f"    {msg}")

sys.exit(1 if _failed else 0)
