# LangGraph Hosted Agent Structure

This folder contains a Foundry-hostable LangGraph version of the auto-heal
workflow.

## What it includes

- `state.py`: shared graph state
- `parser.py`: deterministic parsing of the pipeline failure text
- `prompts.py`: fix-planning prompt builder
- `planner.py`: calls a Foundry model deployment through the Responses API
- `nodes.py`: graph nodes for parse, context gathering, fix planning, apply,
  and finalize
- `graph.py`: compiled LangGraph workflow
- `main.py`: CLI runner for direct local testing with `AUTOHEAL_INPUT`
- `host.py`: Foundry hosting entrypoint using `azure-ai-agentserver-langgraph`
- `Dockerfile`: container image for hosted-agent deployment
- `agent.yaml`: hosted-agent manifest
- `deploy_hosted_agent.py`: SDK-based deployment helper

## Runtime model

This LangGraph path uses:

- LangGraph for explicit orchestration
- `azure-ai-agentserver-langgraph` as the hosting adapter
- the existing `autoheal/github.py` GitHub App client for repository access
- a Foundry model deployment for the fix-planning node

The current graph now supports true MCP-based repository operations through the
shared repository client in `autoheal/github.py`.

## GitHub access

The hosted graph reuses the same environment variables as the current app:

- `GITHUB_APP_ID`
- `GITHUB_APP_INSTALLATION_ID`
- `GITHUB_APP_PRIVATE_KEY`
- `GITHUB_REPO_OWNER`
- `GITHUB_REPO_NAME`

That means the LangGraph version can read files, create branches, update files,
and open pull requests either through direct GitHub App API calls or through
the GitHub MCP server.

## GitHub MCP runtime support

Set one of these MCP transport modes:

- Streamable HTTP:
  - `USE_GITHUB_MCP=true`
  - `GITHUB_MCP_URL=https://.../mcp`
  - optional `GITHUB_MCP_HEADERS_JSON={"Authorization":"Bearer ..."}`

- stdio:
  - `USE_GITHUB_MCP=true`
  - `GITHUB_MCP_COMMAND=npx`
  - `GITHUB_MCP_ARGS_JSON=["-y","@github/github-mcp-server"]`
  - optional `GITHUB_MCP_ENV_JSON={"GITHUB_TOKEN":"..."}`

## GitHub MCP deployment support

The deployment helper also supports attaching one or more MCP connections to
the hosted agent definition through the Foundry SDK.

Set:

- `GITHUB_MCP_CONNECTION_ID`: project connection ID for your GitHub MCP server
- `EXTRA_MCP_CONNECTION_IDS`: optional comma-separated list of additional MCP
  connection IDs

Then deploy with:

```bash
export HOSTED_AGENT_IMAGE="myregistry.azurecr.io/autoheal-langgraph:v1"
export GITHUB_MCP_CONNECTION_ID="/subscriptions/.../connections/github-mcp"
python -m langgraph_agent.deploy_hosted_agent
```

This attaches MCP tools using the `HostedAgentDefinition.tools` field described
in the Foundry hosted-agent docs.

The graph nodes now use the shared repository client, which can route GitHub
operations through MCP when `USE_GITHUB_MCP=true`.

## Local testing

CLI mode:

```bash
export AUTOHEAL_INPUT="Pipeline Source Branch: main ..."
python -m langgraph_agent.main
```

Hosted-agent HTTP mode:

```bash
python -m langgraph_agent.host
curl -sS -H "Content-Type: application/json" \
  -X POST http://localhost:8088/responses \
  -d '{"input":{"messages":[{"role":"user","content":"Pipeline Source Branch: main ..."}]}}'
```

## Deploy to Foundry

Build the image using `Dockerfile`, push it to Azure Container Registry, then
deploy with either:

- `langgraph_agent/agent.yaml` if you use manifest-based deployment tooling
- `langgraph_agent/deploy_hosted_agent.py` if you prefer SDK-driven deployment
  and want to attach MCP tools

## Optional MCP note

Foundry hosted agents attach MCP tools during deployment. In this repo, that is
handled in `deploy_hosted_agent.py` through `HostedAgentDefinition.tools`.
