# SQL Server Blocking Agent — Microsoft Agent Framework

Autonomous SQL Server blocking detection and remediation agent, rewritten with
**Microsoft Agent Framework 1.0** (Semantic Kernel + AutoGen combined, released April 2026).

> **Companion repo:** The original LangGraph implementation lives at
> `c:\Python\sql-blocking-agent\` (GitHub: `venyel238/sql-blocking-agent`).
> See [COMPARISON.md](COMPARISON.md) or open [COMPARISON.html](COMPARISON.html)
> for a full side-by-side framework analysis.

---

## What it does

Every 15 seconds (configurable) the agent runs a 6-node pipeline:

```
Detection → Analyzer → Determination → [Action] → RCA → Notification
```

| Node | Type | Role |
|------|------|------|
| Detection | Tool (deterministic) | DMV queries: blocking chain, log safety, plan cache, Query Store, locks, KB scenario |
| Analyzer | LLM (`gpt-4.1`) | Synthesizes all diagnostic data into a narrative + severity hint |
| Determination | LLM + hard gates | Applies 8 governance rules (R2/R3/R9–R14), then LLM decides KILL / ALERT_ONLY / SKIP |
| Action | Tool (deterministic) | Issues `KILL <spid>`, validates outcome, writes immutable KillAuditLog |
| RCA | LLM | Generates root-cause analysis with historical recurrence context |
| Notification | Tool (deterministic) | Writes BlockingEventLog, renders HTML/MD report, sends DBA email |

---

## Framework: Microsoft Agent Framework 1.0

| LangGraph (original) | MS Agent Framework (this repo) |
|----------------------|-------------------------------|
| `StateGraph` | `WorkflowBuilder` |
| `ChatOpenAI` (sync) | `FoundryChatClient` + `ChatAgent` (async) |
| `def node(state, RunnableConfig)` | `@executor async def node(state, WorkflowContext)` |
| Return partial dict | Mutate + `context.send_message(state)` |
| `add_conditional_edges` | `FanOutEdgeGroup(selection_func)` |
| `add_edge(..., END)` | `await context.yield_output(state)` |

---

## Prerequisites

- Python 3.10+
- ODBC Driver 17 for SQL Server
- Azure AI Foundry project endpoint + model deployment
- SQL Server with AgentConfigDB and AgentLogDB (see `sql/`)

---

## Quick Start

```bash
# 1. Clone and enter the folder
cd c:\Python\ms-blocking-agent

# 2. Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — set FOUNDRY_PROJECT_ENDPOINT, LLM_MODEL, SQL_SERVER

# 5. Set up SQL Server databases (run once)
sqlcmd -S YOUR_SERVER -i sql\01_config_db.sql
sqlcmd -S YOUR_SERVER -i sql\02_log_db.sql
sqlcmd -S YOUR_SERVER -i sql\03_logins.sql

# 6. Run
python main.py
```

---

## Environment Variables

### Azure AI Foundry (LLM)

| Variable | Default | Description |
|----------|---------|-------------|
| `FOUNDRY_PROJECT_ENDPOINT` | — | Azure AI Foundry project endpoint |
| `LLM_MODEL` | `gpt-4.1` | Model deployment name |
| `LLM_API_KEY` | — | API key (leave blank to use `DefaultAzureCredential`) |
| `LLM_TIMEOUT_SECONDS` | `30` | Per-request LLM timeout |
| `LLM_MAX_RETRIES` | `2` | Retry attempts on transient errors |

**`FOUNDRY_PROJECT_ENDPOINT` format:**
```
https://<resource-name>.services.ai.azure.com/api/projects/<project-name>
```

### SQL Server

| Variable | Default | Description |
|----------|---------|-------------|
| `SQL_SERVER` | `localhost` | SQL Server hostname or IP |
| `DRY_RUN` | `true` | `true` = never actually KILL; log and alert only |

### Agent Behaviour

| Variable | Default | Description |
|----------|---------|-------------|
| `KILL_THRESHOLD_MS` | `30000` | Minimum wait duration before considering a KILL |
| `POLL_INTERVAL_SECONDS` | `15` | How often the detection loop runs |
| `LOG_SIZE_KILL_THRESHOLD_GB` | `10` | Log size above which DBA approval is required |
| `MAX_KILLS_PER_HOUR` | `10` | Kill-rate limiter (R10) |
| `PLAN_LOOKBACK_HOURS` | `24` | How far back plan cache / Query Store looks |
| `DBA_EMAIL` | — | Email for DBA approval alerts |

> All variables can also be overridden live via `AgentConfigDB.dbo.GlobalConfig` — no restart needed.

---

## Project Structure

```
ms-blocking-agent/
├── main.py                         # Polling loop entry point
├── requirements.txt
├── agent.yaml                      # Agent manifest
├── Dockerfile
├── .env.example
├── COMPARISON.md                   # LangGraph vs MAF — markdown
├── COMPARISON.html                 # LangGraph vs MAF — interactive HTML
├── CHANGES.md
├── sql/
│   ├── 01_config_db.sql            # AgentConfigDB + GlobalConfig table
│   ├── 02_log_db.sql               # AgentLogDB + BlockingEventLog + KillAuditLog
│   └── 03_logins.sql               # SQL logins for the agent service account
├── src/
│   ├── orchestrator/
│   │   ├── workflow.py             # WorkflowBuilder (replaces StateGraph)
│   │   ├── state.py                # BlockingState Pydantic model
│   │   ├── router.py               # selection_func helpers for FanOutEdgeGroup
│   │   └── config.py               # Two-layer config + get_config() singleton
│   ├── agents/
│   │   ├── base_agent.py           # FoundryChatClient, SQL helpers, async ask_llm
│   │   ├── detector/agent.py       # @executor detection_node
│   │   ├── analyzer/               # @executor analyzer_node + prompt.md
│   │   ├── determination/          # @executor determination_node + prompt.md
│   │   ├── action/agent.py         # @executor action_node (SQL kill + validate)
│   │   ├── rca/                    # @executor rca_node + prompt.md
│   │   └── notifier/agent.py       # @executor notification_node (yields output)
│   ├── tools/                      # 13 framework-agnostic tool modules (identical to LangGraph)
│   ├── memory/long_term.py         # Historical recurrence queries (identical)
│   └── models/schemas.py           # Pydantic LLM response contracts (identical)
└── tests/
    ├── unit/                       # Pure-Python tests, no SQL Server needed
    └── integration/                # Requires live SQL Server
```

---

## Governance Rules (hard gates — never overridable by LLM)

| Rule | Condition | Decision |
|------|-----------|----------|
| R2 | Wait < `KILL_THRESHOLD_MS` | SKIP |
| R3 | SPID < 50 (system session) | ALERT_ONLY |
| R9 | Log > `LOG_SIZE_KILL_THRESHOLD_GB` | ALERT_ONLY + DBA email |
| R10 | Kills last hour ≥ `MAX_KILLS_PER_HOUR` | ALERT_ONLY |
| R11 | Victims not in `application_account_patterns` | SKIP |
| R12 | Isolation level in `skip_isolation_levels` | SKIP |
| R13 | Session already rolling back (scenario 5) | ALERT_ONLY |
| R14 | Distributed transaction / DTC wait type | ALERT_ONLY |

---

## Running Tests

```bash
# Unit tests (no SQL Server required)
pytest tests/unit/ -v

# Integration tests (requires live SQL Server)
pytest tests/integration/ -v
```

---

## Authentication

| Environment | Credential used |
|-------------|----------------|
| Local dev | `az login` → `AzureCliCredential` via `DefaultAzureCredential` |
| Azure Container Apps | Managed Identity → `ManagedIdentityCredential` |
| API key mode | Set `LLM_API_KEY` → `AzureKeyCredential` |
