"""Auto-Heal CI/CD Agent — Microsoft Foundry hosted-agent entry point.

The agent inspects pipeline failure logs, opens (or reuses) an auto-heal
branch, commits a minimal fix, and returns the resulting Pull Request.
GitHub access is performed by local Python tools using the REST API; no
external MCP server is required.
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

from agent_framework import Agent
from agent_framework.azure import AzureAIAgentClient
from azure.ai.agentserver.agentframework import from_agent_framework
from azure.identity.aio import DefaultAzureCredential

from autoheal.instructions import AGENT_INSTRUCTIONS
from autoheal.github_tools import (
    create_branch,
    create_or_update_file,
    create_pull_request,
    get_file_contents,
    get_pull_request,
    list_branches,
    list_pull_requests,
    validate_input,
)


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("autoheal")


PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4.1")

REQUIRED_ENV_VARS = ["PROJECT_ENDPOINT", "GITHUB_TOKEN"]


def _validate_env() -> None:
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)


async def main() -> None:
    _validate_env()
    async with (
        DefaultAzureCredential() as credential,
        AzureAIAgentClient(
            project_endpoint=PROJECT_ENDPOINT,
            model_deployment_name=MODEL_DEPLOYMENT_NAME,
            credential=credential,
        ) as client,
    ):
        agent = Agent(
            client,
            name="AutoHealAgent",
            instructions=AGENT_INSTRUCTIONS,
            tools=[
                validate_input,
                get_file_contents,
                list_branches,
                create_branch,
                create_or_update_file,
                create_pull_request,
                list_pull_requests,
                get_pull_request,
            ],
        )

        logger.info("Auto-Heal Agent server running on http://localhost:8088")
        server = from_agent_framework(agent)
        await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())
