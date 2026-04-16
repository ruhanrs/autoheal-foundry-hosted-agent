"""
Auto-heal hosted agent — single-tool pipeline entry point.

The outer model has exactly one tool, `run_autoheal_pipeline`, which performs
every GitHub read, fix generation, and GitHub write inside Python. This keeps
the workflow inside a single model turn, matching the hosted-agent runtime's
one-turn-per-run semantics.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
import signal
import sys
import time

from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv(override=False)

from agent_framework.azure import AzureAIAgentClient
from azure.ai.agentserver.agentframework import from_agent_framework

from autoheal.github import create_repository_client
from autoheal.instructions import PIPELINE_INSTRUCTIONS
from autoheal.tools import create_pipeline_tool


def _configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%SZ",
            }
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "standard",
            },
        },
        "root": {"level": log_level, "handlers": ["stdout"]},
    })


_configure_logging()
logger = logging.getLogger(__name__)

_REQUIRED_ENV_VARS = [
    "FOUNDRY_PROJECT_ENDPOINT",
    "GITHUB_REPO_OWNER",
    "GITHUB_REPO_NAME",
    "GITHUB_MCP_CONNECTION_ID",
]


def _validate_env() -> None:
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)
    logger.info("Environment validation passed.")


AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT_SECONDS") or "240")
MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES") or "2")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _handle(sig: signal.Signals) -> None:
        logger.info("Received %s — shutting down.", sig.name)
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle, sig)
        except NotImplementedError:
            # add_signal_handler is POSIX-only; skip on platforms that lack it.
            pass


async def _run_phase(runner, phase_name: str, timeout: int):
    start = time.monotonic()
    logger.info("%s starting (timeout=%ds).", phase_name, timeout)
    try:
        result = await asyncio.wait_for(runner.run_async(), timeout=timeout)
        logger.info("%s completed in %.2fs.", phase_name, time.monotonic() - start)
        return result
    except asyncio.TimeoutError:
        logger.error("%s timed out after %.2fs.", phase_name, time.monotonic() - start)
        return None
    except Exception:
        logger.exception("%s raised an unexpected exception.", phase_name)
        return None


async def main() -> None:
    _validate_env()

    github = create_repository_client()
    credential = DefaultAzureCredential()
    model = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-4.1")
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]

    final_result = "Automatic fix could not be safely determined."

    try:
        for attempt in range(1, MAX_RETRIES + 2):
            logger.info("=== Pipeline attempt %d/%d ===", attempt, MAX_RETRIES + 1)
            result_container: list[str] = []

            ai_client = AzureAIAgentClient(
                project_endpoint=endpoint,
                model_deployment_name=model,
                credential=credential,
            )
            tools = create_pipeline_tool(github, ai_client, result_container)

            async with ai_client.as_agent(
                name="auto-heal-agent",
                instructions=PIPELINE_INSTRUCTIONS,
                tools=tools,
            ) as agent:
                runner = from_agent_framework(agent)
                await _run_phase(runner, "auto-heal pipeline", AGENT_TIMEOUT)

            if result_container:
                final_result = result_container[0]
                logger.info("Pipeline succeeded — result captured.")
                break

            logger.warning("Pipeline produced no result (attempt %d).", attempt)
            if attempt > MAX_RETRIES:
                logger.error("Pipeline failed after all retries.")
                break

    finally:
        await github.close()
        await credential.close()

    logger.info(
        "FINAL RESPONSE:\n%s\n%s\n%s", "=" * 44, final_result, "=" * 44
    )


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    except Exception:
        logger.exception("Unhandled exception in main.")
        sys.exit(1)
    finally:
        loop.close()
