"""
src/orchestrator/state.py
Shared state schema that travels between all executor nodes in the pipeline.
Same fields as BlockingAgentState -- just renamed to BlockingState and passed
directly between MAF executors via WorkflowContext.send_message().
"""

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class BlockingState(BaseModel):
    # Identity
    server_name:     str = ""
    correlation_id:  str = ""

    # Detection
    has_blocking:    bool = False
    blocking_rows:   list[dict] = Field(default_factory=list)
    head_blocker:    Optional[dict] = None

    # Analyzer Agent
    llm_analysis:           str = ""
    severity_hint:          Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    diagnosis:              str = ""

    # Log & rollback safety
    log_used_pct:           float = 0.0
    log_used_mb:            float = 0.0
    kill_safety_rating:     str = ""
    estimated_rollback_sec: float = 0.0
    txn_age_seconds:        int = 0
    log_safety_database:    str = ""

    # Plan cache
    plan_cache_hit:             bool = False
    plan_cache_source:          str = ""
    plan_cache_parent_object:   str = ""
    plan_cache_statement_text:  str = ""
    query_hash:                 str = ""
    query_plan_hash:            str = ""
    plan_age_minutes:           int = 0

    # Query Store
    qs_enabled:             bool = False
    qs_plans_found:         int = 0
    qs_better_plan_exists:  bool = False
    qs_plan_recommendation: str = ""
    qs_best_plan_id:        Optional[int] = None
    qs_plan_table:          str = ""

    # Lock analysis
    lock_type:              str = ""
    lock_diagnosis:         str = ""
    isolation_level:        str = ""
    locked_object:          str = ""
    open_txn_count:         int = 0

    # Microsoft KB scenario classification
    scenario_id:            int = 0
    scenario_name:          str = ""
    scenario_guidance:      str = ""
    percent_complete:       Optional[float] = None

    # Determination Agent
    decision:               Literal["KILL", "ALERT_ONLY", "SKIP"] = "SKIP"
    decision_reason:        str = ""
    risk_level:             Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    rule_triggered:         int = 0
    llm_reasoning:          str = ""
    dba_approval_required:  bool = False

    # Action (SQL Executor / SQL Validator)
    kill_executed:   bool = False
    killed_spid:     Optional[int] = None
    kill_time_utc:   Optional[str] = None
    kill_status:     str = "NOT_ATTEMPTED"
    kill_validation: str = ""

    # RCA Agent
    rca_report:       str = ""
    rca_data:         Optional[dict] = None
    html_report_path: Optional[str] = None

    # Parallel query signals
    parallel_query_detected: bool = False
    parallel_wait_types: str = ""

    # Plan capture
    blocker_plan_xml: Optional[str] = None

    # Runtime
    cycle_start_utc: Optional[str] = None
    dry_run:         bool = True
    errors:          list[str] = Field(default_factory=list)

    # dict-style access for compatibility with tool/node code
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)
