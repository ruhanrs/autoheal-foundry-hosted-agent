# Auto-Heal Foundry Hosted Agent

This project runs a hosted Azure AI agent that attempts to diagnose CI/CD build failures and open a GitHub pull request with a minimal fix.

## How it works

The runtime uses a two-phase pipeline because the current hosted-agent runtime gives each `run_async()` call a single model turn:

1. Phase 1 gathers context with read-only tools.
2. Phase 2 applies a fix with write-only tools.

This keeps each phase narrow and reduces tool misuse.

### Phase 1: Context Gatherer

Phase 1 reads the pipeline failure message, determines the source branch, stack, build ID, failing files, and error lines, then uses GitHub read APIs to fetch the current file contents.

The gathered context is stored as structured JSON with:

- repository metadata
- unique build errors
- all failing files
- per-file fetch status, SHA, and content

### Phase 2: Fix Applier

Phase 2 receives the structured JSON context inside its instructions and can:

- create or reuse the auto-heal branch
- commit one or more corrected files
- open a pull request if one does not already exist
- emit a final structured result summary

## Repository layout

- [main.py](main.py) orchestrates the two phases and retry behavior.
- [autoheal/tools.py](autoheal/tools.py) defines the read-only and write-only tool sets.
- [autoheal/instructions.py](autoheal/instructions.py) contains the phase-specific system instructions.
- [autoheal/github.py](autoheal/github.py) wraps GitHub App authentication and REST operations.
- [agent.yaml](agent.yaml) defines the hosted agent container metadata.

## Environment variables

These variables are required at runtime unless noted otherwise:

- `FOUNDRY_PROJECT_ENDPOINT`
- `FOUNDRY_MODEL_DEPLOYMENT_NAME` optional, defaults to `gpt-4.1`
- `GITHUB_APP_ID`
- `GITHUB_APP_INSTALLATION_ID`
- `GITHUB_APP_PRIVATE_KEY`
- `GITHUB_REPO_OWNER`
- `GITHUB_REPO_NAME`
- `PHASE1_TIMEOUT_SECONDS` optional, defaults to `120`
- `PHASE2_TIMEOUT_SECONDS` optional, defaults to `180`
- `AGENT_MAX_RETRIES` optional, defaults to `2`
- `LOG_LEVEL` optional, defaults to `INFO`

`GITHUB_APP_PRIVATE_KEY` may be either:

- a PEM string
- a path to a PEM file relative to the repo root

## Local development

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the agent locally:

```bash
python3 main.py
```

The project loads variables from `.env` if present.

## GitHub MCP mode

The current `autoheal` hosted-agent path uses the Foundry-managed GitHub MCP
tool runtime for all repository operations.

Required:

- `GITHUB_MCP_CONNECTION_ID`: Foundry project connection ID for the GitHub MCP tool

Optional:

- `GITHUB_MCP_SERVER_LABEL`: defaults to `github`
- `GITHUB_MCP_REQUIRE_APPROVAL`: defaults to `never`
- `GITHUB_MCP_ALLOWED_TOOLS_JSON`: JSON array of allowed MCP tool names

This path no longer requires GitHub App credentials or app-managed MCP headers.

Enable it with:

- `USE_GITHUB_MCP=true`
- either `GITHUB_MCP_URL` for a streamable HTTP MCP endpoint
- or `GITHUB_MCP_COMMAND` plus optional `GITHUB_MCP_ARGS_JSON`,
  `GITHUB_MCP_ENV_JSON`, and `GITHUB_MCP_HEADERS_JSON`

When MCP mode is disabled, the app falls back to direct GitHub App API calls.

## Notes on behavior

- Phase 1 supports multiple failing files and stores them in structured JSON.
- Phase 2 updates each file individually and retries once with a refreshed SHA if GitHub reports a conflict.
- The code assumes human review of any generated PR before merge.
