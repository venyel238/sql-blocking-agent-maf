"""
tools/scenario.py -- Blocking scenario classifier (Microsoft KB)
--------------------------------------------------------------------
Pure, deterministic classifier that maps a blocking session's runtime
state onto the "Common blocking scenarios" table from:
https://learn.microsoft.com/en-us/troubleshoot/sql/database-engine/performance/understand-resolve-blocking

Used by the Analyzer Agent to tag the head blocker with one of the
six scenarios from the KB, which the Determination Agent then uses
as additional context for its kill/alert/skip decision and which the
RCA Agent uses to ground its root-cause narrative.

No SQL, no LLM -- pure lookup over fields already collected by
detection (status/command/wait_type) and tools/locks.py (lock_type,
open_txn_count).
"""

import logging
from typing import Optional

from pydantic import BaseModel

log = logging.getLogger("tools.scenario")

# Wait types associated with MSDTC / distributed transaction coordination
DTC_WAIT_TYPES = {"DTC_STATE", "PREEMPTIVE_TRANSIMPORT", "PREEMPTIVE_DTC_ENLIST"}
_DTC_WAIT_TYPES = DTC_WAIT_TYPES  # backward-compat alias


class ScenarioInput(BaseModel):
    status: str = ""               # sys.dm_exec_requests.status / dm_exec_sessions.status
    command: str = ""               # sys.dm_exec_requests.command (or 'HOLDING_LOCK' synthetic)
    wait_type: Optional[str] = None
    open_transaction_count: int = 0
    percent_complete: Optional[float] = None
    lock_type: str = ""             # from tools/locks.py LocksOutput.lock_type


class ScenarioOutput(BaseModel):
    scenario_id: int = 0
    scenario_name: str = ""
    scenario_guidance: str = ""
    kb_reference: str = (
        "https://learn.microsoft.com/en-us/troubleshoot/sql/database-engine/"
        "performance/understand-resolve-blocking"
    )


def classify_scenario(input: ScenarioInput) -> ScenarioOutput:
    """
    Map the head blocker's runtime state onto one of the six "Common
    blocking scenarios" in the Microsoft KB's blocking-analysis table.
    Checked in priority order (most specific / least killable first).
    """
    status = (input.status or "").lower()
    command = (input.command or "").lower()
    wait_type = (input.wait_type or "").upper()

    # Scenario 5 -- session is actively rolling back; cannot be killed again
    if "rollback" in status or "rollback" in command or (input.percent_complete or 0) > 0:
        return ScenarioOutput(
            scenario_id=5,
            scenario_name="Session in rollback",
            scenario_guidance=(
                "Session is already rolling back an aborted transaction -- it "
                "cannot be killed again. Wait for percent_complete to reach 100; "
                "alert only."
            ),
        )

    # Scenario 6 -- orphaned / idle session holding an open transaction
    if command == "holding_lock" or (status == "sleeping" and input.open_transaction_count > 0):
        return ScenarioOutput(
            scenario_id=6,
            scenario_name="Orphaned connection / idle session with open transaction",
            scenario_guidance=(
                "Session is sleeping with open_transaction_count > 0 -- the "
                "application most likely failed to COMMIT or ROLLBACK. "
                "Strong kill candidate once safety gates pass."
            ),
        )

    # Scenario 4 -- distributed transaction / MSDTC involvement
    if wait_type in _DTC_WAIT_TYPES or "DTC" in wait_type:
        return ScenarioOutput(
            scenario_id=4,
            scenario_name="Distributed transaction / MSDTC deadlock",
            scenario_guidance=(
                "Blocking involves the distributed transaction coordinator. "
                "May require resolution on the coordinator/other server. "
                "Alert only -- RCA needed before any kill."
            ),
        )

    # Scenario 3 -- client not draining the result set
    if wait_type == "ASYNC_NETWORK_IO":
        return ScenarioOutput(
            scenario_id=3,
            scenario_name="Slow client / partial fetch (ASYNC_NETWORK_IO)",
            scenario_guidance=(
                "Session is waiting on the client to consume rows. Investigate "
                "the application's fetch loop or network path; kill candidate "
                "if the wait persists across cycles."
            ),
        )

    # Scenario 2 -- table-level lock escalation, usually short-lived
    if input.lock_type == "OBJECT":
        return ScenarioOutput(
            scenario_id=2,
            scenario_name="Lock escalation (table-level lock)",
            scenario_guidance=(
                "OBJECT-level lock suggests lock escalation from a large DML "
                "batch rather than a stuck transaction. Prefer SKIP unless the "
                "wait is sustained across multiple cycles."
            ),
        )

    # Scenario 1 -- long-running active query
    if status in ("running", "suspended"):
        return ScenarioOutput(
            scenario_id=1,
            scenario_name="Long-running active query",
            scenario_guidance=(
                "Head blocker has an active request in progress. Evaluate "
                "whether the query itself can be tuned (missing index, stale "
                "plan, parameter sniffing) before considering a kill."
            ),
        )

    return ScenarioOutput(scenario_id=0, scenario_name="Unclassified", scenario_guidance="")
