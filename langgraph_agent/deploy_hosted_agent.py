"""SDK deployment helper for the LangGraph hosted agent."""

from __future__ import annotations

import os

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentProtocol, HostedAgentDefinition, ProtocolVersionRecord


def _build_tools() -> list[dict[str, str]]:
    tools: list[dict[str, str]] = []

    github_connection_id = os.environ.get("GITHUB_MCP_CONNECTION_ID", "").strip()
    if github_connection_id:
        tools.append(
            {
                "type": "mcp",
                "project_connection_id": github_connection_id,
            }
        )

    extra_connection_ids = os.environ.get("EXTRA_MCP_CONNECTION_IDS", "").strip()
    if extra_connection_ids:
        for connection_id in extra_connection_ids.split(","):
            normalized = connection_id.strip()
            if normalized:
                tools.append(
                    {
                        "type": "mcp",
                        "project_connection_id": normalized,
                    }
                )

    return tools


def main() -> None:
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    agent_name = os.environ.get("HOSTED_AGENT_NAME", "autoheal-langgraph-agent")
    image = os.environ["HOSTED_AGENT_IMAGE"]
    tools = _build_tools()

    client = AIProjectClient(
        endpoint=project_endpoint,
        credential=DefaultAzureCredential(),
    )

    definition = HostedAgentDefinition(
        container_protocol_versions=[
            ProtocolVersionRecord(protocol=AgentProtocol.RESPONSES, version="v1")
        ],
        image=image,
        cpu="1",
        memory="2Gi",
        tools=tools,
        environment_variables={
            "FOUNDRY_PROJECT_ENDPOINT": project_endpoint,
            "FOUNDRY_MODEL_DEPLOYMENT_NAME": os.environ.get(
                "FOUNDRY_MODEL_DEPLOYMENT_NAME",
                "gpt-4.1",
            ),
            "GITHUB_APP_ID": os.environ["GITHUB_APP_ID"],
            "GITHUB_APP_INSTALLATION_ID": os.environ["GITHUB_APP_INSTALLATION_ID"],
            "GITHUB_APP_PRIVATE_KEY": os.environ["GITHUB_APP_PRIVATE_KEY"],
            "GITHUB_REPO_OWNER": os.environ["GITHUB_REPO_OWNER"],
            "GITHUB_REPO_NAME": os.environ["GITHUB_REPO_NAME"],
            "USE_GITHUB_MCP": os.environ.get("USE_GITHUB_MCP", "false"),
            "GITHUB_MCP_URL": os.environ.get("GITHUB_MCP_URL", ""),
            "GITHUB_MCP_COMMAND": os.environ.get("GITHUB_MCP_COMMAND", ""),
            "GITHUB_MCP_ARGS_JSON": os.environ.get("GITHUB_MCP_ARGS_JSON", "[]"),
            "GITHUB_MCP_ENV_JSON": os.environ.get("GITHUB_MCP_ENV_JSON", "{}"),
            "GITHUB_MCP_HEADERS_JSON": os.environ.get("GITHUB_MCP_HEADERS_JSON", "{}"),
            "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO"),
        },
    )

    agent = client.agents.create_version(
        agent_name=agent_name,
        definition=definition,
    )
    print(f"Created hosted agent {agent.name} version {agent.version}")
    if tools:
        print(f"Attached {len(tools)} MCP tool connection(s).")
    else:
        print("No MCP tools attached. Set GITHUB_MCP_CONNECTION_ID to enable GitHub MCP.")


if __name__ == "__main__":
    main()
