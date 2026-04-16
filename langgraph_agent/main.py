"""CLI runner for the LangGraph auto-heal workflow."""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

from autoheal.github import create_repository_client

from .graph import build_autoheal_graph
from .planner import FoundryResponsesPlanner

load_dotenv(override=False)


async def run_once(raw_input: str) -> str:
    github = create_repository_client()
    planner = FoundryResponsesPlanner()
    try:
        graph = build_autoheal_graph(github=github, planner=planner)
        result = await graph.ainvoke({"raw_input": raw_input})
        return str(result.get("final_result", "No final result produced."))
    finally:
        await github.close()
        await planner.close()


async def _main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    raw_input = os.environ.get("AUTOHEAL_INPUT", "")
    if not raw_input:
        raise SystemExit("Set AUTOHEAL_INPUT to a pipeline failure message.")
    print(await run_once(raw_input))


if __name__ == "__main__":
    asyncio.run(_main())
