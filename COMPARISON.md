# Framework Comparison: LangGraph vs Microsoft Agent Framework

SQL Server Blocking Agent — side-by-side analysis of the two implementations.

---

## Executive Summary

| Dimension | LangGraph (LangChain) | Microsoft Agent Framework 1.0 |
|-----------|----------------------|-------------------------------|
| Released | 2023 | April 2026 |
| Foundation | LangChain + LangGraph | Semantic Kernel + AutoGen (unified) |
| Graph model | Pregel / Bulk Synchronous Parallel | Superstep BSP (same model) |
| LLM client | `ChatOpenAI` (any OpenAI-compatible) | `FoundryChatClient` (Azure AI Foundry) |
| State | `TypedDict` / Pydantic dict-like | Any object via `send_message()` |
| Routing | `add_conditional_edges()` | `FanOutEdgeGroup(selection_func=...)` |
| Node signature | `def node(state, config) -> dict` | `async def node(state, context) -> None` |
| Config injection | `RunnableConfig["configurable"]` | Module-level singleton (`get_config()`) |
| Auth | API key via env var | `DefaultAzureCredential` / API key |
| Python requirement | 3.9+ | 3.10+ |
| Ecosystem | Open-source, multi-provider | Microsoft-first, Azure-native |

---

## 1. Orchestration Layer

### LangGraph: `StateGraph`

```python
from langgraph.graph import StateGraph, END

g = StateGraph(BlockingAgentState)
g.add_node("detection",     detection_node)
g.add_node("analyzer",      analyzer_node)
g.set_entry_point("detection")
g.add_conditional_edges("detection", route_after_detection,
                        {"analyzer": "analyzer", "notification": "notification"})
g.add_edge("analyzer", "determination")
g.add_conditional_edges("determination", route_after_determination,
                        {"action": "action", "rca": "rca"})
g.add_edge("rca", "notification")
g.add_edge("notification", END)

AGENT_GRAPH = g.compile()

# Invocation
final = await AGENT_GRAPH.ainvoke(initial_state_dict, config=graph_config)
```

### Microsoft Agent Framework: `WorkflowBuilder`

```python
from agent_framework import WorkflowBuilder, FanOutEdgeGroup

builder = WorkflowBuilder(start_executor=detection_node)

builder.add_edge_group(FanOutEdgeGroup(
    source_id="detection",
    target_ids=["analyzer", "notification"],
    selection_func=route_after_detection,   # returns list[str] of target IDs
))
builder.add_edge(analyzer_node, determination_node)
builder.add_edge_group(FanOutEdgeGroup(
    source_id="determination",
    target_ids=["action", "rca"],
    selection_func=route_after_determination,
))
builder.add_edge(action_node, rca_node)
builder.add_edge(rca_node, notification_node)

AGENT_WORKFLOW = builder.build()

# Invocation
final = await AGENT_WORKFLOW.run(initial_state_object)
```

**Key differences:**
- LangGraph uses a string-based `END` sentinel; MAF has no `END` — the terminal node calls `context.yield_output(state)` to return the result.
- LangGraph router functions return a string; MAF `selection_func` returns a `list[str]` (supports true fan-out to multiple targets).
- MAF's `add_edge()` takes executor objects; LangGraph's `add_edge()` takes string node names.
- MAF's `builder.build()` is equivalent to `g.compile()`.

---

## 2. State Management

### LangGraph

```python
# src/orchestrator/state.py
class BlockingAgentState(BaseModel):
    has_blocking: bool = False
    ...

# Nodes receive a dict and return a PARTIAL update dict
def detection_node(state: dict, config: RunnableConfig) -> dict:
    return {"has_blocking": True, "blocking_rows": [...]}   # merged by LangGraph
```

LangGraph merges the returned dict into the shared state object automatically. Nodes only need to return the fields they changed.

### Microsoft Agent Framework

```python
# src/orchestrator/state.py (identical fields, different usage)
class BlockingState(BaseModel):
    has_blocking: bool = False
    ...

# Nodes mutate state in-place, then forward it
@executor(id="detection")
async def detection_node(state: BlockingState, context: WorkflowContext) -> None:
    state.has_blocking = True
    state.blocking_rows = [...]
    await context.send_message(state)      # forwards the FULL state to next node
```

**Key differences:**
- LangGraph: nodes return partial dicts — framework merges them. Fields not mentioned are preserved automatically.
- MAF: nodes mutate and forward the full state object. Unmodified fields are carried along because they are part of the same object.
- Both approaches result in the same single-source-of-truth state flowing through the pipeline.
- MAF state object can be any Python type (dataclass, dict, Pydantic model, plain object).

---

## 3. LLM Client

### LangGraph: `ChatOpenAI`

```python
# base_agent.py
from langchain_openai import ChatOpenAI

def _make_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL", "gpt-4o"),
        api_key=os.getenv("LLM_API_KEY", ""),
        base_url=os.getenv("LLM_BASE_URL", "..."),
        temperature=0,
        max_tokens=4096,
        timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
    )

class BaseAgent:
    LLM = _make_llm()            # class-level singleton

    def ask_llm(self, system_prompt, user_message) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage
        response = self.LLM.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        return response.content
```

### Microsoft Agent Framework: `FoundryChatClient` + `ChatAgent`

```python
# base_agent.py
from agent_framework import ChatAgent
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential

def _make_client() -> FoundryChatClient:
    return FoundryChatClient(
        project_endpoint=os.getenv("FOUNDRY_PROJECT_ENDPOINT"),
        model=os.getenv("LLM_MODEL", "gpt-4.1"),
        credential=DefaultAzureCredential(),
    )

class BaseAgent:
    _CLIENT: FoundryChatClient | None = None   # connection-level singleton

    async def ask_llm(self, system_prompt, user_message) -> str:
        agent = ChatAgent(
            chat_client=self.get_client(),
            instructions=system_prompt,          # system prompt set at ChatAgent creation
            temperature=0,
            max_tokens=4096,
        )
        result = await agent.run(user_message)   # async call
        return result.content
```

**Key differences:**

| Aspect | LangGraph / ChatOpenAI | MAF / FoundryChatClient |
|--------|----------------------|------------------------|
| LLM call | `LLM.invoke(messages)` — synchronous | `await agent.run(text)` — async coroutine |
| System prompt | Passed as `SystemMessage` in the messages list | Set in `ChatAgent(instructions=...)` at construction |
| Model routing | Any OpenAI-compatible endpoint via `base_url` | Azure AI Foundry project endpoint |
| Authentication | API key string in env var | `DefaultAzureCredential` (managed identity / CLI) |
| Retry logic | Built into `ChatOpenAI(max_retries=N)` | Retry configured on `FoundryChatClient` |
| Multi-provider | Any provider with an OpenAI-compatible API | Azure Foundry (other providers require adapters) |

**Important implication:** Because MAF's LLM calls are async, all agent `run()` methods and executor node functions become `async def`. The polling loop in `main.py` uses `await AGENT_WORKFLOW.run()` vs `await AGENT_GRAPH.ainvoke()` — both are already async, so `main.py` structure is nearly identical.

---

## 4. Node / Executor Definition

### LangGraph: Plain function with `RunnableConfig`

```python
from langchain_core.runnables import RunnableConfig

def analyzer_node(state: dict, config: RunnableConfig) -> dict:
    cfg = config["configurable"]               # runtime config injected by framework
    agent = AnalyzerAgent(cfg)
    updates = agent.run(state)                 # synchronous
    return updates                             # partial dict merged by LangGraph
```

### Microsoft Agent Framework: `@executor` decorator

```python
from agent_framework import executor, WorkflowContext

@executor(id="analyzer")
async def analyzer_node(state: BlockingState, context: WorkflowContext) -> None:
    cfg = get_config()                         # config from module singleton
    updates = await AnalyzerAgent(cfg).run(state)  # async
    state.llm_analysis  = updates["llm_analysis"]
    state.severity_hint = updates["severity_hint"]
    await context.send_message(state)          # explicitly forward state
```

**Key differences:**
- LangGraph nodes are plain Python functions; MAF nodes are decorated with `@executor(id=...)`.
- LangGraph injects config via `RunnableConfig`; MAF uses a module-level `get_config()` singleton.
- LangGraph nodes return a partial dict; MAF nodes call `context.send_message(state)`.
- MAF nodes must be `async def`; LangGraph nodes are typically synchronous (though async is supported).
- The `id` in `@executor(id="...")` must match the string IDs used in `FanOutEdgeGroup(source_id=...)` and `add_edge_group()`.

---

## 5. Conditional Routing

### LangGraph

```python
# router.py
def route_after_detection(state: BlockingAgentState) -> str:
    return "analyzer" if state.get("has_blocking") else "notification"

# workflow.py
g.add_conditional_edges("detection", route_after_detection,
                        {"analyzer": "analyzer", "notification": "notification"})
```

The mapping dict `{"analyzer": "analyzer", "notification": "notification"}` translates the router's string return value to a node name. This indirection exists to allow aliasing.

### Microsoft Agent Framework

```python
# router.py
def route_after_detection(state: BlockingState) -> list[str]:
    return ["analyzer"] if state.has_blocking else ["notification"]

# workflow.py
builder.add_edge_group(FanOutEdgeGroup(
    source_id="detection",
    target_ids=["analyzer", "notification"],
    selection_func=route_after_detection,    # returns list[str]
))
```

**Key differences:**
- LangGraph router returns a `str`; MAF `selection_func` returns a `list[str]` — enabling true fan-out to multiple executors in the same superstep.
- LangGraph requires a name-mapping dict; MAF's `target_ids` defines all possible targets with the selection function choosing among them.
- MAF's `FanOutEdgeGroup` can also support `FanInEdgeGroup` for merging parallel branches (not used here but available).
- `SwitchCaseEdgeGroup` is MAF's alternative for switch/case-style routing with explicit `Case(predicate, target_id)` objects.

---

## 6. Configuration Injection

### LangGraph

Config is passed per-invocation as `RunnableConfig` and injected by the framework into every node:

```python
# main.py
graph_config = {"configurable": config}
final = await AGENT_GRAPH.ainvoke(initial_state, config=graph_config)

# detector/agent.py
def detection_node(state: dict, config: RunnableConfig) -> dict:
    cfg = config["configurable"]     # available in every node automatically
```

### Microsoft Agent Framework

MAF has no equivalent of `RunnableConfig`. Config is stored in a module-level singleton after `load_config()`:

```python
# config.py
_ACTIVE_CONFIG: dict = {}

def load_config() -> dict:
    global _ACTIVE_CONFIG
    ...
    _ACTIVE_CONFIG = config
    return config

def get_config() -> dict:
    return _ACTIVE_CONFIG

# detector/agent.py
@executor(id="detection")
async def detection_node(state: BlockingState, context: WorkflowContext) -> None:
    cfg = get_config()      # single import, no framework injection
```

**Trade-offs:**
- LangGraph's `RunnableConfig` enables per-invocation config changes (useful for multi-tenant scenarios).
- MAF's singleton is simpler for single-server agents but requires restart to pick up config changes.
- Both allow DB-layer config overrides via `GlobalConfig` table.

---

## 7. Terminal Node & Output

### LangGraph

The terminal node is indicated by `g.add_edge("notification", END)`. The final state is whatever `ainvoke()` returns — the merged state after all nodes have run.

```python
# workflow.py
from langgraph.graph import END
g.add_edge("notification", END)

# main.py
final = await AGENT_GRAPH.ainvoke(initial_state, config=graph_config)
decision = final.get("decision", "SKIP")   # final is dict-like
```

### Microsoft Agent Framework

There is no `END` sentinel. The terminal node calls `context.yield_output()`:

```python
# notifier/agent.py
@executor(id="notification")
async def notification_node(state: BlockingState, context: WorkflowContext) -> None:
    ...
    await context.yield_output(state)    # exposes state as the workflow result

# main.py
final = await AGENT_WORKFLOW.run(initial_state)
decision = final.get("decision", "SKIP")   # final is BlockingState (has .get())
```

`BlockingState` keeps the `.get()` compatibility method so `main.py` code is identical in both versions.

---

## 8. Memory / Conversation History

### LangGraph

LangGraph `StateGraph` is stateless between invocations by default. History is managed explicitly via the `BlockingEventLog` / `KillAuditLog` SQL tables in `memory/long_term.py`.

### Microsoft Agent Framework

MAF provides `AgentThread` for built-in conversation history within a single session, and `AgentThreadManager` for persistence across sessions. This blocking agent doesn't require multi-turn conversation (each poll cycle is independent), so `AgentThread` is not used.

Long-term memory (recurrence detection, kill history) still uses the same `memory/long_term.py` SQL queries — unchanged between implementations.

---

## 9. Testing

### LangGraph tests

- Tests patch `BaseAgent.LLM` with a mock `ChatOpenAI` (synchronous).
- Node functions are called directly: `result = detection_node(state_dict, config)`.

### MAF tests

- Tests patch `BaseAgent.get_client()` with a mock `FoundryChatClient`.
- Executor functions must be called with `await`: `await detection_node(state, mock_context)`.
- `pytest-asyncio` is required for async tests.
- `WorkflowContext` must be mocked with `send_message` and `yield_output` coroutines.

---

## 10. Deployment & Auth

### LangGraph

```
LLM_API_KEY=sk-...           # any OpenAI-compatible key
LLM_BASE_URL=https://...     # any OpenAI-compatible endpoint
```

Works with OpenAI, Azure OpenAI (via compatibility layer), RouteLLM, Abacus AI, Ollama, etc. Pure API key auth.

### Microsoft Agent Framework

```
FOUNDRY_PROJECT_ENDPOINT=https://RESOURCE.services.ai.azure.com/api/projects/PROJECT
LLM_MODEL=gpt-4.1
```

Uses Azure credential chain:
1. `AzureKeyCredential(LLM_API_KEY)` — if key set
2. `DefaultAzureCredential` — managed identity in Azure, `az login` locally

Natively integrates with Azure RBAC, Key Vault, Azure Monitor. Better suited for enterprise Azure environments.

---

## 11. Async/Sync Bridge (MAF-specific)

One non-trivial difference arises from `tools/rca.py` calling `ask_llm_json` as a **synchronous callable**:

```python
# tools/rca.py (shared, unchanged)
def generate_rca(input: RCAInput, ask_llm_json: Callable[[str, str], dict]) -> RCAOutput:
    raw = ask_llm_json(_SYSTEM_PROMPT, user_msg)   # called synchronously
```

In the LangGraph version, `BaseAgent.ask_llm_json()` is synchronous (`LLM.invoke()`), so this works directly.

In the MAF version, `ask_llm_json()` is a coroutine — passing it to `generate_rca` would return a coroutine object instead of a dict.

**Solution:** `BaseAgent` adds `ask_llm_json_sync()` — a synchronous bridge that runs the async coroutine in a dedicated `ThreadPoolExecutor` with its own `asyncio.run()` event loop:

```python
# base_agent.py (MAF version only)
def ask_llm_json_sync(self, system_prompt: str, user_message: str) -> dict:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, self.ask_llm_json(system_prompt, user_message))
        return future.result()
```

`rca_node` then calls `generate_rca(rca_input, ask_llm_json=agent.ask_llm_json_sync)`.

This avoids `nest_asyncio` as a dependency and keeps `tools/rca.py` 100% unchanged.

---

## 12. Code Volume Comparison

| File | LangGraph | MAF | Delta |
|------|-----------|-----|-------|
| `main.py` | 122 lines | 95 lines | −27 (no graph_config boilerplate) |
| `orchestrator/workflow.py` | 46 lines | 43 lines | −3 |
| `orchestrator/state.py` | 101 lines | 103 lines | +2 (`.get()` helper) |
| `orchestrator/config.py` | 134 lines | 148 lines | +14 (get_config() singleton) |
| `agents/base_agent.py` | 152 lines | 118 lines | −34 (no LangChain message types) |
| `agents/detector/agent.py` | 373 lines | 355 lines | −18 (direct attribute access) |
| `agents/analyzer/agent.py` | 206 lines | 195 lines | −11 |
| `agents/determination/agent.py` | 256 lines | 248 lines | −8 |
| `agents/action/agent.py` | 99 lines | 93 lines | −6 |
| `agents/rca/agent.py` | 123 lines | 110 lines | −13 |
| `agents/notifier/agent.py` | 142 lines | 135 lines | −7 |
| **Total orchestration** | **~1754** | **~1643** | **−111 lines (−6%)** |
| Tools / memory / models | **identical** | **identical** | 0 |

---

## 13. Verdict — When to Use Each

### Choose LangGraph when:

- You need **multi-provider LLM flexibility** (OpenAI, Anthropic, Cohere, Ollama, RouteLLM).
- You are **not an Azure shop** or don't want Azure dependency.
- You prefer **open-source** with a large community and extensive documentation.
- You need **complex graph topologies** with cycles, interrupts, or human-in-the-loop (LangGraph Studio).
- Your team already uses the **LangChain ecosystem**.

### Choose Microsoft Agent Framework when:

- You are an **Azure-first organization** (uses Azure RBAC, Managed Identity, Key Vault).
- You use **Azure AI Foundry** for model governance and deployment.
- You want **Semantic Kernel + AutoGen** capabilities in a single SDK.
- You need **enterprise-grade observability** via Azure Monitor / Application Insights.
- You are building in a **Microsoft Copilot Studio** or M365 context.
- The **superstep BSP model** (true parallel fan-out) is important to your pipeline.

### For this blocking agent specifically:

Both frameworks produce **functionally equivalent implementations**. The business logic, SQL tools, prompt files, schemas, and memory layer are 100% shared — only the orchestration glue differs. The MAF version is **6% smaller** in orchestration code but requires Azure Foundry access. The LangGraph version is more portable and works with any OpenAI-compatible endpoint including the current RouteLLM/Abacus AI setup.
