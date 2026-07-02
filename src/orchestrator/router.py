"""
src/orchestrator/router.py
Routing functions that determine which node runs next in the workflow.
Identical logic to the LangGraph version -- used as selection_func
in MAF FanOutEdgeGroup.
"""

from orchestrator.state import BlockingState


def route_after_detection(state: BlockingState) -> list[str]:
    return ["analyzer"] if state.get("has_blocking") else ["notification"]


def route_after_determination(state: BlockingState) -> list[str]:
    return ["action"] if state.get("decision") == "KILL" else ["rca"]
