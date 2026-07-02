"""
orchestrator/config.py
----------------------
Loads configuration in two layers:
  1. Environment variables (.env) — always applied first as defaults.
  2. AgentConfigDB.dbo.GlobalConfig — DB values override env vars for
     any key that exists in the table, so DBAs can tune thresholds live
     without redeploying. Falls back to env-var defaults if the DB is
     unavailable (e.g. during local dev without AgentConfigDB).

Also exposes get_config() so MAF executor nodes can access the active
config without a RunnableConfig injection (MAF has no equivalent mechanism).
"""

import os
import logging
import pyodbc

log = logging.getLogger("config")

DRIVER = "ODBC Driver 17 for SQL Server"

_GLOBAL_CONFIG_SQL = "SELECT ConfigKey, ConfigValue FROM dbo.GlobalConfig"

# Singleton populated by load_config() -- read by get_config() from every executor
_ACTIVE_CONFIG: dict = {}


def get_config() -> dict:
    """Return the config dict loaded at startup. Call after load_config()."""
    return _ACTIVE_CONFIG


# Maps GlobalConfig keys → (config dict key, type caster)
_DB_KEY_MAP = {
    "DryRunGlobal":           ("dry_run",                      lambda v: v.lower() == "true"),
    "KillThresholdMs":        ("kill_threshold_ms",            int),
    "PollIntervalSeconds":    ("poll_interval_seconds",        int),
    "LogSizeKillThresholdGB": ("log_size_kill_threshold_gb",   float),
    "MaxKillsPerHour":        ("max_kills_per_hour",           int),
    "PlanLookbackHours":      ("plan_lookback_hours",          int),
    "DbaAlertEmail":          ("dba_email",                    str),
}


def _read_db_config(config_conn_str: str) -> dict:
    """
    Read GlobalConfig from AgentConfigDB.
    Returns {} if the DB is unreachable (graceful degradation).
    """
    try:
        with pyodbc.connect(config_conn_str, timeout=5) as conn:
            cursor = conn.cursor()
            cursor.execute(_GLOBAL_CONFIG_SQL)
            rows = cursor.fetchall()

        overrides = {}
        for db_key, config_value in rows:
            if db_key in _DB_KEY_MAP:
                config_key, caster = _DB_KEY_MAP[db_key]
                try:
                    overrides[config_key] = caster(config_value)
                except Exception as e:
                    log.warning("GlobalConfig: could not cast %s=%r — %s", db_key, config_value, e)

        log.info("GlobalConfig: loaded %d override(s) from AgentConfigDB", len(overrides))
        return overrides

    except Exception as e:
        log.warning("GlobalConfig: AgentConfigDB unreachable (%s) — using env-var defaults", e)
        return {}


def _conn(server: str, database: str) -> str:
    return (
        f"DRIVER={{{DRIVER}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        "Trusted_Connection=yes;"
        "TrustServerCertificate=yes;"
    )


def load_config() -> dict:
    """
    Build the runtime config dict.
    Env vars supply defaults; AgentConfigDB.GlobalConfig overrides them
    for any key present in the table.
    """
    server = os.getenv("SQL_SERVER", "localhost")

    config = {
        # Connection strings (always from env — never stored in GlobalConfig)
        "monitor_conn_str": _conn(server, "master"),
        "config_conn_str":  _conn(server, "AgentConfigDB"),
        "log_conn_str":     _conn(server, "AgentLogDB"),

        "server_name": server,

        # --- Env-var defaults (may be overridden by GlobalConfig below) ---
        "dry_run":                    os.getenv("DRY_RUN", "true").lower() == "true",
        "kill_threshold_ms":          int(os.getenv("KILL_THRESHOLD_MS", "30000")),
        "poll_interval_seconds":      int(os.getenv("POLL_INTERVAL_SECONDS", "15")),
        "log_size_kill_threshold_gb": float(os.getenv("LOG_SIZE_KILL_THRESHOLD_GB", "10")),
        "max_kills_per_hour":         int(os.getenv("MAX_KILLS_PER_HOUR", "10")),
        "plan_lookback_hours":        int(os.getenv("PLAN_LOOKBACK_HOURS", "24")),
        "dba_email":                  os.getenv("DBA_EMAIL", "evhdba@evolent.com"),

        # R11 -- only kill if victims include one of these account patterns
        # (fnmatch-style, e.g. "app_*", "svc_*")
        "application_account_patterns": [
            "app_*", "svc_*", "BBI_*",
        ],

        # R12 -- isolation levels that are intentional by design, not bugs
        "skip_isolation_levels": [
            "SERIALIZABLE",
        ],

        # SMTP — always from env (credentials don't belong in the DB)
        "smtp_host":     os.getenv("SMTP_HOST", ""),
        "smtp_port":     int(os.getenv("SMTP_PORT", "587")),
        "smtp_from":     os.getenv("SMTP_FROM", ""),
        "smtp_user":     os.getenv("SMTP_USER", ""),
        "smtp_password": os.getenv("SMTP_PASSWORD", ""),
    }

    # Layer 2: apply DB overrides on top of env-var defaults
    db_overrides = _read_db_config(config["config_conn_str"])
    config.update(db_overrides)

    # Populate singleton so executor nodes can call get_config()
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = config

    log.info("Config loaded:")
    log.info("  server                    = %s", config["server_name"])
    log.info("  dry_run                   = %s", config["dry_run"])
    log.info("  kill_threshold_ms         = %s", config["kill_threshold_ms"])
    log.info("  poll_interval_seconds     = %s", config["poll_interval_seconds"])
    log.info("  log_size_kill_threshold_gb= %s", config["log_size_kill_threshold_gb"])
    log.info("  max_kills_per_hour        = %s", config["max_kills_per_hour"])
    log.info("  plan_lookback_hours       = %s", config["plan_lookback_hours"])
    log.info("  dba_email                 = %s", config["dba_email"])
    log.info("  llm_model                 = %s", os.getenv("LLM_MODEL", "gpt-4.1"))
    log.info("  llm_timeout_seconds       = %s", os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    log.info("  llm_max_retries           = %s", os.getenv("LLM_MAX_RETRIES", "2"))

    return config
