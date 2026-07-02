"""
tests/unit/test_gap_a_and_h.py
--------------------------------------------------------------

Unit tests for:
  Gap A — expected_login passed to validate_kill (SPID recycling protection)
  Gap H — Parallel query detection in _detect_parallel_signals()

No SQL Server or Azure connection required — all pure-Python logic.
These tests are identical to the LangGraph version because the tools
and detector logic are framework-agnostic.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault(
    "FOUNDRY_PROJECT_ENDPOINT",
    "https://fake-test.services.ai.azure.com/api/projects/test",
)
os.environ.setdefault("LLM_API_KEY", "test-placeholder-key")

_env = Path(__file__).resolve().parent.parent.parent / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env, override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import pytest

from tools.sql_validator import SqlValidatorInput, validate_kill
from agents.detector.agent import _detect_parallel_signals


class _QuerySqlSpy:
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[dict] = []

    def __call__(self, conn_str, sql, params=None):
        self.calls.append({"sql": sql, "params": params})
        return self._rows


# ── Gap A tests ───────────────────────────────────────────────────────────────

class TestGapA_ExpectedLogin:

    def test_expected_login_triggers_spid_recycling_detection(self):
        spy = _QuerySqlSpy(rows=[{"session_id": 55, "login_name": "sa", "status": "running"}])
        result = validate_kill(
            SqlValidatorInput(monitor_conn_str="Driver=...", session_id=55,
                              expected_login="batch_job_usr"),
            query_sql=spy,
        )
        assert result.validation_status == "CONFIRMED_GONE"
        assert result.kill_status == "SUCCESS"

    def test_expected_login_same_login_not_recycled(self):
        spy = _QuerySqlSpy(rows=[{"session_id": 55, "login_name": "batch_job_usr", "status": "sleeping"}])
        result = validate_kill(
            SqlValidatorInput(monitor_conn_str="Driver=...", session_id=55,
                              expected_login="batch_job_usr"),
            query_sql=spy,
        )
        assert result.validation_status in ("STILL_PRESENT", "ROLLING_BACK", "CONFIRMED_GONE")
        assert spy.calls

    def test_no_expected_login_skips_recycling_check(self):
        spy = _QuerySqlSpy(rows=[{"session_id": 55, "login_name": "sa", "status": "running"}])
        result = validate_kill(
            SqlValidatorInput(monitor_conn_str="Driver=...", session_id=55, expected_login=""),
            query_sql=spy,
        )
        assert result.validation_status == "STILL_PRESENT"

    def test_expected_login_spid_gone_entirely(self):
        spy = _QuerySqlSpy(rows=[])
        result = validate_kill(
            SqlValidatorInput(monitor_conn_str="Driver=...", session_id=55,
                              expected_login="batch_job_usr"),
            query_sql=spy,
        )
        assert result.validation_status == "CONFIRMED_GONE"
        assert result.kill_status == "SUCCESS"

    def test_expected_login_rollback_status(self):
        spy = _QuerySqlSpy(rows=[{"session_id": 55, "login_name": "batch_job_usr", "status": "rollback"}])
        result = validate_kill(
            SqlValidatorInput(monitor_conn_str="Driver=...", session_id=55,
                              expected_login="batch_job_usr"),
            query_sql=spy,
        )
        assert result.validation_status == "ROLLING_BACK"
        assert result.kill_status == "SUCCESS"

    def test_action_node_passes_login_to_validator(self):
        inp = SqlValidatorInput(monitor_conn_str="Driver=...", session_id=77,
                                expected_login="svc_appaccount")
        assert inp.expected_login == "svc_appaccount"
        assert inp.session_id == 77


# ── Gap H tests ───────────────────────────────────────────────────────────────

class TestGapH_ParallelDetection:

    def test_cxpacket_in_victim_detected(self):
        rows = [{"session_id": 70, "blocking_session_id": 55, "wait_type": "CXPACKET"}]
        detected, wait_types = _detect_parallel_signals(rows, plan_xml=None)
        assert detected is True
        assert "CXPACKET" in wait_types

    def test_cxconsumer_in_victim_detected(self):
        rows = [{"session_id": 71, "blocking_session_id": 55, "wait_type": "CXCONSUMER"}]
        detected, wait_types = _detect_parallel_signals(rows, plan_xml=None)
        assert detected is True
        assert "CXCONSUMER" in wait_types

    def test_multiple_parallel_wait_types_all_captured(self):
        rows = [
            {"session_id": 70, "wait_type": "CXPACKET"},
            {"session_id": 71, "wait_type": "CXCONSUMER"},
            {"session_id": 72, "wait_type": "LCK_M_X"},
        ]
        detected, wait_types = _detect_parallel_signals(rows, plan_xml=None)
        assert detected is True
        assert "CXPACKET" in wait_types
        assert "CXCONSUMER" in wait_types
        assert "LCK_M_X" not in wait_types

    def test_no_parallel_wait_types_not_detected(self):
        rows = [{"session_id": 70, "wait_type": "LCK_M_X"},
                {"session_id": 71, "wait_type": "ASYNC_NETWORK_IO"}]
        detected, wait_types = _detect_parallel_signals(rows, plan_xml=None)
        assert detected is False
        assert wait_types == ""

    def test_empty_rows_not_detected(self):
        detected, wait_types = _detect_parallel_signals([], plan_xml=None)
        assert detected is False

    def test_none_wait_type_in_rows_handled(self):
        rows = [{"session_id": 70, "wait_type": None}]
        detected, wait_types = _detect_parallel_signals(rows, plan_xml=None)
        assert detected is False

    def test_parallelism_element_in_xml_detected(self):
        xml = '<ShowPlanXML><Parallelism Activations="4"/></ShowPlanXML>'
        detected, wait_types = _detect_parallel_signals([], plan_xml=xml)
        assert detected is True
        assert wait_types == ""

    def test_isparallel_attribute_in_xml_detected(self):
        xml = '<RelOp IsParallel="1" PhysicalOp="Hash Match">'
        detected, wait_types = _detect_parallel_signals([], plan_xml=xml)
        assert detected is True

    def test_serial_plan_xml_not_detected(self):
        xml = '<RelOp PhysicalOp="Clustered Index Scan" IsParallel="0"/>'
        detected, wait_types = _detect_parallel_signals([], plan_xml=xml)
        assert detected is False

    def test_none_plan_xml_not_detected(self):
        detected, _ = _detect_parallel_signals([], plan_xml=None)
        assert detected is False

    def test_both_wait_type_and_xml_parallel(self):
        rows = [{"session_id": 70, "wait_type": "CXPACKET"}]
        xml = '<Parallelism Activations="4"/>'
        detected, wait_types = _detect_parallel_signals(rows, plan_xml=xml)
        assert detected is True
        assert "CXPACKET" in wait_types

    def test_xml_parallel_overrides_no_wait_types(self):
        rows = [{"session_id": 70, "wait_type": "LCK_M_S"}]
        xml = '<Parallelism Activations="4"/>'
        detected, wait_types = _detect_parallel_signals(rows, plan_xml=xml)
        assert detected is True
        assert wait_types == ""

    def test_analyzer_prompt_contains_parallel_guidance(self):
        prompt = (Path(__file__).parents[2] / "src/agents/analyzer/prompt.md").read_text(encoding="utf-8")
        assert "CXPACKET" in prompt
        assert "CXCONSUMER" in prompt
        assert "parallel_query_detected" in prompt
        assert "MAXDOP" in prompt

    def test_rca_prompt_contains_parallel_gap4(self):
        prompt = (Path(__file__).parents[2] / "src/agents/rca/prompt.md").read_text(encoding="utf-8")
        assert "Gap 4" in prompt
        assert "CXPACKET" in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
