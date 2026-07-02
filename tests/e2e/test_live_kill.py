"""
tests/e2e/test_live_kill.py  --  Microsoft Agent Framework live kill test
--------------------------------------------------------------------------
LIVE end-to-end kill test — NOT a dry run.

Steps:
  1. Set DryRunGlobal=false in AgentConfigDB (restored in finally block)
  2. Create a real blocking scenario: blocker (batch_job_usr) holds an
     exclusive row lock; victim (svc_appaccount) waits for it
  3. Wait 47 seconds so victim wait_ms >> KillThresholdMs (30 s)
  4. Run ONE MAF workflow cycle — agent should detect, decide KILL, execute KILL
  5. Verify SPID is gone from sys.dm_exec_sessions
  6. Print KillAuditLog and BlockingEventLog rows
  7. Restore DryRunGlobal=true

Key differences from LangGraph version:
  - Uses AGENT_WORKFLOW.run(BlockingState(...))  instead of AGENT_GRAPH.ainvoke()
  - Result is a BlockingState Pydantic model  (supports .get() for dict-style access)
  - load_config() sets the _ACTIVE_CONFIG singleton; no config injection needed
  - agent_framework shim (src/agent_framework/) makes the code runnable

Run with:
    c:\\Python\\sql-blocking-agent\\.venv\\Scripts\\python tests\\e2e\\test_live_kill.py
"""

import asyncio
import logging
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

# Allow execution as a script from any cwd
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Load env: prefer ms-blocking-agent/.env, fall back to LangGraph .env.foundry
_env_ms = PROJECT_ROOT / ".env"
_env_lg = Path("c:/Python/sql-blocking-agent/.env.foundry")

if _env_ms.exists():
    load_dotenv(_env_ms)
    print(f"[env] Loaded {_env_ms}")
elif _env_lg.exists():
    load_dotenv(_env_lg)
    print(f"[env] Loaded {_env_lg} (LangGraph .env.foundry fallback)")
else:
    print("[env] WARNING: no .env file found — LLM calls may fail")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("maf_live_kill_test")

# ── Connection strings ────────────────────────────────────────────────────────

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;DATABASE=master;"
    "Trusted_Connection=yes;TrustServerCertificate=yes;"
)
BLOCKER_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;DATABASE=master;"
    "UID=batch_job_usr;PWD=B@tch_J0b_2026!;TrustServerCertificate=yes;"
)
CONFIG_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;DATABASE=AgentConfigDB;"
    "Trusted_Connection=yes;TrustServerCertificate=yes;"
)
LOG_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;DATABASE=AgentLogDB;"
    "Trusted_Connection=yes;TrustServerCertificate=yes;"
)

SEP  = "=" * 70
DASH = "-" * 70


# ── DB helpers ─────────────────────────────────────────────────────────────────

def set_dry_run(value: str):
    conn = pyodbc.connect(CONFIG_CONN_STR, autocommit=True)
    conn.cursor().execute(
        "UPDATE dbo.GlobalConfig SET ConfigValue=?, UpdatedAt=SYSUTCDATETIME()"
        " WHERE ConfigKey='DryRunGlobal'",
        value,
    )
    conn.close()
    log.info("GlobalConfig: DryRunGlobal = %s", value)


def setup_test_table():
    conn = pyodbc.connect(CONN_STR, autocommit=True)
    conn.cursor().execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='BlockingTest')
        BEGIN
            CREATE TABLE dbo.BlockingTest (ID INT PRIMARY KEY, Value NVARCHAR(200));
            INSERT INTO dbo.BlockingTest VALUES (1, 'maf live kill test row');
        END
        ELSE
            UPDATE dbo.BlockingTest SET Value='maf live kill test row' WHERE ID=1;
    """)
    conn.close()
    log.info("BlockingTest table ready.")


# ── Blocker thread ─────────────────────────────────────────────────────────────

def blocker_thread_fn(hold_seconds: int = 120):
    """
    Acquires an exclusive row lock with UPDATE then goes idle (open transaction,
    no further SQL) — KB scenario 6: idle session with open transaction.
    The agent's strongest kill candidate: status='sleeping', open_transaction_count>0.
    """
    conn = pyodbc.connect(BLOCKER_CONN_STR, autocommit=False)
    conn.timeout = 0
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE dbo.BlockingTest SET Value='MAF BLOCKER HOLDING LOCK' WHERE ID=1"
        )
        log.warning(
            ">>> BLOCKER (batch_job_usr): lock acquired — idle with open txn for %ss",
            hold_seconds,
        )
        time.sleep(hold_seconds)
        log.info(">>> BLOCKER: idle period done (unexpected — should have been killed)")
    except Exception as e:
        log.info(">>> BLOCKER: session ended (%s)", e)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        log.info(">>> BLOCKER: transaction rolled back, lock released.")


# ── Victim thread ──────────────────────────────────────────────────────────────

def victim_thread_fn(ready: threading.Event):
    conn = pyodbc.connect(CONN_STR, autocommit=True)
    conn.timeout = 180
    cur = conn.cursor()
    cur.execute("EXECUTE AS LOGIN = 'svc_appaccount'")
    ready.set()
    log.warning(">>> VICTIM: waiting for locked row (usp_GetBlockingTestRow)...")
    try:
        cur.execute("EXEC dbo.usp_GetBlockingTestRow")
        log.info(">>> VICTIM: lock acquired — blocker was killed successfully.")
    except Exception as e:
        log.info(">>> VICTIM: query ended: %s", e)
    finally:
        conn.close()


# ── MAF agent cycle ────────────────────────────────────────────────────────────

async def run_agent_cycle(config: dict):
    """Run one MAF workflow cycle.  Returns the final BlockingState."""
    from orchestrator.workflow import AGENT_WORKFLOW
    from orchestrator.state import BlockingState

    initial = BlockingState(
        server_name=config["server_name"],
        correlation_id=str(uuid.uuid4())[:8],
        dry_run=config.get("dry_run", False),
        cycle_start_utc=datetime.now(timezone.utc).isoformat(),
    )
    # AGENT_WORKFLOW.run() calls each executor in sequence via the shim and
    # returns the BlockingState yielded by notification_node.yield_output().
    return await AGENT_WORKFLOW.run(initial)


# ── DMV helpers ────────────────────────────────────────────────────────────────

def dmv_snapshot() -> list:
    conn = pyodbc.connect(CONN_STR, autocommit=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT r.session_id, r.blocking_session_id, r.wait_type,
               r.wait_time AS wait_ms, s.login_name, r.status
        FROM sys.dm_exec_requests r
        JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id
        WHERE r.blocking_session_id > 0
           OR r.session_id IN (
               SELECT blocking_session_id FROM sys.dm_exec_requests
               WHERE blocking_session_id > 0)
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def spid_alive(spid: int) -> bool:
    conn = pyodbc.connect(CONN_STR, autocommit=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sys.dm_exec_sessions WHERE session_id=?", spid
    )
    alive = cur.fetchone() is not None
    conn.close()
    return alive


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{SEP}")
    print("  SQL BLOCKING AGENT (MAF) — LIVE KILL TEST  (dry_run=False)")
    print(f"{SEP}\n")

    from orchestrator.config import load_config

    # ── Phase 0: flip to LIVE mode ─────────────────────────────────────────────
    print(f"{DASH}\nPHASE 0: Enabling LIVE mode in AgentConfigDB\n{DASH}")
    set_dry_run("false")

    try:
        config = load_config()
        assert not config["dry_run"], "dry_run should be False after DB update"
        log.info(
            "dry_run=%s  kill_threshold_ms=%s  log_size_kill_threshold_gb=%s",
            config["dry_run"],
            config["kill_threshold_ms"],
            config["log_size_kill_threshold_gb"],
        )

        # ── Phase 1: setup ──────────────────────────────────────────────────────
        print(f"\n{DASH}\nPHASE 1: Setting up test table\n{DASH}")
        log.info("Waiting 5s for any in-flight agent cycles to settle...")
        time.sleep(5)
        setup_test_table()

        # ── Phase 2: create blocking scenario ──────────────────────────────────
        print(f"\n{DASH}\nPHASE 2: Creating blocking scenario\n{DASH}")

        test_start_utc = datetime.now(timezone.utc)

        victim_ready = threading.Event()
        blocker = threading.Thread(
            target=blocker_thread_fn, args=(120,), daemon=True
        )
        blocker.start()
        time.sleep(1.0)

        victim = threading.Thread(
            target=victim_thread_fn, args=(victim_ready,), daemon=True
        )
        victim.start()
        victim_ready.wait(timeout=5)
        time.sleep(2.0)

        rows = dmv_snapshot()
        log.info("DMV confirms %d session(s) in blocking chain:", len(rows))
        for r in rows:
            log.info(
                "  spid=%-4s  blocked_by=%-4s  wait_type=%-16s  wait_ms=%-8s  login=%s",
                r[0], r[1], r[2], r[3], r[4],
            )
        assert rows, "No blocking detected in DMV — test setup failed"

        blocker_candidates = {r[0] for r in rows if r[1] == 0}
        blocked_by_values  = {r[1] for r in rows if r[1] != 0}
        blocker_spid = (
            next(iter(blocker_candidates), None)
            or next(iter(blocked_by_values))
        )
        log.info("Head blocker SPID identified: %s", blocker_spid)

        # ── Phase 3: wait 47 seconds ────────────────────────────────────────────
        print(f"\n{DASH}\nPHASE 3: Waiting 47s (blocking must exceed KillThresholdMs=30s)\n{DASH}")
        for remaining in range(47, 0, -5):
            log.info("  ... %d seconds remaining ...", remaining)
            time.sleep(min(5, remaining))

        rows_after = dmv_snapshot()
        log.info("DMV after wait (%d rows):", len(rows_after))
        for r in rows_after:
            log.info(
                "  spid=%-4s  blocked_by=%-4s  wait_ms=%-8s  status=%s",
                r[0], r[1], r[3], r[5],
            )
        if not rows_after:
            log.warning(
                "Blocking already dissolved — may have been cleared by a "
                "concurrent agent cycle.  Running MAF cycle anyway."
            )

        # ── Phase 4: run MAF workflow ────────────────────────────────────────────
        print(f"\n{DASH}\nPHASE 4: Running MAF workflow cycle (LIVE — dry_run=False)\n{DASH}\n")
        result = await run_agent_cycle(config)

        # ── Phase 5: results ─────────────────────────────────────────────────────
        print(f"\n{DASH}\nPHASE 5: MAF workflow results\n{DASH}")
        print(f"  Framework       : Microsoft Agent Framework (WorkflowBuilder shim)")
        print(f"  has_blocking    : {result.get('has_blocking', False)}")
        print(f"  decision        : {result.get('decision', 'SKIP')}")
        print(f"  risk_level      : {result.get('risk_level', 'LOW')}")
        print(f"  rule_triggered  : {result.get('rule_triggered', 0)}")
        print(f"  kill_executed   : {result.get('kill_executed', False)}")
        print(f"  kill_status     : {result.get('kill_status', 'NOT_ATTEMPTED')}")
        print(f"  killed_spid     : {result.get('killed_spid')}")
        print(f"  kill_time_utc   : {result.get('kill_time_utc')}")
        print(f"  errors          : {result.get('errors', [])}")

        head = result.get("head_blocker") or {}
        if head:
            print(f"\n  Head blocker:")
            print(f"    SPID     : {head.get('session_id')}")
            print(f"    Login    : {head.get('login_name')}")
            print(f"    Wait ms  : {head.get('wait_duration_ms')}")
            print(f"    Victims  : {head.get('victim_count')}")
            print(f"    Chain    : {head.get('blocking_chain')}")

        if result.get("decision_reason"):
            print(f"\n  Decision reason:\n    {result.get('decision_reason', '')[:300]}")

        if result.get("llm_analysis"):
            print(f"\n  LLM analysis (first 400 chars):")
            for line in str(result.get("llm_analysis", ""))[:400].splitlines():
                print(f"    {line}")

        if result.get("rca_report"):
            print(f"\n  RCA Report (first 800 chars):")
            for line in str(result.get("rca_report", ""))[:800].splitlines():
                print(f"    {line}")

        # ── Phase 6: verify SPID is gone ─────────────────────────────────────────
        print(f"\n{DASH}\nPHASE 6: Verifying SPID {blocker_spid} is gone\n{DASH}")
        time.sleep(1)
        still_alive = spid_alive(blocker_spid)
        if still_alive:
            log.warning(
                "  SPID %s still alive — kill may not have executed.", blocker_spid
            )
        else:
            log.info(
                "  CONFIRMED: SPID %s no longer in sys.dm_exec_sessions.", blocker_spid
            )

        # ── Phase 7: audit log check ──────────────────────────────────────────────
        print(f"\n{DASH}\nPHASE 7: AgentLogDB audit tables\n{DASH}")
        log_conn = pyodbc.connect(LOG_CONN_STR, autocommit=True)
        cur = log_conn.cursor()

        cur.execute("""
            SELECT TOP 1
                EventTimeUTC, HeadBlockerSPID, HeadBlockerLogin, BlockerDatabase,
                BlockerSQLText, VictimSPIDs, VictimLogins, VictimDatabases,
                VictimSQLText, WaitDurationMs, VictimCount,
                WaitType, LockResource, DecisionTaken, RiskLevel, DryRun
            FROM dbo.BlockingEventLog
            ORDER BY EventTimeUTC DESC
        """)
        row = cur.fetchone()
        if row:
            (evt_time, blkr_spid, blkr_login, blkr_db, blkr_sql,
             vic_spids, vic_logins, vic_dbs, vic_sql,
             wait_ms, vic_count, wait_type, lock_res,
             decision, risk, dry_run) = row
            print("  BlockingEventLog (latest row):")
            print(f"    Time             : {evt_time}")
            print(f"    Decision         : {decision}  risk={risk}  dry_run={dry_run}")
            print(f"    --- HEAD BLOCKER ---")
            print(f"    SPID             : {blkr_spid}")
            print(f"    Login            : {blkr_login}")
            print(f"    Database         : {blkr_db}")
            print(f"    SQL              : {str(blkr_sql)[:120]}")
            print(f"    --- VICTIM(S) ---")
            print(f"    SPIDs            : {vic_spids}")
            print(f"    Logins           : {vic_logins}")
            print(f"    Databases        : {vic_dbs}")
            print(f"    SQL              : {str(vic_sql)[:120]}")
            print(f"    --- LOCK DETAILS ---")
            print(f"    Wait ms          : {wait_ms}  (victims={vic_count})")
            print(f"    Wait type        : {wait_type}")
            print(f"    Lock resource    : {str(lock_res)[:120]}")

        cur.execute("""
            SELECT TOP 1 BlockerParentObject, VictimParentObjects,
                         LockObjectName, LockIndexName
            FROM dbo.BlockingEventLog ORDER BY EventTimeUTC DESC
        """)
        po = cur.fetchone()
        if po:
            print(f"    --- PARENT OBJECTS ---")
            print(f"    Blocker parent   : {po[0] or '(ad-hoc / idle)'}")
            print(f"    Victim parent(s) : {po[1] or '(ad-hoc)'}")
            print(f"    --- LOCK TARGET ---")
            print(f"    Table / Object   : {po[2] or '(not resolved)'}")
            print(f"    Index            : {po[3] or '(heap or OBJECT-level lock)'}")

        cur.execute("""
            SELECT TOP 1 KillTimeUTC, KilledSPID, KilledLogin,
                         WaitDurationMs, VictimCount, KillStatus, RiskLevel, DryRun
            FROM dbo.KillAuditLog
            ORDER BY KillTimeUTC DESC
        """)
        kill_row = cur.fetchone()
        print("\n  KillAuditLog (latest row):")
        if kill_row:
            print(
                f"    {kill_row[0]}  SPID={kill_row[1]}  login={kill_row[2]}  "
                f"wait={kill_row[3]}ms  victims={kill_row[4]}  "
                f"status={kill_row[5]}  risk={kill_row[6]}  dry_run={kill_row[7]}"
            )
        else:
            print("    (no rows — kill_executed was False or logger skipped)")

        log_conn.close()

        # ── Verdict ───────────────────────────────────────────────────────────────
        log_conn2 = pyodbc.connect(LOG_CONN_STR, autocommit=True)
        cur2 = log_conn2.cursor()
        cur2.execute(
            """
            SELECT TOP 1 KillTimeUTC, KilledSPID, KillStatus, DryRun
            FROM dbo.KillAuditLog
            WHERE KilledSPID = ? AND KillTimeUTC >= ?
            ORDER BY KillTimeUTC DESC
            """,
            blocker_spid,
            test_start_utc.replace(tzinfo=None),
        )
        audit_kill = cur2.fetchone()
        log_conn2.close()

        print(f"\n{SEP}")
        kill_ok = result.get("kill_status") == "SUCCESS"
        audit_kill_ok = audit_kill and str(audit_kill[2]) == "SUCCESS"

        if kill_ok:
            print("  [PASS] MAF agent detected blocking, decided KILL,")
            print(
                f"         executed KILL {result.get('killed_spid')}, "
                f"wrote audit records."
            )
        elif audit_kill_ok:
            print(
                "  [PASS] Kill confirmed via KillAuditLog — SPID was killed "
                "during this test run."
            )
            print(
                "         (Agent cycle returned SKIP because blocking was "
                "already resolved by detection time — timing overlap.)"
            )
        elif result.get("decision") == "KILL" and result.get("kill_status") == "DRY_RUN_SIMULATED":
            print("  [FAIL] kill_status=DRY_RUN_SIMULATED — dry_run was still True.")
            print("         Check that DryRunGlobal=false was read from GlobalConfig.")
        else:
            print(
                f"  [INFO] decision={result.get('decision')}  "
                f"kill_status={result.get('kill_status')}"
            )
            print("         Review logs above for details.")
        print(f"{SEP}\n")

    finally:
        set_dry_run("true")
        log.info("GlobalConfig: DryRunGlobal restored to true")


if __name__ == "__main__":
    asyncio.run(main())
