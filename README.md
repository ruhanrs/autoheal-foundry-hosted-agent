# Auto-Heal Foundry Hosted Agent

This project runs a hosted Azure AI agent that diagnoses CI/CD build failures and opens a GitHub pull request with a minimal fix through a Foundry-managed GitHub MCP connection.

## How it works

The runtime uses a two-phase pipeline because the current hosted-agent runtime gives each agent run a single model turn:

1. Phase 1 gathers context with read-only tools.
2. Phase 2 applies a fix with write-only tools.

This keeps each phase narrow and reduces tool misuse.

### Phase 1: Context Gatherer

Phase 1 reads the pipeline failure message, determines the source branch, stack, build ID, failing files, and error lines, then uses GitHub MCP tools to fetch the current file contents.

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

- [main.py](main.py) exposes the hosted-agent server and runs the two phases per incoming request.
- [autoheal/tools.py](autoheal/tools.py) defines the read-only and write-only tool sets.
- [autoheal/instructions.py](autoheal/instructions.py) contains the phase-specific system instructions.
- [autoheal/github.py](autoheal/github.py) wraps Foundry-managed GitHub MCP tool invocation.
- [agent.yaml](agent.yaml) defines the hosted agent container metadata.

## Environment variables

These variables are required at runtime unless noted otherwise:

- `FOUNDRY_PROJECT_ENDPOINT`
- `FOUNDRY_MODEL_DEPLOYMENT_NAME` optional, defaults to `gpt-4.1`
- `GITHUB_REPO_OWNER`
- `GITHUB_REPO_NAME`
- `GITHUB_MCP_CONNECTION_ID`
- `PHASE1_TIMEOUT_SECONDS` optional, defaults to `120`
- `PHASE2_TIMEOUT_SECONDS` optional, defaults to `180`
- `AGENT_MAX_RETRIES` optional, defaults to `2`
- `LOG_LEVEL` optional, defaults to `INFO`

## Local development

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the hosted agent locally:

```bash
python3 main.py
```

The project loads variables from `.env` if present and serves the Foundry `responses` API on `http://localhost:8088`.

## GitHub MCP setup

The current `autoheal` hosted-agent path uses the Foundry-managed GitHub MCP
tool runtime for all repository operations.

Required:

- `GITHUB_MCP_CONNECTION_ID`: the Foundry Project connection ID for the GitHub MCP tool

Optional:

- `GITHUB_MCP_SERVER_LABEL`: defaults to `github`
- `GITHUB_MCP_REQUIRE_APPROVAL`: defaults to `never`
- `GITHUB_MCP_ALLOWED_TOOLS_JSON`: JSON array of allowed MCP tool names
  Recommended list: `["list_pull_requests","get_commit","get_file_contents","create_branch","create_or_update_file","create_pull_request"]`

This runtime does not use GitHub App credentials or app-managed MCP headers.

## Notes on behavior

- Phase 1 supports multiple failing files and stores them in structured JSON.
- Phase 2 updates each file individually and retries once with a refreshed SHA if GitHub reports a conflict.
- The code assumes human review of any generated PR before merge.
