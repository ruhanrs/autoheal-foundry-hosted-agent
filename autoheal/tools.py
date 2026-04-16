"""
Tool factories for the two-phase auto-heal pipeline.

Phase 1 — Context Gathering  : create_context_tools()
Phase 2 — Fix Application    : create_apply_tools()

Each factory returns a single high-leverage tool, reducing dependence on
multi-step tool chaining inside one model turn.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import PurePosixPath
from typing import Annotated

from agent_framework import tool

from .github import GitHubClient, GitHubError

logger = logging.getLogger(__name__)

_MAX_FILE_CHARS = 12_000

_VALID_BRANCH_RE = re.compile(r"^[a-zA-Z0-9._/\-]{1,250}$")
_PATH_TRAVERSAL_RE = re.compile(r"(\.\./|/\.\.|^\.\.|^\./\.\.|^/)")


def _validate_branch(name: str) -> str | None:
    if not name or not name.strip():
        return "Branch name must not be empty."
    if not _VALID_BRANCH_RE.match(name):
        return f"Invalid branch name '{name}'."
    return None


def _validate_path(path: str) -> str | None:
    if not path or not path.strip():
        return "File path must not be empty."
    if _PATH_TRAVERSAL_RE.search(path):
        return f"Unsafe file path '{path}'. Path traversal is not allowed."
    return None


def _parse_json_array(raw: str, label: str) -> tuple[list | None, str | None]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{label} must be valid JSON: {exc}"
    if not isinstance(value, list):
        return None, f"{label} must be a JSON array."
    return value, None


def _normalize_failing_path(path: str) -> str:
    normalized = str(PurePosixPath(path.strip()))
    prefix = "/home/vsts/work/1/s/"
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix):]
    if normalized == ".":
        return ""
    return normalized


def _build_context_block(
    *,
    source_branch: str,
    autoheal_branch: str,
    stack: str,
    build_id: str,
    existing_pr_url: str,
    errors: list[str],
    files: list[dict],
) -> str:
    return json.dumps(
        {
            "source_branch": source_branch,
            "autoheal_branch": autoheal_branch,
            "stack": stack,
            "build_id": build_id,
            "existing_pr_url": existing_pr_url,
            "errors": errors,
            "files": files,
        },
        indent=2,
    )


def create_context_tools(github: GitHubClient, context_container: list) -> list:
    """Tools for Phase 1. gather_context writes into context_container[0]."""

    @tool
    async def gather_context(
        source_branch: Annotated[str, "Source branch from the pipeline prompt"],
        stack: Annotated[str, "Technology stack (for example dotnet)"],
        build_id: Annotated[str, "Build ID from the prompt"],
        errors_json: Annotated[str, "JSON array of unique build error lines, verbatim"],
        failing_files_json: Annotated[str, "JSON array of unique repo-relative failing file paths"],
    ) -> str:
        """Resolve branches, fetch failing files, and store Phase 2 context."""
        errors, errors_err = _parse_json_array(errors_json, "errors_json")
        if errors_err:
            return f"ERROR: {errors_err}"

        failing_files, failing_files_err = _parse_json_array(
            failing_files_json, "failing_files_json"
        )
        if failing_files_err:
            return f"ERROR: {failing_files_err}"

        branch_err = _validate_branch(source_branch)
        if branch_err:
            return f"ERROR: {branch_err}"
        if not stack or not stack.strip():
            return "ERROR: stack must not be empty."
        if not build_id or not build_id.strip():
            return "ERROR: build_id must not be empty."

        unique_errors: list[str] = []
        seen_errors: set[str] = set()
        for raw_error in errors:
            error_text = str(raw_error)
            if error_text not in seen_errors:
                unique_errors.append(error_text)
                seen_errors.add(error_text)

        normalized_paths: list[str] = []
        seen_paths: set[str] = set()
        for idx, raw_path in enumerate(failing_files, start=1):
            path = _normalize_failing_path(str(raw_path))
            path_err = _validate_path(path)
            if path_err:
                return f"ERROR: failing_files_json item {idx}: {path_err}"
            if path not in seen_paths:
                normalized_paths.append(path)
                seen_paths.add(path)

        autoheal_branch = f"autoheal-{stack}/{source_branch}"
        logger.info("[Phase 1] gather_context: source='%s' stack='%s'", source_branch, stack)

        prs = await github.list_pull_requests(state="open", head=autoheal_branch)
        existing_pr_url = prs[0].html_url if prs else "none"
        ref_to_read = autoheal_branch if prs else source_branch

        files: list[dict] = []
        for path in normalized_paths:
            logger.debug("[Phase 1] reading '%s' from '%s'", path, ref_to_read)
            try:
                fc = await github.get_file_contents(path, ref_to_read)
            except GitHubError as exc:
                logger.warning("[Phase 1] read failed for '%s': %s", path, exc)
                files.append(
                    {
                        "path": path,
                        "ref": ref_to_read,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                continue

            content = fc.content
            truncated = False
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS]
                truncated = True

            files.append(
                {
                    "path": fc.path,
                    "ref": ref_to_read,
                    "sha": fc.sha,
                    "status": "ok",
                    "content": content,
                    "content_truncated": truncated,
                }
            )

        block = _build_context_block(
            source_branch=source_branch,
            autoheal_branch=autoheal_branch,
            stack=stack,
            build_id=build_id,
            existing_pr_url=existing_pr_url,
            errors=unique_errors,
            files=files,
        )
        context_container.clear()
        context_container.append(block)
        logger.info(
            "[Phase 1] Context captured for %d file(s) on branch '%s'.",
            len(files),
            source_branch,
        )
        return "Context captured. Phase 1 complete."

    return [gather_context]


def create_apply_tools(github: GitHubClient, result_container: list) -> list:
    """Tools for Phase 2. apply_fixes writes into result_container[0]."""

    @tool
    async def apply_fixes(
        root_cause: Annotated[str, "One-line summary of why the build failed"],
        fix_applied: Annotated[str, "What changed and why it fixes the error"],
        fixes_json: Annotated[str, "JSON array of file updates with path/content/sha/error_code"],
    ) -> str:
        """Apply all file updates, create a PR when needed, and store the result."""
        fixes, fixes_err = _parse_json_array(fixes_json, "fixes_json")
        if fixes_err:
            return f"ERROR: {fixes_err}"

        context_raw = os.environ.get("AUTOHEAL_PHASE2_CONTEXT")
        if not context_raw:
            return "ERROR: AUTOHEAL_PHASE2_CONTEXT is missing."

        try:
            context = json.loads(context_raw)
        except json.JSONDecodeError as exc:
            return f"ERROR: AUTOHEAL_PHASE2_CONTEXT is invalid JSON: {exc}"

        source_branch = str(context.get("source_branch", ""))
        autoheal_branch = str(context.get("autoheal_branch", ""))
        stack = str(context.get("stack", ""))
        build_id = str(context.get("build_id", ""))
        existing_pr_url = str(context.get("existing_pr_url", "none"))

        for name, label in ((source_branch, "source_branch"), (autoheal_branch, "autoheal_branch")):
            err = _validate_branch(name)
            if err:
                return f"ERROR: {label}: {err}"

        normalized_fixes: list[dict[str, str]] = []
        for idx, fix in enumerate(fixes, start=1):
            if not isinstance(fix, dict):
                return f"ERROR: fixes_json item {idx} must be a JSON object."
            path = str(fix.get("path", ""))
            path_err = _validate_path(path)
            if path_err:
                return f"ERROR: fixes_json item {idx}: {path_err}"
            content = str(fix.get("content", ""))
            if not content:
                return f"ERROR: fixes_json item {idx}: content must not be empty."
            normalized_fixes.append(
                {
                    "path": path,
                    "content": content,
                    "sha": str(fix.get("sha", "")),
                    "error_code": str(fix.get("error_code", "")).strip() or "build-error",
                }
            )

        logger.info(
            "[Phase 2] apply_fixes: source='%s' branch='%s' fixes=%d",
            source_branch,
            autoheal_branch,
            len(normalized_fixes),
        )

        if normalized_fixes:
            if not await github.branch_exists(source_branch):
                return f"ERROR: Source branch '{source_branch}' does not exist."
            if not await github.branch_exists(autoheal_branch):
                sha = await github.get_branch_sha(source_branch)
                new_sha = await github.create_branch(autoheal_branch, sha)
                logger.info("[Phase 2] Created '%s' at SHA %s.", autoheal_branch, new_sha[:8])
            else:
                logger.info("[Phase 2] Reusing existing branch '%s'.", autoheal_branch)

        modified_paths: list[str] = []
        write_errors: list[str] = []
        for fix in normalized_fixes:
            path = fix["path"]
            commit_message = (
                f"fix: resolve {fix['error_code']} in {PurePosixPath(path).name} "
                f"(build {build_id})"
            )
            try:
                new_sha = await github.create_or_update_file(
                    path=path,
                    content=fix["content"],
                    message=commit_message,
                    branch=autoheal_branch,
                    sha=fix["sha"] or None,
                )
            except GitHubError as exc:
                logger.warning("[Phase 2] update failed for '%s': %s", path, exc)
                write_errors.append(f"{path}: {exc}")
                continue
            modified_paths.append(path)
            logger.info("[Phase 2] Committed '%s'. New SHA: %s", path, new_sha[:8])

        pull_request_url = existing_pr_url
        if modified_paths and existing_pr_url == "none":
            body = (
                f"Root cause: {root_cause}\n\n"
                f"Files changed: {', '.join(modified_paths)}\n\n"
                f"Fix summary: {fix_applied}\n\n"
                f"Build ID: {build_id}"
            )
            try:
                pr = await github.create_pull_request(
                    title=f"Auto-heal: Fix {stack} pipeline failure ({source_branch})",
                    body=body,
                    head=autoheal_branch,
                    base=source_branch,
                )
                pull_request_url = pr.html_url
                logger.info("[Phase 2] Created PR #%d: %s", pr.number, pr.html_url)
            except GitHubError as exc:
                logger.warning("[Phase 2] PR creation failed: %s", exc)
                write_errors.append(f"pull_request: {exc}")

        details = fix_applied
        if write_errors:
            details = f"{fix_applied} Write errors: {'; '.join(write_errors)}"

        output = (
            f"Root Cause: {root_cause}\n"
            f"Fix Applied: {details}\n"
            f"Files Modified: {', '.join(modified_paths)}\n"
            f"Branch: {autoheal_branch}\n"
            f"Pull Request: {pull_request_url}\n"
            f"Build ID: {build_id}"
        )
        result_container.clear()
        result_container.append(output)
        logger.info("[Phase 2] Final result recorded:\n%s", output)
        return f"Result recorded. Workflow complete.\n\n{output}"

    return [apply_fixes]
