"""Model-backed planner for the LangGraph auto-heal workflow."""

from __future__ import annotations

import logging
import os

from azure.identity.aio import DefaultAzureCredential
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class FoundryResponsesPlanner:
    """Call a Foundry model deployment through the OpenAI Responses API."""

    def __init__(
        self,
        *,
        project_endpoint: str | None = None,
        model_deployment_name: str | None = None,
        credential: DefaultAzureCredential | None = None,
    ) -> None:
        self.project_endpoint = (
            project_endpoint or os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        ).rstrip("/")
        self.model_deployment_name = (
            model_deployment_name
            or os.environ.get("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-4.1")
        )
        self.credential = credential or DefaultAzureCredential()

    async def ainvoke(self, prompt: str) -> str:
        token = await self.credential.get_token("https://ai.azure.com/.default")
        client = AsyncOpenAI(
            base_url=f"{self.project_endpoint}/openai/v1",
            api_key=token.token,
        )
        try:
            response = await client.responses.create(
                model=self.model_deployment_name,
                input=prompt,
            )
            output_text = getattr(response, "output_text", None)
            if output_text:
                return output_text
            return response.model_dump_json(indent=2)
        finally:
            await client.close()

    async def close(self) -> None:
        await self.credential.close()
