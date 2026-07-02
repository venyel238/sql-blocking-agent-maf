# Changelog — SQL Server Blocking Agent (Microsoft Agent Framework)

All notable changes to the MAF implementation are documented here.
For the LangGraph version changelog see `c:\Python\sql-blocking-agent\CHANGES.md`.

---

## [2026-07-02] — Initial MAF implementation

**Complete rewrite of the orchestration layer using Microsoft Agent Framework 1.0.**

### Added
- `src/orchestrator/workflow.py` — `WorkflowBuilder` + `FanOutEdgeGroup` replacing LangGraph `StateGraph`
- `src/orchestrator/config.py` — added `get_config()` singleton (replaces `RunnableConfig` injection)
- `src/orchestrator/router.py` — routing functions returning `list[str]` (MAF `selection_func` API)
- `src/orchestrator/state.py` — `BlockingState` Pydantic model (same fields as LangGraph version)
- `src/agents/base_agent.py` — `FoundryChatClient` + `ChatAgent` replacing `ChatOpenAI`; async `ask_llm` / `ask_llm_json`; `ask_llm_json_sync()` bridge for sync tool callables
- All 6 agent executors decorated with `@executor(id=...)` and converted to `async def`
- `COMPARISON.md` + `COMPARISON.html` — full side-by-side framework comparison
- `agent.yaml` — MAF-flavoured manifest with `executor_id` annotations
- `Dockerfile` — includes Azure CLI for `DefaultAzureCredential` support
- `tests/unit/` — adapted unit tests (async-aware, no ChatOpenAI dependency)

### Identical (copied from LangGraph version)
- All 13 tool files in `src/tools/`
- `src/memory/long_term.py`
- `src/models/schemas.py`
- All three `prompt.md` files (analyzer, determination, rca)
- `sql/` — database setup scripts

### Key API differences vs LangGraph
| Concept | LangGraph | MAF |
|---------|-----------|-----|
| Workflow builder | `StateGraph` | `WorkflowBuilder` |
| Node definition | `def node(state, RunnableConfig)` | `@executor async def node(state, WorkflowContext)` |
| State flow | Return partial dict | Mutate + `context.send_message(state)` |
| Conditional routing | `add_conditional_edges(fn → str)` | `FanOutEdgeGroup(selection_func → list[str])` |
| Terminal node | `add_edge(..., END)` | `await context.yield_output(state)` |
| LLM client | `ChatOpenAI` (sync) | `FoundryChatClient` + `ChatAgent` (async) |
| Config injection | `RunnableConfig["configurable"]` | `get_config()` singleton |
