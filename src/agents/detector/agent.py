"""
src/agents/detector/agent.py  --  Detection + Collector (tool executor)
-----------------------------------------------------------------------
Same logic as the LangGraph version (deterministic, no LLM). Runs blocking
detection AND all 5 diagnostic tools in one executor, then passes the
populated BlockingState to the next node via context.send_message().

Dependency order:
  1. detect_blocking()
  2. log_safety()      -- independent (needs SPID only)
  3. locks()           -- independent (needs SPID only)
  4. plan_cache()      -- needs SPID + victim_spids from step 1
  5. query_store()     -- needs query_hash/plan_hash from step 4
  6. classify_scenario()
"""

import logging
import time
from typing import Optional

from agent_framework import executor, WorkflowContext
from agents.base_agent import BaseAgent
from orchestrator.config import get_config
from orchestrator.state import BlockingState
from tools.detection import DetectionInput, detect_blocking
from tools.log_safety import LogSafetyInput, LogSafetyOutput, analyze_log_safety
from tools.plan_cache import PlanCacheInput, PlanCacheOutput, analyze_plan_cache
from tools.query_store import QueryStoreInput, QueryStoreOutput, analyze_query_store
from tools.locks import LocksInput, LocksOutput, analyze_locks
from tools.scenario import ScenarioInput, classify_scenario

log = logging.getLogger("agent.detection")

# ── In-memory dedup cache ──────────────────────────────────────────────────
_seen_blockers: dict[tuple[str, int, str], float] = {}
_DEDUP_TTL = 60
_cleanup_counter = 0


def _dedup_check(server: str, spid: int, login: str) -> bool:
    global _cleanup_counter
    key = (server, spid, login)
    now = time.monotonic()
    seen_at = _seen_blockers.get(key)
    if seen_at is not None and (now - seen_at) < _DEDUP_TTL:
        return True
    _seen_blockers[key] = now
    _cleanup_counter += 1
    if _cleanup_counter >= 100:
        _cleanup_counter = 0
        stale = [k for k, v in _seen_blockers.items() if (now - v) > _DEDUP_TTL * 2]
        for k in stale:
            del _seen_blockers[k]
    return False


def _build_qs_table(qs_info: QueryStoreOutput, lookback_hours: int) -> str:
    if not qs_info.qs_enabled:
        return ""
    if not qs_info.plans_found or not qs_info.all_plans:
        return ""

    parts = [
        f"Query Store is ENABLED. {qs_info.plans_found} plan(s) found "
        f"(lookback: {lookback_hours}h)."
    ]

    header = "| plan_id | avg_ms | stdev_ms | avg_reads | stdev_reads | execs | status |"
    sep    = "|---------|--------|----------|-----------|-------------|-------|--------|"
    parts.append("")
    parts.append(header)
    parts.append(sep)

    for p in qs_info.all_plans:
        status = p.plan_status or ""
        suffix = ""
        if qs_info.better_plan_exists and p.plan_id == qs_info.best_plan_id:
            suffix = " (best)"
        parts.append(
            f"| {p.plan_id:>7} | {p.avg_duration_ms:>6.0f} | {p.stdev_duration_ms:>8.0f} | "
            f"{p.avg_logical_io_reads:>9.0f} | {p.stdev_logical_io_reads:>10.0f} | "
            f"{p.count_executions:>5} | {status}{suffix} |"
        )

    if qs_info.better_plan_exists and qs_info.best_plan_id:
        current = next(
            (p for p in qs_info.all_plans if p.plan_status == "CURRENT_RUNNING_PLAN"),
            None,
        )
        best = next(
            (p for p in qs_info.all_plans if p.plan_id == qs_info.best_plan_id),
            None,
        )
        if current and best and best.avg_duration_ms > 0:
            ratio = current.avg_duration_ms / best.avg_duration_ms
            if best.avg_logical_io_reads > 0:
                io_ratio = current.avg_logical_io_reads / best.avg_logical_io_reads
                io_line = (
                    f"    {io_ratio:.0f}x fewer logical reads "
                    f"({best.avg_logical_io_reads:.0f} vs {current.avg_logical_io_reads:.0f})."
                )
            else:
                io_line = ""
            parts.append(
                f"\n>>> BETTER PLAN EXISTS: plan_id {best.plan_id} averages "
                f"{best.avg_duration_ms:.0f}ms vs current plan_id {current.plan_id} "
                f"at {current.avg_duration_ms:.0f}ms ({ratio:.1f}x faster)."
            )
            if io_line:
                parts.append(io_line)
        elif best:
            parts.append(
                f"\n>>> BETTER PLAN EXISTS: plan_id {best.plan_id} "
                f"averages {best.avg_duration_ms:.0f}ms."
            )
        parts.append(f"    Consider sp_query_store_force_plan {qs_info.best_plan_id}.")
        parts.append("")
        parts.append("Note: the best plan XML is NOT individually stored in state -- "
                     "compare its metrics against the current plan XML above.")

    return "\n".join(parts)


_PARALLEL_WAIT_TYPES = frozenset({"CXPACKET", "CXCONSUMER", "CXSYNC_PORT", "CXSYNC_CONSUMER"})


def _detect_parallel_signals(
    blocking_rows: list[dict],
    plan_xml: str | None,
) -> tuple[bool, str]:
    found: list[str] = []
    for row in blocking_rows:
        wt = str(row.get("wait_type") or "").upper()
        if wt in _PARALLEL_WAIT_TYPES and wt not in found:
            found.append(wt)

    xml_parallel = False
    if plan_xml:
        xml_lower = plan_xml.lower()
        if "<parallelism" in xml_lower or 'isparallel="1"' in xml_lower or 'parallel="1"' in xml_lower:
            xml_parallel = True

    detected = bool(found) or xml_parallel
    return detected, ",".join(found)


def _fallback_database_name(
    blocking_rows: list[dict], head_spid: int,
) -> Optional[str]:
    for row in blocking_rows:
        if row.get("session_id") == head_spid and row.get("database_name"):
            return row["database_name"]
    for row in blocking_rows:
        if row.get("database_name"):
            return row["database_name"]
    return None


@executor(id="detection")
async def detection_node(state: BlockingState, context: WorkflowContext) -> None:
    cfg = get_config()
    agent = BaseAgent(cfg)
    server = state.server_name
    errors: list[str] = list(state.errors)

    lookback_hours: int = cfg.get("plan_lookback_hours", 24)

    # ── 1. Blocking detection ─────────────────────────────────────────────
    detection_input = DetectionInput(
        server_name=server,
        monitor_conn_str=agent.monitor_conn_str,
    )
    result = detect_blocking(detection_input, query_sql=agent.query_sql)

    state.has_blocking = result.has_blocking
    state.blocking_rows = result.blocking_rows

    if not result.has_blocking:
        state.errors = errors + result.errors
        await context.send_message(state)
        return

    state.head_blocker = result.head_blocker.model_dump()
    head = result.head_blocker
    spid = head.session_id
    blocking_rows = result.blocking_rows

    # ── 2a. Dedup ─────────────────────────────────────────────────────────
    if _dedup_check(server, spid, head.login_name):
        log.info(
            "[%s] SPID %s (%s) already processed within %ds window "
            "-- skipping diagnostics + LLM + notification.",
            server, spid, head.login_name, _DEDUP_TTL,
        )
        state.has_blocking = False
        state.blocking_rows = result.blocking_rows
        state.errors = errors
        await context.send_message(state)
        return

    # ── 2. Log & rollback safety ──────────────────────────────────────────
    try:
        log_info = analyze_log_safety(
            LogSafetyInput(
                monitor_conn_str=agent.monitor_conn_str,
                session_id=spid,
                fallback_database_name=_fallback_database_name(blocking_rows, spid),
            ),
            agent.query_sql,
        )
    except Exception as e:
        log.error("[%s] log_safety tool crashed: %s", server, e)
        errors.append(f"log_safety_crashed: {e}")
        log_info = LogSafetyOutput()
    log.info("[%s] log_safety: rating=%s log_used_pct=%.1f db=%s",
             server, log_info.kill_safety_rating, log_info.log_used_pct,
             log_info.database_name)

    # ── 3. Locks & isolation level ────────────────────────────────────────
    try:
        lock_info = analyze_locks(
            LocksInput(monitor_conn_str=agent.monitor_conn_str, session_id=spid),
            agent.query_sql,
        )
    except Exception as e:
        log.error("[%s] locks tool crashed: %s", server, e)
        errors.append(f"locks_crashed: {e}")
        lock_info = LocksOutput()
    log.info("[%s] locks: lock_type=%s isolation=%s open_txn=%d",
             server, lock_info.lock_type or "N/A", lock_info.isolation_level or "N/A",
             lock_info.open_txn_count)

    # ── 4. Plan cache ──────────────────────────────────────────────────────
    try:
        plan_info = analyze_plan_cache(
            PlanCacheInput(
                monitor_conn_str=agent.monitor_conn_str,
                session_id=spid,
                lookback_hours=lookback_hours,
            ),
            agent.query_sql,
        )
    except Exception as e:
        log.error("[%s] plan_cache tool crashed: %s", server, e)
        errors.append(f"plan_cache_crashed: {e}")
        plan_info = PlanCacheOutput()
    log.info("[%s] plan_cache: hit=%s source=%s query_hash=%s plan_xml=%s",
             server, plan_info.hit, plan_info.source or "—",
             plan_info.query_hash, "yes" if plan_info.plan_xml else "no")

    # ── 5. Query Store ─────────────────────────────────────────────────────
    qs_db = log_info.database_name or ""
    try:
        qs_info = analyze_query_store(
            QueryStoreInput(
                monitor_conn_str=agent.monitor_conn_str,
                query_hash=plan_info.query_hash,
                query_plan_hash=plan_info.query_plan_hash,
                database_name=qs_db,
                lookback_hours=lookback_hours,
            ),
            agent.query_sql,
        )
    except Exception as e:
        log.error("[%s] query_store tool crashed: %s", server, e)
        errors.append(f"query_store_crashed: {e}")
        qs_info = QueryStoreOutput()
    log.info("[%s] query_store: enabled=%s plans_found=%d recommendation=%s",
             server, qs_info.qs_enabled, qs_info.plans_found,
             qs_info.qs_plan_recommendation or "N/A")

    qs_plan_table = _build_qs_table(qs_info, lookback_hours)

    # ── Choose best available plan XML ────────────────────────────────────
    blocker_plan_xml = qs_info.current_plan_xml or plan_info.plan_xml
    log.info("[%s] blocker_plan_xml: %s",
             server, "captured" if blocker_plan_xml else "unavailable")

    # ── Gap H: Parallel query detection ──────────────────────────────────
    parallel_detected, parallel_wait_types = _detect_parallel_signals(
        blocking_rows, blocker_plan_xml
    )
    if parallel_detected:
        log.info("[%s] Parallel query signals detected: wait_types=%s",
                 server, parallel_wait_types or "from_xml_only")

    # ── 6. Microsoft KB scenario classification ───────────────────────────
    head_row = next(
        (r for r in blocking_rows if r.get("session_id") == spid),
        {},
    )
    scenario_info = classify_scenario(
        ScenarioInput(
            status=str(head_row.get("status") or ""),
            command=str(head_row.get("command") or ""),
            wait_type=head.wait_type or head_row.get("wait_type"),
            open_transaction_count=lock_info.open_txn_count,
            percent_complete=log_info.percent_complete,
            lock_type=lock_info.lock_type,
        )
    )
    log.info("[%s] scenario: #%d %s", server, scenario_info.scenario_id,
             scenario_info.scenario_name)

    # ── Write all results to state ─────────────────────────────────────────
    state.log_used_pct           = log_info.log_used_pct
    state.log_used_mb            = log_info.log_used_mb
    state.kill_safety_rating     = log_info.kill_safety_rating
    state.estimated_rollback_sec = log_info.estimated_rollback_sec
    state.txn_age_seconds        = log_info.txn_age_seconds
    state.log_safety_database    = log_info.database_name
    state.plan_cache_hit             = plan_info.hit
    state.plan_cache_source          = plan_info.source or ""
    state.plan_cache_parent_object   = plan_info.parent_object
    state.plan_cache_statement_text  = plan_info.statement_text or ""
    state.query_hash               = plan_info.query_hash
    state.query_plan_hash          = plan_info.query_plan_hash
    state.plan_age_minutes         = plan_info.plan_age_minutes
    state.qs_enabled             = qs_info.qs_enabled
    state.qs_plans_found         = qs_info.plans_found
    state.qs_better_plan_exists  = qs_info.better_plan_exists
    state.qs_plan_recommendation = qs_info.qs_plan_recommendation
    state.qs_best_plan_id        = qs_info.best_plan_id
    state.lock_type              = lock_info.lock_type
    state.lock_diagnosis         = lock_info.lock_diagnosis
    state.isolation_level        = lock_info.isolation_level
    state.locked_object          = lock_info.locked_object
    state.open_txn_count         = lock_info.open_txn_count
    state.scenario_id            = scenario_info.scenario_id
    state.scenario_name          = scenario_info.scenario_name
    state.scenario_guidance      = scenario_info.scenario_guidance
    state.percent_complete       = log_info.percent_complete
    state.qs_plan_table          = qs_plan_table
    state.blocker_plan_xml       = blocker_plan_xml
    state.parallel_query_detected = parallel_detected
    state.parallel_wait_types     = parallel_wait_types
    state.errors                 = errors

    await context.send_message(state)
