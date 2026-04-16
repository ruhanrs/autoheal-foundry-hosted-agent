"""
Auto-heal hosted agent entry point.

This module exposes a proper Foundry hosted-agent HTTP server. Incoming user
input is handled by a lightweight top-level agent, which then runs the existing
two-phase auto-heal pipeline inside the request:

Phase 1 (Context Gatherer)
  The model parses the failure input and invokes `gather_context` once.

Phase 2 (Fix Applier)
  The model reads the captured context and invokes `apply_fixes` once.

Why two phases?
  azure-ai-agentserver-agentframework 1.0.0b16 executes exactly one model turn
  per run call and does not continue after tool outputs. Splitting the workflow
  into two focused model runs keeps the MCP-backed GitHub interactions wrapped
  inside single high-leverage tools.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
import sys
import time
import uuid
from typing import Any, AsyncIterable

from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv(override=False)

from agent_framework import AgentResponse, AgentResponseUpdate, BaseAgent, ChatMessage
from agent_framework.azure import AzureAIAgentClient
from azure.ai.agentserver.agentframework import from_agent_framework

from autoheal.github import create_repository_client
from autoheal.instructions import APPLY_FIX_TEMPLATE, CONTEXT_GATHER_INSTRUCTIONS
from autoheal.tools import create_apply_tools, create_context_tools


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


PHASE1_TIMEOUT = int(os.getenv("PHASE1_TIMEOUT_SECONDS", "120"))
PHASE2_TIMEOUT = int(os.getenv("PHASE2_TIMEOUT_SECONDS", "180"))
MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES", "2"))


def _extract_input_text(messages: Any) -> str:
    def _collect(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, ChatMessage):
            text = (value.text or "").strip()
            if text:
                return [text]
            return _collect(getattr(value, "contents", None))
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                parts.extend(_collect(item))
            return parts
        if isinstance(value, dict):
            parts: list[str] = []
            for key in ("input", "text", "content", "contents", "messages"):
                if key in value:
                    parts.extend(_collect(value.get(key)))
            if parts:
                return parts
        text = str(value).strip()
        return [text] if text else []

    return "\n".join(_collect(messages))


async def _run_phase(phase_coro, phase_name: str, timeout: int) -> Any:
    start = time.monotonic()
    logger.info("%s starting (timeout=%ds).", phase_name, timeout)
    try:
        result = await asyncio.wait_for(phase_coro, timeout=timeout)
        logger.info("%s completed in %.2fs.", phase_name, time.monotonic() - start)
        return result
    except asyncio.TimeoutError:
        logger.error("%s timed out after %.2fs.", phase_name, time.monotonic() - start)
        return None
    except Exception:
        logger.exception("%s raised an unexpected exception.", phase_name)
        return None


async def _run_autoheal_pipeline(raw_input: str) -> str:
    github = create_repository_client()
    model = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-4.1")
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]

    final_result = "Automatic fix could not be safely determined."

    try:
        async with (
            DefaultAzureCredential() as credential,
            AzureAIAgentClient(
                project_endpoint=endpoint,
                model_deployment_name=model,
                credential=credential,
            ) as ai_client,
        ):
            for attempt in range(1, MAX_RETRIES + 2):
                logger.info("=== Phase 1 attempt %d/%d ===", attempt, MAX_RETRIES + 1)
                context_container: list[str] = []
                context_tools = create_context_tools(github, context_container)

                async with ai_client.as_agent(
                    name="context-gatherer",
                    instructions=CONTEXT_GATHER_INSTRUCTIONS,
                    tools=context_tools,
                ) as phase1_agent:
                    phase1_result = await _run_phase(
                        phase1_agent.run(raw_input),
                        "Phase 1 (context)",
                        PHASE1_TIMEOUT,
                    )

                if context_container:
                    logger.info("Phase 1 succeeded — context captured.")
                    break

                logger.warning(
                    "Phase 1 produced no context (attempt %d). Agent result=%r",
                    attempt,
                    phase1_result,
                )
                if attempt > MAX_RETRIES:
                    logger.error("Phase 1 failed after all retries. Aborting.")
                    return final_result

            context_block = context_container[0]
            apply_instructions = APPLY_FIX_TEMPLATE.format(context=context_block)

            for attempt in range(1, MAX_RETRIES + 2):
                logger.info("=== Phase 2 attempt %d/%d ===", attempt, MAX_RETRIES + 1)
                result_container: list[str] = []
                apply_tools = create_apply_tools(github, result_container, context_block)

                async with ai_client.as_agent(
                    name="fix-applier",
                    instructions=apply_instructions,
                    tools=apply_tools,
                ) as phase2_agent:
                    phase2_result = await _run_phase(
                        phase2_agent.run("Analyze the provided context and call apply_fixes."),
                        "Phase 2 (apply)",
                        PHASE2_TIMEOUT,
                    )

                if result_container:
                    final_result = result_container[0]
                    logger.info("Phase 2 succeeded — result captured.")
                    break

                logger.warning(
                    "Phase 2 produced no result (attempt %d). Agent result=%r",
                    attempt,
                    phase2_result,
                )
                if attempt > MAX_RETRIES:
                    logger.error("Phase 2 failed after all retries.")
                    break

    finally:
        await github.close()

    logger.info(
        "FINAL RESPONSE:\n%s\n%s\n%s", "=" * 44, final_result, "=" * 44
    )
    return final_result


class AutoHealHostedAgent(BaseAgent):
    """Top-level hosted agent that runs the auto-heal pipeline per request."""

    def __init__(self) -> None:
        super().__init__(
            name="auto-heal-agent",
            description="Diagnoses CI/CD failures and creates GitHub remediation PRs.",
        )

    async def run(self, messages=None, *, thread=None, **kwargs) -> AgentResponse:
        raw_input = _extract_input_text(messages)
        if not raw_input:
            text = "No pipeline failure input was provided."
        else:
            text = await _run_autoheal_pipeline(raw_input)

        response_id = f"autoheal-{uuid.uuid4()}"
        return AgentResponse(
            messages=[ChatMessage(role="assistant", text=text)],
            response_id=response_id,
        )

    def run_stream(self, messages=None, *, thread=None, **kwargs) -> AsyncIterable[AgentResponseUpdate]:
        async def _stream() -> AsyncIterable[AgentResponseUpdate]:
            response = await self.run(messages=messages, thread=thread, **kwargs)
            response_text = _extract_input_text(getattr(response, "messages", None))
            yield AgentResponseUpdate(
                role="assistant",
                text=response_text,
                response_id=response.response_id,
            )

        return _stream()


if __name__ == "__main__":
    _validate_env()
    hosted_agent = AutoHealHostedAgent()
    from_agent_framework(hosted_agent).run()
