"""
agent_framework compatibility shim
====================================
Implements the Microsoft Agent Framework 1.0 API surface used by this project,
backed by the openai SDK.  Allows the MAF-style code to run without the real
agent-framework-foundry PyPI package (which requires Azure AI Foundry preview
access).

APIs provided:
  WorkflowContext   -- async context passed to each @executor function
  executor(id)      -- decorator tagging a function with an executor ID
  FanOutEdgeGroup   -- conditional routing rule (replaces add_conditional_edges)
  WorkflowBuilder   -- builds the workflow topology from executor + edge defs
  Workflow          -- compiled, runnable workflow returned by builder.build()
  ChatAgent         -- single-turn LLM chat wrapper (used in base_agent.py)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("agent_framework")


# ── WorkflowContext ────────────────────────────────────────────────────────────

class WorkflowContext:
    """
    Passed to every @executor function.

    send_message(state)  -- forward updated state to the next node(s).
    yield_output(state)  -- mark as terminal; state is returned by Workflow.run().
    """

    def __init__(self):
        self._next_state: Any = None
        self._output: Any = None
        self._terminal: bool = False

    async def send_message(self, state: Any) -> None:
        self._next_state = state

    async def yield_output(self, state: Any) -> None:
        self._output = state
        self._terminal = True


# ── executor decorator ─────────────────────────────────────────────────────────

def executor(id: str):
    """Decorator that tags an async function as an executor with the given ID."""
    def decorator(fn):
        fn._executor_id = id
        return fn
    return decorator


# ── FanOutEdgeGroup ────────────────────────────────────────────────────────────

@dataclass
class FanOutEdgeGroup:
    """
    After the source executor runs, call selection_func(state) -> list[str]
    to decide which target executor(s) to invoke next.
    """
    source_id: str
    target_ids: list[str]
    selection_func: Callable


# ── WorkflowBuilder / Workflow ─────────────────────────────────────────────────

class Workflow:
    """Compiled workflow.  Call: final_state = await workflow.run(initial_state)"""

    def __init__(
        self,
        start_id: str,
        executors: dict[str, Any],
        edges: dict[str, list[str]],
        edge_groups: dict[str, FanOutEdgeGroup],
    ):
        self._start_id = start_id
        self._executors = executors
        self._edges = edges
        self._edge_groups = edge_groups

    def _next_ids(self, executor_id: str, state: Any) -> list[str]:
        if executor_id in self._edge_groups:
            return self._edge_groups[executor_id].selection_func(state)
        return self._edges.get(executor_id, [])

    async def run(self, initial_state: Any) -> Any:
        state = initial_state
        current_id = self._start_id

        while current_id:
            fn = self._executors.get(current_id)
            if fn is None:
                raise RuntimeError(
                    f"agent_framework: unknown executor id={current_id!r}"
                )

            log.debug("executor → %s", current_id)
            ctx = WorkflowContext()
            await fn(state, ctx)

            if ctx._terminal:
                return ctx._output

            if ctx._next_state is not None:
                state = ctx._next_state

            next_ids = self._next_ids(current_id, state)
            if not next_ids:
                log.warning("No outgoing edge from executor %r — stopping.", current_id)
                break

            # Routing functions return exactly one target in this graph
            current_id = next_ids[0]

        return state


class WorkflowBuilder:
    """Assembles a Workflow from executor functions and edge/routing rules."""

    def __init__(self, start_executor):
        self._start_id: str = start_executor._executor_id
        self._executors: dict[str, Any] = {}
        self._edges: dict[str, list[str]] = {}
        self._edge_groups: dict[str, FanOutEdgeGroup] = {}
        self._register(start_executor)

    def _register(self, fn):
        eid = getattr(fn, "_executor_id", None)
        if eid:
            self._executors[eid] = fn

    def add_edge(self, source, target) -> "WorkflowBuilder":
        self._register(source)
        self._register(target)
        src_id = source._executor_id
        tgt_id = target._executor_id
        self._edges.setdefault(src_id, []).append(tgt_id)
        return self

    def add_edge_group(self, group: FanOutEdgeGroup) -> "WorkflowBuilder":
        self._edge_groups[group.source_id] = group
        return self

    def build(self) -> Workflow:
        return Workflow(
            start_id=self._start_id,
            executors=self._executors,
            edges=self._edges,
            edge_groups=self._edge_groups,
        )


# ── ChatAgent ──────────────────────────────────────────────────────────────────

class _ChatResult:
    def __init__(self, content: str):
        self.content = content

    def __str__(self) -> str:
        return self.content


class ChatAgent:
    """
    Thin async wrapper around a FoundryChatClient for single-turn chat.

    Usage (as in base_agent.py):
        agent = ChatAgent(chat_client=client, instructions=system_prompt,
                          temperature=0, max_tokens=4096)
        result = await agent.run(user_message)
        text = result.content
    """

    def __init__(
        self,
        chat_client,
        instructions: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ):
        self._client = chat_client
        self._instructions = instructions
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def run(self, user_message: str) -> _ChatResult:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None,
            self._call_sync,
            user_message,
        )
        return _ChatResult(raw)

    def _call_sync(self, user_message: str) -> str:
        return self._client.complete(
            system_prompt=self._instructions,
            user_message=user_message,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
