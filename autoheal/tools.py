"""
Single-tool auto-heal pipeline.

`create_pipeline_tool` returns one tool — `run_autoheal_pipeline` — that
performs every GitHub read, fix generation, and GitHub write inside Python.
The outer model only needs to extract parameters from the CI log and call
this tool once.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import PurePosixPath
from typing import Annotated, Any

from agent_framework import tool

from .github import GitHubError, RepositoryClient

logger = logging.getLogger(__name__)

_MAX_FILE_CHARS = 12_000

_VALID_BRANCH_RE = re.compile(r"^[a-zA-Z0-9._/\-]{1,250}$")
_PATH_TRAVERSAL_RE = re.compile(r"(\.\./|/\.\.|^\.\.|^\./\.\.|^/)")

_FIX_SYSTEM_PROMPT = (
    "You are a code-fix assistant. Given build errors and a file's current "
    "contents, return ONLY the complete corrected file contents. Change the "
    "minimum number of lines necessary to resolve the errors. Do not include "
    "explanations, markdown fences, or commentary — output only the raw file "
    "content so it can be written back to the repository verbatim."
)


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


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _build_fix_prompt(path: str, errors: list[str], content: str) -> str:
    errors_block = "\n".join(f"- {e}" for e in errors) or "- (no error lines provided)"
    return (
        f"File path: {path}\n\n"
        f"Build errors:\n{errors_block}\n\n"
        f"Current file content:\n{content}\n\n"
        "Return the complete corrected file content."
    )


def create_pipeline_tool(
    github: RepositoryClient,
    ai_client: Any,
    result_container: list,
) -> list:
    """Return the single `run_autoheal_pipeline` tool."""

    async def _generate_fix(path: str, errors: list[str], content: str) -> str:
        from agent_framework import ChatMessage

        prompt = _build_fix_prompt(path, errors, content)

        # Use the same AzureAIAgentClient to generate fixes (no tools, just LLM)
        async with ai_client.as_agent(
            name=f"fix-generator-{path.replace('/', '-')[:30]}",
            instructions=_FIX_SYSTEM_PROMPT,
        ) as fix_agent:
            response = await fix_agent.run(prompt)

        # Extract text from response
        response_text = ""
        if isinstance(response, str):
            response_text = response
        elif hasattr(response, "text"):
            response_text = response.text or ""
        elif hasattr(response, "messages"):
            for msg in response.messages:
                if isinstance(msg, ChatMessage):
                    response_text = msg.text or ""
                    break

        return _strip_code_fences(response_text)

    @tool
    async def run_autoheal_pipeline(
        source_branch: Annotated[str, "Source branch from the pipeline prompt"],
        stack: Annotated[str, "Technology stack (for example dotnet)"],
        build_id: Annotated[str, "Build ID from the prompt"],
        root_cause: Annotated[str, "One-line summary of why the build failed"],
        errors_json: Annotated[str, "JSON array of unique build error lines, verbatim"],
        failing_files_json: Annotated[str, "JSON array of unique repo-relative failing file paths"],
    ) -> str:
        """Run the full auto-heal workflow: read files, generate fixes, commit, and open a PR."""

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
        if not root_cause or not root_cause.strip():
            return "ERROR: root_cause must not be empty."

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
        logger.info(
            "[pipeline] source='%s' stack='%s' build='%s' files=%d",
            source_branch,
            stack,
            build_id,
            len(normalized_paths),
        )

        existing_prs = await github.list_pull_requests(state="open", head=autoheal_branch)
        existing_pr_url = existing_prs[0].html_url if existing_prs else "none"
        ref_to_read = autoheal_branch if existing_prs else source_branch

        fetched_files: list[dict] = []
        for path in normalized_paths:
            try:
                fc = await github.get_file_contents(path, ref_to_read)
            except GitHubError as exc:
                logger.warning("[pipeline] read failed for '%s': %s", path, exc)
                fetched_files.append(
                    {"path": path, "status": "error", "error": str(exc)}
                )
                continue
            content = fc.content
            truncated = False
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS]
                truncated = True
            fetched_files.append(
                {
                    "path": fc.path,
                    "sha": fc.sha,
                    "status": "ok",
                    "content": content,
                    "content_truncated": truncated,
                }
            )

        generated_fixes: list[dict[str, str]] = []
        inference_errors: list[str] = []
        for entry in fetched_files:
            if entry["status"] != "ok":
                continue
            try:
                corrected = await _generate_fix(
                    entry["path"], unique_errors, entry["content"]
                )
            except Exception as exc:
                logger.warning(
                    "[pipeline] fix generation failed for '%s': %s", entry["path"], exc
                )
                inference_errors.append(f"{entry['path']}: {exc}")
                continue

            if not corrected.strip():
                inference_errors.append(
                    f"{entry['path']}: fix model returned empty content"
                )
                continue
            if corrected == entry["content"]:
                logger.info(
                    "[pipeline] fix model returned unchanged content for '%s'; skipping",
                    entry["path"],
                )
                continue

            error_code = "build-error"
            for err in unique_errors:
                match = re.search(r"\b([A-Z]{1,4}\d{3,5})\b", err)
                if match:
                    error_code = match.group(1)
                    break

            generated_fixes.append(
                {
                    "path": entry["path"],
                    "content": corrected,
                    "sha": entry["sha"],
                    "error_code": error_code,
                }
            )

        if generated_fixes:
            if not await github.branch_exists(source_branch):
                return f"ERROR: Source branch '{source_branch}' does not exist."
            if not await github.branch_exists(autoheal_branch):
                new_sha = await github.create_branch(autoheal_branch, source_branch)
                logger.info(
                    "[pipeline] Created '%s' at SHA %s.", autoheal_branch, new_sha[:8]
                )
            else:
                logger.info("[pipeline] Reusing existing branch '%s'.", autoheal_branch)

        modified_paths: list[str] = []
        write_errors: list[str] = []
        for fix in generated_fixes:
            commit_message = (
                f"fix: resolve {fix['error_code']} in "
                f"{PurePosixPath(fix['path']).name} (build {build_id})"
            )
            try:
                new_sha = await github.create_or_update_file(
                    path=fix["path"],
                    content=fix["content"],
                    message=commit_message,
                    branch=autoheal_branch,
                    sha=fix["sha"] or None,
                )
            except GitHubError as exc:
                logger.warning("[pipeline] update failed for '%s': %s", fix["path"], exc)
                write_errors.append(f"{fix['path']}: {exc}")
                continue
            modified_paths.append(fix["path"])
            logger.info("[pipeline] Committed '%s'. New SHA: %s", fix["path"], new_sha[:8])

        pull_request_url = existing_pr_url
        if modified_paths and existing_pr_url == "none":
            body = (
                f"Root cause: {root_cause}\n\n"
                f"Files changed: {', '.join(modified_paths)}\n\n"
                f"Build ID: {build_id}\n\n"
                f"Build errors:\n- "
                + "\n- ".join(unique_errors[:20])
            )
            try:
                pr = await github.create_pull_request(
                    title=f"Auto-heal: Fix {stack} pipeline failure ({source_branch})",
                    body=body,
                    head=autoheal_branch,
                    base=source_branch,
                )
                pull_request_url = pr.html_url
                logger.info("[pipeline] Created PR #%d: %s", pr.number, pr.html_url)
            except GitHubError as exc:
                logger.warning("[pipeline] PR creation failed: %s", exc)
                write_errors.append(f"pull_request: {exc}")

        fix_details_parts: list[str] = []
        if modified_paths:
            fix_details_parts.append(
                f"Generated corrected contents for: {', '.join(modified_paths)}"
            )
        else:
            fix_details_parts.append("No file updates were committed.")
        read_errors = [f["path"] for f in fetched_files if f["status"] == "error"]
        if read_errors:
            fix_details_parts.append(f"Files that could not be read: {', '.join(read_errors)}")
        if inference_errors:
            fix_details_parts.append(f"Fix generation errors: {'; '.join(inference_errors)}")
        if write_errors:
            fix_details_parts.append(f"Write errors: {'; '.join(write_errors)}")
        fix_applied = " ".join(fix_details_parts)

        output = (
            f"Root Cause: {root_cause}\n"
            f"Fix Applied: {fix_applied}\n"
            f"Files Modified: {', '.join(modified_paths) if modified_paths else '(none)'}\n"
            f"Branch: {autoheal_branch}\n"
            f"Pull Request: {pull_request_url}\n"
            f"Build ID: {build_id}"
        )
        result_container.clear()
        result_container.append(output)
        logger.info("[pipeline] Final result recorded:\n%s", output)
        return f"Pipeline complete.\n\n{output}"

    return [run_autoheal_pipeline]
