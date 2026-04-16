"""Auto-heal hosted agent entry point."""

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

from agent_framework.azure import AzureAIClient
from azure.ai.agentserver.agentframework import from_agent_framework

from autoheal.github import GitHubClient
from autoheal.instructions import SYSTEM_INSTRUCTIONS
from autoheal.tools import create_tools

# ── Logging ───────────────────────────────────────────────────────────────────

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

# ── Required environment variables ────────────────────────────────────────────

_REQUIRED_ENV_VARS = [
    "FOUNDRY_PROJECT_ENDPOINT",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_REPO_OWNER",
    "GITHUB_REPO_NAME",
]

def _validate_env() -> None:
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)
    logger.info("Environment validation passed.")

# ── Config ─────────────────────────────────────────────────────────────────────

AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "300"))
MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES", "2"))

# ── Graceful shutdown ──────────────────────────────────────────────────────────

def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _handle(sig: signal.Signals) -> None:
        logger.info("Received %s — shutting down.", sig.name)
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)

# ── Agent execution ────────────────────────────────────────────────────────────

async def run_agent_once(runner) -> str | None:
    start = time.monotonic()
    try:
        logger.info("Starting agent execution (timeout=%ds).", AGENT_TIMEOUT_SECONDS)
        result = await asyncio.wait_for(runner.run_async(), timeout=AGENT_TIMEOUT_SECONDS)
        logger.info("Agent completed in %.2fs.", time.monotonic() - start)
        return result
    except asyncio.TimeoutError:
        logger.error("Agent timed out after %.2fs.", time.monotonic() - start)
        return None
    except Exception:
        logger.exception("Agent execution raised an unexpected exception.")
        return None


def _extract_result(run_result: str | None, result_container: list) -> str:
    """
    Return the best available result, checking sources in priority order:

    1. result_container  — populated when the model called report_final_result
       (works even when run_async() returns no text, which is a known limitation
       of azure-ai-agentserver-agentframework 1.0.0b16 where the SDK only
       executes one model turn without feeding tool results back for continuation)
    2. run_result text   — standard path when the SDK does return text
    3. Fallback message  — nothing worked
    """
    # Priority 1: captured via report_final_result tool call
    if result_container:
        captured = result_container[0]
        logger.info("Using result captured from report_final_result tool call.")
        return captured

    # Priority 2: text returned directly from run_async()
    if run_result:
        text = str(run_result).strip()
        if text and "Root Cause:" in text:
            logger.info("Using text result returned by run_async().")
            return text
        logger.warning("run_async() returned text but it lacks 'Root Cause:' — treating as invalid.")

    return "Automatic fix could not be safely determined."


async def main() -> None:
    _validate_env()

    # Shared capture cell — report_final_result writes here, _extract_result reads here.
    result_container: list[str] = []

    github = GitHubClient()
    tools = create_tools(github, result_container)
    credential = DefaultAzureCredential()

    try:
        async with AzureAIClient(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            model_deployment_name=os.environ.get("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-4.1"),
            credential=credential,
        ).as_agent(
            name="auto-heal-agent",
            instructions=SYSTEM_INSTRUCTIONS,
            tools=tools,
        ) as agent:

            runner = from_agent_framework(agent)

            attempt = 0
            final_result: str | None = None

            while attempt <= MAX_RETRIES:
                attempt += 1
                result_container.clear()   # reset capture cell for each attempt
                logger.info("Agent attempt %d/%d", attempt, MAX_RETRIES + 1)

                run_result = await run_agent_once(runner)
                candidate = _extract_result(run_result, result_container)

                if candidate != "Automatic fix could not be safely determined.":
                    final_result = candidate
                    break

                if attempt <= MAX_RETRIES:
                    logger.warning("No valid result on attempt %d — retrying.", attempt)

            if not final_result:
                final_result = "Automatic fix could not be safely determined."

            logger.info(
                "FINAL RESPONSE:\n%s\n%s\n%s", "=" * 44, final_result, "=" * 44
            )

    finally:
        await github.close()
        await credential.close()


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
