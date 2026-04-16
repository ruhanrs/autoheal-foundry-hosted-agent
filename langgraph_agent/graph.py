"""Graph construction for the LangGraph auto-heal workflow."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from autoheal.github import RepositoryClient

from .nodes import AutoHealNodes, FixPlanner, should_apply_fixes
from .state import AutoHealState


def build_autoheal_graph(github: RepositoryClient, planner: FixPlanner):
    nodes = AutoHealNodes(github=github, planner=planner)

    graph = StateGraph(AutoHealState)
    graph.add_node("parse_input", nodes.parse_input)
    graph.add_node("gather_context", nodes.gather_context)
    graph.add_node("plan_fix", nodes.plan_fix)
    graph.add_node("apply_fixes", nodes.apply_fixes)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "parse_input")
    graph.add_edge("parse_input", "gather_context")
    graph.add_edge("gather_context", "plan_fix")
    graph.add_conditional_edges(
        "plan_fix",
        should_apply_fixes,
        {
            "apply_fixes": "apply_fixes",
            "finalize": "finalize",
        },
    )
    graph.add_edge("apply_fixes", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
