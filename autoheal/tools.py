"""Tool functions exposed to the model — each wraps GitHubClient with validation."""

from __future__ import annotations

import logging
import re
from typing import Annotated

from agent_framework import tool

from .github import GitHubClient, GitHubError

logger = logging.getLogger(__name__)

# Max characters of file content returned to the model.
# Keeps tool responses within a safe context budget (~3K tokens per file).
_MAX_FILE_CHARS = 12_000

# Branch names: allow alphanumeric, hyphens, underscores, forward slashes, dots.
_VALID_BRANCH_RE = re.compile(r"^[a-zA-Z0-9._/\-]{1,250}$")

# File paths must not escape the repo root or reference hidden system paths.
_PATH_TRAVERSAL_RE = re.compile(r"(\.\./|/\.\.|^\.\.|^\./\.\.|^/)")


def _validate_branch(name: str) -> str | None:
    """Return an error string if the branch name is invalid, else None."""
    if not name or not name.strip():
        return "Branch name must not be empty."
    if not _VALID_BRANCH_RE.match(name):
        return (
            f"Invalid branch name '{name}'. "
            "Only alphanumeric characters, hyphens, underscores, dots, and forward slashes are allowed."
        )
    return None


def _validate_path(path: str) -> str | None:
    """Return an error string if the file path looks unsafe, else None."""
    if not path or not path.strip():
        return "File path must not be empty."
    if _PATH_TRAVERSAL_RE.search(path):
        return f"Unsafe file path '{path}'. Path traversal sequences are not allowed."
    return None


def create_tools(github: GitHubClient) -> list:
    """Create tool functions bound to the given GitHubClient instance."""

    # ── Branch operations ─────────────────────────────────────────────

    @tool
    async def verify_branch_exists(
        branch: Annotated[str, "Branch name to check"],
    ) -> str:
        """Verify that a branch exists in the repository. Returns confirmation or 'not_found'. Always call this before creating an auto-heal branch to confirm the source branch is real."""
        err = _validate_branch(branch)
        if err:
            return f"ERROR: {err}"
        logger.debug("verify_branch_exists: branch='%s'", branch)
        exists = await github.branch_exists(branch)
        if exists:
            return f"Branch '{branch}' exists."
        return f"Branch '{branch}' does NOT exist in the repository."

    @tool
    async def create_branch(
        new_branch: Annotated[str, "Name for the new branch"],
        from_branch: Annotated[str, "Source branch to create from"],
    ) -> str:
        """Create a new branch from a source branch. Returns the SHA of the new branch head. The source branch MUST be verified with verify_branch_exists first."""
        for name in (new_branch, from_branch):
            err = _validate_branch(name)
            if err:
                return f"ERROR: {err}"

        logger.info("create_branch: '%s' from '%s'", new_branch, from_branch)

        if not await github.branch_exists(from_branch):
            return f"ERROR: Source branch '{from_branch}' does not exist. Cannot create '{new_branch}'."
        if await github.branch_exists(new_branch):
            return f"Branch '{new_branch}' already exists. Reuse it instead of creating a new one."
        sha = await github.get_branch_sha(from_branch)
        new_sha = await github.create_branch(new_branch, sha)
        logger.info("Created branch '%s' at SHA %s.", new_branch, new_sha[:8])
        return f"Created branch '{new_branch}' from '{from_branch}' at SHA {new_sha}."

    # ── File operations ───────────────────────────────────────────────

    @tool
    async def get_file_contents(
        path: Annotated[str, "File path in the repository (no leading slash)"],
        ref: Annotated[str, "Branch or commit ref to read from"],
    ) -> str:
        """Read a file from the repository at a specific branch ref. Returns the file content and its SHA (needed for updates). Use the auto-heal branch ref if it exists, otherwise the source branch."""
        path_err = _validate_path(path)
        if path_err:
            return f"ERROR: {path_err}"
        ref_err = _validate_branch(ref)
        if ref_err:
            return f"ERROR: {ref_err}"

        logger.debug("get_file_contents: path='%s' ref='%s'", path, ref)
        try:
            fc = await github.get_file_contents(path, ref)
        except GitHubError as e:
            logger.warning("get_file_contents failed: %s", e)
            return f"ERROR reading '{path}' at ref '{ref}': {e}"

        content = fc.content
        truncated = ""
        if len(content) > _MAX_FILE_CHARS:
            content = content[:_MAX_FILE_CHARS]
            truncated = f"\n[TRUNCATED: file exceeds {_MAX_FILE_CHARS} chars — shown first {_MAX_FILE_CHARS}]"

        return (
            f"File: {fc.path}\n"
            f"Ref: {ref}\n"
            f"SHA: {fc.sha}\n"
            f"---\n"
            f"{content}{truncated}"
        )

    @tool
    async def create_or_update_file(
        path: Annotated[str, "File path in the repository (no leading slash)"],
        content: Annotated[str, "Complete plain-text file content — never Base64"],
        commit_message: Annotated[str, "Git commit message"],
        branch: Annotated[str, "Branch to commit to"],
        sha: Annotated[str, "File SHA from get_file_contents (required for updates, empty string for new files)"] = "",
    ) -> str:
        """Create or update a file on a branch. Requires the file SHA from get_file_contents for updates (to prevent conflicts). For new files, omit the sha parameter. Content must be plain text in the original file format."""
        path_err = _validate_path(path)
        if path_err:
            return f"ERROR: {path_err}"
        branch_err = _validate_branch(branch)
        if branch_err:
            return f"ERROR: {branch_err}"
        if not commit_message or not commit_message.strip():
            return "ERROR: commit_message must not be empty."
        if not content:
            return "ERROR: content must not be empty."

        file_sha = sha if sha else None
        logger.info("create_or_update_file: path='%s' branch='%s'", path, branch)
        try:
            new_sha = await github.create_or_update_file(
                path=path,
                content=content,
                message=commit_message,
                branch=branch,
                sha=file_sha,
            )
        except GitHubError as e:
            logger.warning("create_or_update_file failed: %s", e)
            return f"ERROR writing '{path}' on branch '{branch}': {e}"
        logger.info("Committed '%s' on '%s'. New SHA: %s", path, branch, new_sha[:8])
        return f"Committed '{path}' on branch '{branch}'. New SHA: {new_sha}."

    # ── Pull request operations ───────────────────────────────────────

    @tool
    async def list_pull_requests(
        head_branch: Annotated[str, "Filter by head branch name (optional, pass empty string to list all)"] = "",
    ) -> str:
        """List open pull requests. Optionally filter by head branch name. Use this to check for an existing auto-heal PR before creating one."""
        head: str | None = None
        if head_branch:
            err = _validate_branch(head_branch)
            if err:
                return f"ERROR: {err}"
            head = head_branch

        logger.debug("list_pull_requests: head='%s'", head)
        prs = await github.list_pull_requests(state="open", head=head)
        if not prs:
            return "No open pull requests found matching the criteria."
        lines = []
        for pr in prs:
            lines.append(
                f"PR #{pr.number}: {pr.title}\n"
                f"  URL: {pr.html_url}\n"
                f"  Head: {pr.head_ref} -> Base: {pr.base_ref}\n"
                f"  State: {pr.state}"
            )
        return "\n\n".join(lines)

    @tool
    async def create_pull_request(
        title: Annotated[str, "PR title"],
        body: Annotated[str, "PR description with root cause, files modified, and fix explanation"],
        head_branch: Annotated[str, "Source branch (auto-heal branch)"],
        base_branch: Annotated[str, "Target branch to merge into"],
    ) -> str:
        """Create a new pull request. Returns the PR number and URL. Only call this if list_pull_requests found no existing auto-heal PR."""
        for name, label in ((head_branch, "head_branch"), (base_branch, "base_branch")):
            err = _validate_branch(name)
            if err:
                return f"ERROR: {label}: {err}"
        if not title or not title.strip():
            return "ERROR: title must not be empty."

        logger.info("create_pull_request: '%s' -> '%s'", head_branch, base_branch)
        try:
            pr = await github.create_pull_request(
                title=title, body=body, head=head_branch, base=base_branch,
            )
        except GitHubError as e:
            logger.warning("create_pull_request failed: %s", e)
            return f"ERROR creating PR: {e}"
        logger.info("Created PR #%d: %s", pr.number, pr.html_url)
        return (
            f"Pull request created.\n"
            f"PR #{pr.number}: {pr.title}\n"
            f"URL: {pr.html_url}\n"
            f"Head: {pr.head_ref} -> Base: {pr.base_ref}"
        )

    @tool
    async def get_pull_request(
        pr_number: Annotated[int, "Pull request number"],
    ) -> str:
        """Get details of an existing pull request by number. Use to verify a PR exists and get its current URL/state."""
        if not isinstance(pr_number, int) or pr_number <= 0:
            return "ERROR: pr_number must be a positive integer."
        logger.debug("get_pull_request: #%d", pr_number)
        try:
            pr = await github.get_pull_request(pr_number)
        except GitHubError as e:
            logger.warning("get_pull_request failed: %s", e)
            return f"ERROR fetching PR #{pr_number}: {e}"
        return (
            f"PR #{pr.number}: {pr.title}\n"
            f"URL: {pr.html_url}\n"
            f"Head: {pr.head_ref} -> Base: {pr.base_ref}\n"
            f"State: {pr.state}"
        )

    return [
        verify_branch_exists,
        create_branch,
        get_file_contents,
        create_or_update_file,
        list_pull_requests,
        create_pull_request,
        get_pull_request,
    ]
