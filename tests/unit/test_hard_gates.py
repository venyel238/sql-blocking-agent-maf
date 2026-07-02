"""
Unit tests for DeterminationAgent._check_hard_gates() R13 and R14.

Tests run without any SQL Server or Azure connection.
_check_hard_gates() is pure logic up to R10 (kill-rate), which is
never reached when R13/R14 fire first.

MAF note: _check_hard_gates() is synchronous — no async required here.
"""

import os
import sys
from pathlib import Path

# MAF uses FoundryChatClient — supply a placeholder endpoint so the import
# succeeds without a real Azure connection.
os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT",
                      "https://fake-test.services.ai.azure.com/api/projects/test")
os.environ.setdefault("LLM_API_KEY", "test-placeholder-key")

_env = Path(__file__).resolve().parent.parent.parent / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env, override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from agents.determination.agent import DeterminationAgent


_CFG = {
    "monitor_conn_str": "Driver={SQL Server};Server=.;",
    "log_conn_str":     "Driver={SQL Server};Server=.;",
    "dry_run":          True,
    "kill_threshold_ms":          30_000,
    "log_size_kill_threshold_gb": 10,
    "max_kills_per_hour":         None,   # disable R10 (avoids DB call)
    "application_account_patterns": [],
    "skip_isolation_levels":      [],
}


def _agent():
    return DeterminationAgent(_CFG)


def _base_state(**overrides):
    state = {
        "server_name":    "TESTSERVER",
        "blocking_rows":  [],
        "head_blocker":   {
            "session_id":       63,
            "login_name":       "batch_usr",
            "wait_duration_ms": 60_000,
            "victim_count":     2,
            "victim_spids":     [100, 101],
            "wait_type":        "LCK_M_X",
        },
        "scenario_id":      0,
        "scenario_name":    "",
        "percent_complete": None,
        "log_used_mb":      0.0,
        "isolation_level":  "READ COMMITTED",
    }
    state.update(overrides)

    # Wrap in a simple object with .get() so DeterminationAgent can access it
    class _State(dict):
        def __getattr__(self, k):
            try:
                return self[k]
            except KeyError:
                raise AttributeError(k)

    return _State(state)


# ── R13 tests ─────────────────────────────────────────────────────────────────

def test_r13_fires_on_scenario_id_5():
    state = _base_state(scenario_id=5)
    result = _agent()._check_hard_gates(state, state["head_blocker"], 63)
    assert result is not None, "R13 must fire when scenario_id=5"
    decision, risk, reason, rule, dba = result
    assert decision == "ALERT_ONLY"
    assert risk == "HIGH"
    assert rule == 13
    assert "rolling back" in reason.lower()


def test_r13_fires_on_percent_complete_nonzero():
    state = _base_state(percent_complete=45.0)
    result = _agent()._check_hard_gates(state, state["head_blocker"], 63)
    assert result is not None, "R13 must fire when percent_complete > 0"
    decision, _, reason, rule, _ = result
    assert decision == "ALERT_ONLY"
    assert rule == 13
    assert "45" in reason


def test_r13_fires_on_rollback_wait_type():
    state = _base_state()
    head = dict(state["head_blocker"])
    head["wait_type"] = "ROLLBACK"
    result = _agent()._check_hard_gates(state, head, 63)
    assert result is not None, "R13 must fire when head wait_type contains ROLLBACK"
    decision, _, _, rule, _ = result
    assert decision == "ALERT_ONLY"
    assert rule == 13


def test_r13_does_not_fire_for_normal_blocking():
    state = _base_state(scenario_id=1)
    result = _agent()._check_hard_gates(state, state["head_blocker"], 63)
    if result is not None:
        _, _, _, rule, _ = result
        assert rule != 13, f"R13 fired unexpectedly for scenario_id=1 (rule={rule})"


# ── R14 tests ─────────────────────────────────────────────────────────────────

def test_r14_fires_on_scenario_id_4():
    state = _base_state(scenario_id=4)
    result = _agent()._check_hard_gates(state, state["head_blocker"], 63)
    assert result is not None, "R14 must fire when scenario_id=4"
    decision, risk, reason, rule, dba = result
    assert decision == "ALERT_ONLY"
    assert risk == "HIGH"
    assert rule == 14
    assert "dtc" in reason.lower() or "distributed" in reason.lower()


def test_r14_fires_on_dtc_wait_type_in_blocking_rows():
    state = _base_state()
    state["blocking_rows"] = [
        {"session_id": 63, "wait_type": "DTC_STATE", "login_name": "batch_usr"},
    ]
    result = _agent()._check_hard_gates(state, state["head_blocker"], 63)
    assert result is not None, "R14 must fire when head blocker has DTC_STATE wait_type"
    _, _, _, rule, _ = result
    assert rule == 14


def test_r14_fires_on_dtc_substring_in_wait_type():
    state = _base_state()
    state["blocking_rows"] = [
        {"session_id": 63, "wait_type": "PREEMPTIVE_DTC_ENLIST", "login_name": "batch_usr"},
    ]
    result = _agent()._check_hard_gates(state, state["head_blocker"], 63)
    assert result is not None, "R14 must fire on PREEMPTIVE_DTC_ENLIST"
    _, _, _, rule, _ = result
    assert rule == 14


def test_r14_does_not_fire_for_normal_blocking():
    state = _base_state(scenario_id=6)
    result = _agent()._check_hard_gates(state, state["head_blocker"], 63)
    if result is not None:
        _, _, _, rule, _ = result
        assert rule != 14, f"R14 fired unexpectedly for scenario_id=6 (rule={rule})"


# ── Ordering: R13 before R14 ──────────────────────────────────────────────────

def test_r13_takes_priority_over_r14_when_both_conditions_met():
    """A session rolling back AND with DTC state should hit R13 first."""
    state = _base_state(scenario_id=5, percent_complete=20.0)
    state["blocking_rows"] = [
        {"session_id": 63, "wait_type": "DTC_STATE", "login_name": "batch_usr"},
    ]
    result = _agent()._check_hard_gates(state, state["head_blocker"], 63)
    assert result is not None
    _, _, _, rule, _ = result
    assert rule == 13, f"Expected R13 before R14, got R{rule}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
