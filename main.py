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
            "stderr": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "standard",
                "level": "ERROR",
            },
        },
        "root": {
            "level": log_level,
            "handlers": ["stdout"],
        },
        "loggers": {
            "autoheal": {"level": log_level, "propagate": True},
            "__main__": {"level": log_level, "propagate": True},
        },
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
    """Fail fast at startup if any required variable is missing."""
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)
    logger.info("Environment validation passed.")

# ── Config ─────────────────────────────────────────────────────────────────────

AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "300"))
MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES", "2"))

# ── Graceful shutdown ──────────────────────────────────────────────────────────

_shutdown_event = asyncio.Event()

def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGTERM and SIGINT handlers for graceful shutdown."""
    def _handle(sig: signal.Signals) -> None:
        logger.info("Received signal %s — initiating graceful shutdown.", sig.name)
        loop.call_soon_threadsafe(_shutdown_event.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)

# ── Agent execution ────────────────────────────────────────────────────────────

async def run_agent_once(runner) -> str | None:
    """Run agent once with timeout protection."""
    start = time.monotonic()
    try:
        logger.info("Starting agent execution (timeout=%ds).", AGENT_TIMEOUT_SECONDS)
        result = await asyncio.wait_for(
            runner.run_async(),
            timeout=AGENT_TIMEOUT_SECONDS,
        )
        elapsed = round(time.monotonic() - start, 2)
        logger.info("Agent completed in %.2fs.", elapsed)
        return result
    except asyncio.TimeoutError:
        elapsed = round(time.monotonic() - start, 2)
        logger.error("Agent timed out after %.2fs (limit=%ds).", elapsed, AGENT_TIMEOUT_SECONDS)
        return None
    except Exception:
        logger.exception("Agent execution raised an unexpected exception.")
        return None


def validate_result(result: str | None) -> str:
    """Ensure agent returns a valid final response."""
    if not result:
        return "Automatic fix could not be safely determined."

    result_str = str(result).strip()
    if not result_str:
        return "Automatic fix could not be safely determined."

    if "Root Cause:" not in result_str:
        logger.warning("Agent output missing required 'Root Cause:' block. Treating as invalid.")
        return "Automatic fix could not be safely determined."

    return result_str


async def main() -> None:
    _validate_env()

    github = GitHubClient()
    tools = create_tools(github)
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
                logger.info("Agent attempt %d/%d", attempt, MAX_RETRIES + 1)

                result = await run_agent_once(runner)
                validated = validate_result(result)

                if validated != "Automatic fix could not be safely determined.":
                    final_result = validated
                    break

                if attempt <= MAX_RETRIES:
                    logger.warning("Invalid result on attempt %d. Retrying...", attempt)

            if not final_result:
                final_result = "Automatic fix could not be safely determined."

            logger.info("FINAL RESPONSE:\n%s\n%s\n%s", "=" * 44, final_result, "=" * 44)

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
