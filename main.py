"""
Auto-heal hosted agent — two-phase pipeline entry point.

Phase 1 (Context Gatherer)
  Agent is given one read-only tool: gather_context. The model only needs to
  parse the input and invoke that tool once.

Phase 2 (Fix Applier)
  Agent is given one write-only tool: apply_fixes. The Phase 1 context is
  injected into the system prompt, and the tool performs the branch, commit,
  PR, and final result operations in Python.

Why two phases?
  azure-ai-agentserver-agentframework 1.0.0b16 executes exactly one model turn
  per run_async() call and does not feed tool results back for a continuation
  turn. Each phase therefore exposes a single high-leverage tool so the
  workflow no longer depends on multi-step tool chaining in one turn.
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

from agent_framework.azure import AzureAIClient
from azure.ai.agentserver.agentframework import from_agent_framework

from autoheal.github import GitHubClient
from autoheal.instructions import APPLY_FIX_TEMPLATE, CONTEXT_GATHER_INSTRUCTIONS
from autoheal.tools import create_apply_tools, create_context_tools

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

# Phase 1 only needs to make 2-3 read calls — 120 s is generous.
PHASE1_TIMEOUT = int(os.getenv("PHASE1_TIMEOUT_SECONDS", "120"))
# Phase 2 needs to write a file, create a branch, and open a PR — 180 s.
PHASE2_TIMEOUT = int(os.getenv("PHASE2_TIMEOUT_SECONDS", "180"))
MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES", "2"))

# ── Graceful shutdown ──────────────────────────────────────────────────────────

def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _handle(sig: signal.Signals) -> None:
        logger.info("Received %s — shutting down.", sig.name)
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)


# ── Phase runner ───────────────────────────────────────────────────────────────

async def _run_phase(runner, phase_name: str, timeout: int) -> str | None:
    """Execute one agent phase with timeout protection."""
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


# ── Main pipeline ──────────────────────────────────────────────────────────────

async def main() -> None:
    _validate_env()

    github = GitHubClient()
    credential = DefaultAzureCredential()
    model = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-4.1")
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]

    final_result = "Automatic fix could not be safely determined."

    try:
        # ── Phase 1: Context Gathering ────────────────────────────────────────
        # Goal: the model parses the input and calls gather_context once.
        # The gather_context tool writes into context_container[0].

        for attempt in range(1, MAX_RETRIES + 2):
            logger.info("=== Phase 1 attempt %d/%d ===", attempt, MAX_RETRIES + 1)
            context_container: list[str] = []
            context_tools = create_context_tools(github, context_container)

            async with AzureAIClient(
                project_endpoint=endpoint,
                model_deployment_name=model,
                credential=credential,
            ).as_agent(
                name="context-gatherer",
                instructions=CONTEXT_GATHER_INSTRUCTIONS,
                tools=context_tools,
            ) as phase1_agent:

                phase1_runner = from_agent_framework(phase1_agent)
                phase1_result = await _run_phase(
                    phase1_runner,
                    "Phase 1 (context)",
                    PHASE1_TIMEOUT,
                )

            if context_container:
                logger.info("Phase 1 succeeded — context captured.")
                break

            logger.warning(
                "Phase 1 produced no context (attempt %d). Runner result=%r",
                attempt,
                phase1_result,
            )
            if attempt > MAX_RETRIES:
                logger.error("Phase 1 failed after all retries. Aborting.")
                return

        context_block = context_container[0]
        os.environ["AUTOHEAL_PHASE2_CONTEXT"] = context_block

        # ── Phase 2: Fix Application ──────────────────────────────────────────
        # Goal: read the pre-fetched JSON context from system instructions,
        # generate the fix, and call apply_fixes once. The tool writes the
        # final result into result_container[0].

        apply_instructions = APPLY_FIX_TEMPLATE.format(context=context_block)

        for attempt in range(1, MAX_RETRIES + 2):
            logger.info("=== Phase 2 attempt %d/%d ===", attempt, MAX_RETRIES + 1)
            result_container: list[str] = []
            apply_tools = create_apply_tools(github, result_container)

            async with AzureAIClient(
                project_endpoint=endpoint,
                model_deployment_name=model,
                credential=credential,
            ).as_agent(
                name="fix-applier",
                instructions=apply_instructions,
                tools=apply_tools,
            ) as phase2_agent:

                phase2_runner = from_agent_framework(phase2_agent)
                phase2_result = await _run_phase(
                    phase2_runner,
                    "Phase 2 (apply)",
                    PHASE2_TIMEOUT,
                )

            if result_container:
                final_result = result_container[0]
                logger.info("Phase 2 succeeded — result captured.")
                break

            logger.warning(
                "Phase 2 produced no result (attempt %d). Runner result=%r",
                attempt,
                phase2_result,
            )
            if attempt > MAX_RETRIES:
                logger.error("Phase 2 failed after all retries.")
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
