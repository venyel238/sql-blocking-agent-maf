"""
src/orchestrator/workflow.py
DAG/workflow definition using Microsoft Agent Framework WorkflowBuilder.
Wires all executor nodes together and exports AGENT_WORKFLOW -- the compiled
MAF workflow that replaces AGENT_GRAPH (LangGraph StateGraph).

Routing logic is identical to router.py; FanOutEdgeGroup replaces
LangGraph's add_conditional_edges.
"""

from agent_framework import WorkflowBuilder, FanOutEdgeGroup

from orchestrator.router import route_after_detection, route_after_determination

from agents.detector.agent     import detection_node
from agents.analyzer.agent     import analyzer_node
from agents.determination.agent import determination_node
from agents.action.agent       import action_node
from agents.rca.agent          import rca_node
from agents.notifier.agent     import notification_node


def build_workflow():
    builder = WorkflowBuilder(start_executor=detection_node)

    # detection → analyzer (if blocking) | notification (if no blocking)
    builder.add_edge_group(FanOutEdgeGroup(
        source_id="detection",
        target_ids=["analyzer", "notification"],
        selection_func=route_after_detection,
    ))

    # analyzer → determination (always)
    builder.add_edge(analyzer_node, determination_node)

    # determination → action (if KILL) | rca (otherwise)
    builder.add_edge_group(FanOutEdgeGroup(
        source_id="determination",
        target_ids=["action", "rca"],
        selection_func=route_after_determination,
    ))

    # action → rca → notification
    builder.add_edge(action_node, rca_node)
    builder.add_edge(rca_node, notification_node)

    return builder.build()


# Compiled once at import time — reused every cycle
AGENT_WORKFLOW = build_workflow()
