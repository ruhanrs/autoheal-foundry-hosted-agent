"""Foundry hosting entrypoint for the LangGraph auto-heal workflow."""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

from azure.ai.agentserver.langgraph import from_langgraph

from autoheal.github import create_repository_client

from .graph import build_autoheal_graph
from .planner import FoundryResponsesPlanner

load_dotenv(override=False)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

_github = create_repository_client()
_planner = FoundryResponsesPlanner()
_graph = build_autoheal_graph(github=_github, planner=_planner)
_adapter = from_langgraph(_graph)


def main() -> None:
    _adapter.run()


if __name__ == "__main__":
    main()
