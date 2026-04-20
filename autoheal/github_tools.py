"""GitHub tools exposed to the Auto-Heal agent.

Each function is a thin wrapper over the GitHub REST API and is intentionally
named to match the equivalent GitHub MCP tool, so the agent's instructions can
be reused without modification.

Authentication uses the GITHUB_TOKEN environment variable (a fine-grained
PAT or GitHub App installation token with `contents:write` and
`pull_requests:write` on the target repo).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Annotated, Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
HTTP_TIMEOUT = float(os.getenv("GITHUB_HTTP_TIMEOUT", "30"))


def _headers() -> dict[str, str]:
    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is not set. Configure it in .env or the hosted-agent environment."
        )
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "autoheal-foundry-hosted-agent",
    }


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    url = f"{GITHUB_API_BASE}{path}"
    logger.info("github %s %s", method, path)
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.request(method, url, headers=_headers(), params=params, json=json_body)
    try:
        body = resp.json() if resp.content else None
    except json.JSONDecodeError:
        body = resp.text
    return resp.status_code, body


def _error(status: int, body: Any, action: str) -> str:
    msg = body.get("message") if isinstance(body, dict) else str(body)
    return json.dumps(
        {"error": True, "action": action, "status": status, "message": msg}
    )


def _ok(payload: Any) -> str:
    return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def get_file_contents(
    owner: Annotated[str, "Repository owner, e.g. 'volpara-health'."],
    repo: Annotated[str, "Repository name, e.g. 'DataOrchestrationEngine'."],
    path: Annotated[str, "File or directory path. Use '/' to list the repo root."],
    ref: Annotated[
        str,
        "Branch, tag, or commit SHA. Leave empty to use the repository default branch.",
    ] = "",
) -> str:
    """Read a file or directory from a GitHub repository.

    For files, returns the decoded text content along with the blob `sha`
    (required when calling `create_or_update_file` to update the file).
    For directories, returns an array of entries.

    Use this with `path='/'` and a branch `ref` to verify a branch exists.
    """
    clean_path = path.lstrip("/")
    api_path = f"/repos/{owner}/{repo}/contents/{clean_path}"
    params = {"ref": ref} if ref else None

    status, body = _request("GET", api_path, params=params)

    if status == 404:
        return _error(status, body, "get_file_contents")
    if status >= 400:
        return _error(status, body, "get_file_contents")

    if isinstance(body, list):
        entries = [
            {"name": e.get("name"), "path": e.get("path"), "type": e.get("type"), "sha": e.get("sha")}
            for e in body
        ]
        return _ok({"type": "directory", "ref": ref or None, "entries": entries})

    if isinstance(body, dict) and body.get("type") == "file":
        encoding = body.get("encoding", "")
        raw = body.get("content", "") or ""
        if encoding == "base64":
            try:
                content = base64.b64decode(raw).decode("utf-8")
            except UnicodeDecodeError:
                return _error(415, {"message": "binary file"}, "get_file_contents")
        else:
            content = raw
        return _ok(
            {
                "type": "file",
                "path": body.get("path"),
                "sha": body.get("sha"),
                "size": body.get("size"),
                "ref": ref or None,
                "content": content,
            }
        )

    return _ok(body)


def list_branches(
    owner: Annotated[str, "Repository owner."],
    repo: Annotated[str, "Repository name."],
    per_page: Annotated[int, "Max branches to return (1-100)."] = 100,
) -> str:
    """List branches in a GitHub repository."""
    status, body = _request(
        "GET",
        f"/repos/{owner}/{repo}/branches",
        params={"per_page": max(1, min(per_page, 100))},
    )
    if status >= 400:
        return _error(status, body, "list_branches")
    branches = [
        {"name": b.get("name"), "sha": b.get("commit", {}).get("sha")}
        for b in (body or [])
    ]
    return _ok({"branches": branches})


def create_branch(
    owner: Annotated[str, "Repository owner."],
    repo: Annotated[str, "Repository name."],
    branch: Annotated[str, "Name of the new branch to create, e.g. 'autoheal-bicep/main'."],
    from_branch: Annotated[str, "Source branch to fork from, e.g. 'main'."],
) -> str:
    """Create a new branch from another branch.

    If the branch already exists, returns success with `existed=True`.
    """
    # Look up source branch SHA.
    status, body = _request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{from_branch}")
    if status >= 400:
        return _error(status, body, "create_branch:lookup_source")
    source_sha = body.get("object", {}).get("sha") if isinstance(body, dict) else None
    if not source_sha:
        return _error(500, {"message": "source branch sha not found"}, "create_branch")

    # Check whether target branch already exists.
    check_status, _ = _request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
    if check_status == 200:
        return _ok({"branch": branch, "sha": source_sha, "existed": True})

    create_status, create_body = _request(
        "POST",
        f"/repos/{owner}/{repo}/git/refs",
        json_body={"ref": f"refs/heads/{branch}", "sha": source_sha},
    )
    if create_status >= 400:
        return _error(create_status, create_body, "create_branch")
    return _ok({"branch": branch, "sha": source_sha, "existed": False})


def create_or_update_file(
    owner: Annotated[str, "Repository owner."],
    repo: Annotated[str, "Repository name."],
    path: Annotated[str, "File path within the repository, e.g. 'infra/main.bicep'."],
    content: Annotated[str, "PLAIN TEXT file content. Do NOT base64-encode; the tool handles encoding."],
    message: Annotated[str, "Commit message."],
    branch: Annotated[str, "Target branch to commit to."],
    sha: Annotated[
        str,
        "Existing blob SHA returned by get_file_contents. Required when updating an existing file; leave empty when creating a new file.",
    ] = "",
) -> str:
    """Create a new file or update an existing file in the repository.

    Always pass plain text in `content`; the tool base64-encodes for the API.
    For updates, supply the `sha` returned by `get_file_contents`.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload: dict[str, Any] = {
        "message": message,
        "content": encoded,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    clean_path = path.lstrip("/")
    status, body = _request(
        "PUT",
        f"/repos/{owner}/{repo}/contents/{clean_path}",
        json_body=payload,
    )
    if status >= 400:
        return _error(status, body, "create_or_update_file")

    commit = body.get("commit", {}) if isinstance(body, dict) else {}
    file_info = body.get("content", {}) if isinstance(body, dict) else {}
    return _ok(
        {
            "path": file_info.get("path"),
            "sha": file_info.get("sha"),
            "branch": branch,
            "commit_sha": commit.get("sha"),
            "commit_url": commit.get("html_url"),
        }
    )


def create_pull_request(
    owner: Annotated[str, "Repository owner."],
    repo: Annotated[str, "Repository name."],
    title: Annotated[str, "Pull request title."],
    body: Annotated[str, "Pull request description (markdown)."],
    head: Annotated[str, "Branch with the changes, e.g. 'autoheal-bicep/main'."],
    base: Annotated[str, "Branch to merge into, e.g. 'main'."],
    draft: Annotated[bool, "Open as a draft PR."] = False,
) -> str:
    """Create a new pull request."""
    status, resp = _request(
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        json_body={
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        },
    )
    if status >= 400:
        return _error(status, resp, "create_pull_request")
    return _ok(
        {
            "number": resp.get("number"),
            "url": resp.get("html_url"),
            "state": resp.get("state"),
            "head": (resp.get("head") or {}).get("ref"),
            "base": (resp.get("base") or {}).get("ref"),
        }
    )


def list_pull_requests(
    owner: Annotated[str, "Repository owner."],
    repo: Annotated[str, "Repository name."],
    state: Annotated[str, "Filter by state: 'open', 'closed', or 'all'."] = "open",
    head: Annotated[
        str,
        "Filter by head branch, formatted as 'user:branch' or 'org:branch'. Leave empty to skip.",
    ] = "",
    base: Annotated[str, "Filter by base branch. Leave empty to skip."] = "",
    per_page: Annotated[int, "Max PRs to return (1-100)."] = 50,
) -> str:
    """List pull requests in a repository."""
    params: dict[str, Any] = {"state": state, "per_page": max(1, min(per_page, 100))}
    if head:
        params["head"] = head
    if base:
        params["base"] = base
    status, resp = _request("GET", f"/repos/{owner}/{repo}/pulls", params=params)
    if status >= 400:
        return _error(status, resp, "list_pull_requests")
    prs = [
        {
            "number": p.get("number"),
            "title": p.get("title"),
            "state": p.get("state"),
            "url": p.get("html_url"),
            "head": (p.get("head") or {}).get("ref"),
            "base": (p.get("base") or {}).get("ref"),
        }
        for p in (resp or [])
    ]
    return _ok({"pull_requests": prs})


def get_pull_request(
    owner: Annotated[str, "Repository owner."],
    repo: Annotated[str, "Repository name."],
    pull_number: Annotated[int, "Pull request number."],
) -> str:
    """Fetch a single pull request by number."""
    status, resp = _request("GET", f"/repos/{owner}/{repo}/pulls/{pull_number}")
    if status >= 400:
        return _error(status, resp, "get_pull_request")
    return _ok(
        {
            "number": resp.get("number"),
            "title": resp.get("title"),
            "state": resp.get("state"),
            "url": resp.get("html_url"),
            "head": (resp.get("head") or {}).get("ref"),
            "base": (resp.get("base") or {}).get("ref"),
            "merged": resp.get("merged"),
            "mergeable": resp.get("mergeable"),
        }
    )
