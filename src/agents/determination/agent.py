"""
src/agents/determination/agent.py  --  Determination Agent
----------------------------------------------------------------------
Same logic as the LangGraph version: hard safety gates (R2/R3/R9/R10/
R11/R12/R13/R14) enforced in code, then LLM makes KILL/ALERT_ONLY/SKIP
decision.

Changes from LangGraph version:
  - run() is now async (ChatAgent.run() is a coroutine)
  - No RunnableConfig; config comes from get_config()
  - State is BlockingState (attribute access instead of dict.get())
"""

import fnmatch
import json
import logging
from pathlib import Path

from agent_framework import executor, WorkflowContext
from agents.base_agent import BaseAgent
from orchestrator.config import get_config
from orchestrator.state import BlockingState
from models.schemas import DeterminationLLMResult
from tools.kill_rate import KillRateInput, check_kill_rate
from tools.scenario import DTC_WAIT_TYPES

log = logging.getLogger("agent.determination")

_PROMPT_FILE = Path(__file__).resolve().parent / "prompt.md"
SYSTEM_PROMPT = _PROMPT_FILE.read_text(encoding="utf-8")


class DeterminationAgent(BaseAgent):

    async def run(self, state: BlockingState) -> dict:
        server = state.server_name
        head = state.head_blocker or {}
        spid = head.get("session_id")
        log.info("[%s] Determination Agent: SPID=%s", server, spid)

        gate = self._check_hard_gates(state, head, spid)
        if gate is not None:
            decision, risk, reason, rule, dba_approval_required = gate
            log.warning("[%s] Rule %d triggered pre-LLM. Forcing %s.", server, rule, decision)
            return self._build_result(decision, risk, reason, rule, dba_approval_required)

        user_msg = f"""\
## Head Blocker

{json.dumps(head, default=str, indent=2)}

## Analyzer Summary (severity hint: {state.severity_hint})

{state.llm_analysis}

## KB Scenario

#{state.scenario_id} {state.scenario_name}
{state.scenario_guidance}

## Diagnostic Fields

{json.dumps({
    "wait_duration_ms": (state.head_blocker or {}).get("wait_duration_ms", 0),
    "victim_count": (state.head_blocker or {}).get("victim_count", 0),
    "log_used_pct": state.log_used_pct,
    "log_used_mb": state.log_used_mb,
    "kill_safety_rating": state.kill_safety_rating,
    "plan_cache_hit": state.plan_cache_hit,
    "qs_better_plan_exists": state.qs_better_plan_exists,
    "qs_plan_recommendation": state.qs_plan_recommendation,
    "lock_type": state.lock_type,
    "lock_diagnosis": state.lock_diagnosis,
    "isolation_level": state.isolation_level,
    "locked_object": state.locked_object,
}, default=str, indent=2)}

Make your decision now.
"""
        try:
            raw = await self.ask_llm_json(SYSTEM_PROMPT, user_msg)
            result = DeterminationLLMResult(**raw)
        except Exception as e:
            log.error("[%s] Determination LLM failed: %s -- defaulting to ALERT_ONLY", server, e)
            result = DeterminationLLMResult(
                decision="ALERT_ONLY", risk_level="MEDIUM",
                reason=f"LLM unavailable ({e}). Defaulting to ALERT_ONLY.",
                safety_check_passed=False, rule_triggered=0,
            )

        decision = result.decision
        risk = result.risk_level
        reason = result.reason

        if decision == "KILL" and self.config.get("dry_run", True):
            reason = "[DRY RUN] " + reason
            decision = "ALERT_ONLY"
            log.info("[%s] DRY RUN: decision changed to ALERT_ONLY", server)

        log.info("[%s] Decision=%s  risk=%s  rule=%s  reason=%s",
                 server, decision, risk, result.rule_triggered, reason[:120])

        return self._build_result(decision, risk, reason, result.rule_triggered)

    # ── Hard safety gates (R2, R3, R13, R14, R9, R10, R11, R12) ────────────────

    def _check_hard_gates(self, state: BlockingState, head: dict, spid):
        server = state.server_name

        # R2 -- wait duration below kill threshold
        kill_threshold_ms = self.config.get("kill_threshold_ms", 30000)
        wait_ms = head.get("wait_duration_ms", 0)
        if wait_ms < kill_threshold_ms:
            return (
                "SKIP", "LOW",
                f"SPID {spid} wait_duration_ms={wait_ms} is below kill_threshold_ms="
                f"{kill_threshold_ms}. Monitoring only.",
                2, False,
            )

        # R3 -- system session (SPID < 50)
        if spid is not None and spid < 50:
            return (
                "ALERT_ONLY", "HIGH",
                f"SPID {spid} is a system session (SPID < 50). System sessions are "
                f"never killed automatically. Escalated to DBA.",
                3, False,
            )

        # R13 -- session already rolling back
        percent_complete = state.percent_complete or 0
        head_wait = (head.get("wait_type") or "").upper()
        if (state.scenario_id == 5
                or percent_complete > 0
                or "ROLLBACK" in head_wait):
            pct_str = f" (rollback {percent_complete:.0f}% complete)" if percent_complete else ""
            return (
                "ALERT_ONLY", "HIGH",
                f"SPID {spid} is already rolling back{pct_str} (scenario 5). "
                f"SQL Server ignores a second KILL on a rolling-back session. "
                f"Wait for rollback to complete before any further action.",
                13, False,
            )

        # R14 -- distributed transaction
        head_row = next(
            (r for r in state.blocking_rows if r.get("session_id") == spid),
            {},
        )
        head_own_wait = (head_row.get("wait_type") or "").upper()
        if (state.scenario_id == 4
                or head_own_wait in DTC_WAIT_TYPES
                or "DTC" in head_own_wait):
            return (
                "ALERT_ONLY", "HIGH",
                f"SPID {spid} is involved in a distributed transaction "
                f"(wait_type={head_own_wait or 'N/A'}, scenario 4). "
                f"Killing the local DTC participant without coordinating the remote "
                f"endpoint can orphan a prepared transaction. Manual MSDTC resolution required.",
                14, False,
            )

        # R9 -- transaction log size exceeds configured threshold
        threshold_gb = self.config.get("log_size_kill_threshold_gb", 10)
        log_used_mb = state.log_used_mb
        if log_used_mb >= threshold_gb * 1024:
            return (
                "ALERT_ONLY", "HIGH",
                f"Transaction log is {log_used_mb / 1024:.1f} GB "
                f"(threshold: {threshold_gb:.0f} GB). Killing SPID {spid} would "
                f"trigger a massive rollback. Escalated to DBA for manual approval.",
                9, True,
            )

        # R10 -- kill-rate limiter
        max_kills_per_hour = self.config.get("max_kills_per_hour")
        if max_kills_per_hour is not None:
            kill_rate = check_kill_rate(
                KillRateInput(log_conn_str=self.log_conn_str, server_name=server),
                self.query_sql,
            )
            if kill_rate.kills_last_hour >= max_kills_per_hour:
                return (
                    "ALERT_ONLY", "HIGH",
                    f"{kill_rate.kills_last_hour} kill(s) already issued on {server} in "
                    f"the last hour (max_kills_per_hour={max_kills_per_hour}). "
                    f"Escalated to DBA to avoid a runaway kill loop.",
                    10, False,
                )

        # R11 -- target policy: only act if victims include application accounts
        app_patterns = self.config.get("application_account_patterns", [])
        if app_patterns:
            victim_spids = set(head.get("victim_spids", []))
            victim_logins = [
                str(row.get("login_name", ""))
                for row in state.blocking_rows
                if row.get("session_id") in victim_spids
            ]
            victim_is_app_account = any(
                any(fnmatch.fnmatch(login, pattern) for pattern in app_patterns)
                for login in victim_logins
            )
            if victim_logins and not victim_is_app_account:
                return (
                    "SKIP", "LOW",
                    f"Victims of SPID {spid} ({', '.join(victim_logins)}) do not match "
                    f"application_account_patterns. Head blocker may be an app account "
                    f"blocking only system processes -- skipping/re-evaluating.",
                    11, False,
                )

        # R12 -- isolation levels we intentionally skip
        skip_isolation_levels = self.config.get("skip_isolation_levels", [])
        isolation_level = state.isolation_level or ""
        if any(lvl.upper() in isolation_level.upper() for lvl in skip_isolation_levels):
            return (
                "SKIP", "LOW",
                f"SPID {spid} is running under isolation level '{isolation_level}', "
                f"which is in skip_isolation_levels (intentional design, not a "
                f"blocking bug).",
                12, False,
            )

        return None

    def _build_result(self, decision, risk, reason, rule, dba_approval_required=False) -> dict:
        return {
            "decision":              decision,
            "risk_level":            risk,
            "decision_reason":       reason,
            "rule_triggered":        rule,
            "dba_approval_required": dba_approval_required,
            "llm_reasoning":         reason,
        }


@executor(id="determination")
async def determination_node(state: BlockingState, context: WorkflowContext) -> None:
    cfg = get_config()
    updates = await DeterminationAgent(cfg).run(state)
    state.decision              = updates["decision"]
    state.risk_level            = updates["risk_level"]
    state.decision_reason       = updates["decision_reason"]
    state.rule_triggered        = updates["rule_triggered"]
    state.dba_approval_required = updates["dba_approval_required"]
    state.llm_reasoning         = updates["llm_reasoning"]
    await context.send_message(state)
