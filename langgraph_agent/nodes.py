"""LangGraph node implementations for the auto-heal workflow."""

from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath
from typing import Any, Protocol

from langchain_core.messages import AIMessage, HumanMessage

from autoheal.github import GitHubError, RepositoryClient

from .parser import parse_failure_input
from .prompts import build_fix_planning_prompt
from .state import AutoHealState, FetchedFile, ProposedFix

logger = logging.getLogger(__name__)

_MAX_FILE_CHARS = 12_000


class FixPlanner(Protocol):
    """LLM-facing interface used by the planning node."""

    async def ainvoke(self, prompt: str) -> str:
        """Return a JSON string containing root cause, summary, and fixes."""


def _extract_latest_user_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                chunks: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        chunks.append(str(item.get("text", "")))
                return "\n".join(chunk for chunk in chunks if chunk)
            return str(content)
    return ""


class AutoHealNodes:
    """Node collection for the LangGraph version of the workflow."""

    def __init__(self, github: RepositoryClient, planner: FixPlanner) -> None:
        self.github = github
        self.planner = planner

    async def parse_input(self, state: AutoHealState) -> AutoHealState:
        raw_input = state.get("raw_input") or _extract_latest_user_text(state.get("messages", []))
        parsed = parse_failure_input(raw_input)
        source_branch = str(parsed["source_branch"])
        stack = str(parsed["stack"])
        build_id = str(parsed["build_id"])
        autoheal_branch = f"autoheal-{stack}/{source_branch}" if source_branch and stack else ""
        return {
            "raw_input": raw_input,
            **parsed,
            "autoheal_branch": autoheal_branch,
            "debug": {"parsed": parsed},
        }

    async def gather_context(self, state: AutoHealState) -> AutoHealState:
        source_branch = state.get("source_branch", "")
        autoheal_branch = state.get("autoheal_branch", "")
        if not source_branch or not autoheal_branch:
            return {"error": "Missing source_branch or autoheal_branch after parsing."}

        try:
            prs = await self.github.list_pull_requests(state="open", head=autoheal_branch)
        except GitHubError as exc:
            return {"error": f"Unable to list pull requests: {exc}"}

        existing_pr_url = prs[0].html_url if prs else "none"
        ref_to_read = autoheal_branch if prs else source_branch

        files: list[FetchedFile] = []
        for path in state.get("failing_files", []):
            try:
                file_content = await self.github.get_file_contents(path, ref_to_read)
            except GitHubError as exc:
                logger.warning("Context read failed for '%s': %s", path, exc)
                files.append(
                    {
                        "path": path,
                        "ref": ref_to_read,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                continue

            content = file_content.content
            truncated = False
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS]
                truncated = True

            files.append(
                {
                    "path": file_content.path,
                    "ref": ref_to_read,
                    "sha": file_content.sha,
                    "status": "ok",
                    "content": content,
                    "content_truncated": truncated,
                }
            )

        return {
            "existing_pr_url": existing_pr_url,
            "files": files,
        }

    async def plan_fix(self, state: AutoHealState) -> AutoHealState:
        if state.get("error"):
            return {}

        context = {
            "source_branch": state.get("source_branch", ""),
            "autoheal_branch": state.get("autoheal_branch", ""),
            "stack": state.get("stack", ""),
            "build_id": state.get("build_id", ""),
            "existing_pr_url": state.get("existing_pr_url", "none"),
            "errors": state.get("errors", []),
            "files": state.get("files", []),
        }
        prompt = build_fix_planning_prompt(context)
        raw_response = await self.planner.ainvoke(prompt)

        try:
            plan = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            return {"error": f"Planner returned invalid JSON: {exc}"}

        proposed_fixes = plan.get("proposed_fixes", [])
        if not isinstance(proposed_fixes, list):
            return {"error": "Planner returned invalid proposed_fixes payload."}

        normalized_fixes: list[ProposedFix] = []
        for item in proposed_fixes:
            if not isinstance(item, dict):
                return {"error": "Planner returned a non-object fix item."}
            normalized_fixes.append(
                {
                    "path": str(item.get("path", "")),
                    "content": str(item.get("content", "")),
                    "sha": str(item.get("sha", "")),
                    "error_code": str(item.get("error_code", "")).strip() or "build-error",
                }
            )

        return {
            "root_cause": str(plan.get("root_cause", "Build failed.")),
            "fix_summary": str(plan.get("fix_summary", "")),
            "proposed_fixes": normalized_fixes,
        }

    async def apply_fixes(self, state: AutoHealState) -> AutoHealState:
        source_branch = state.get("source_branch", "")
        autoheal_branch = state.get("autoheal_branch", "")
        fixes = state.get("proposed_fixes", [])
        existing_pr_url = state.get("existing_pr_url", "none")
        build_id = state.get("build_id", "")
        stack = state.get("stack", "")

        if fixes:
            try:
                if not await self.github.branch_exists(source_branch):
                    return {"error": f"Source branch '{source_branch}' does not exist."}
                if not await self.github.branch_exists(autoheal_branch):
                    await self.github.create_branch(autoheal_branch, source_branch)
            except GitHubError as exc:
                return {"error": f"Unable to prepare branch '{autoheal_branch}': {exc}"}

        modified_files: list[str] = []
        write_errors: list[str] = []
        for fix in fixes:
            path = fix["path"]
            commit_message = (
                f"fix: resolve {fix['error_code']} in {PurePosixPath(path).name} "
                f"(build {build_id})"
            )
            try:
                await self.github.create_or_update_file(
                    path=path,
                    content=fix["content"],
                    message=commit_message,
                    branch=autoheal_branch,
                    sha=fix["sha"] or None,
                )
            except GitHubError as exc:
                logger.warning("Write failed for '%s': %s", path, exc)
                write_errors.append(f"{path}: {exc}")
                continue
            modified_files.append(path)

        pull_request_url = existing_pr_url
        if modified_files and existing_pr_url == "none":
            body = (
                f"Root cause: {state.get('root_cause', '')}\n\n"
                f"Files changed: {', '.join(modified_files)}\n\n"
                f"Fix summary: {state.get('fix_summary', '')}\n\n"
                f"Build ID: {build_id}"
            )
            try:
                pr = await self.github.create_pull_request(
                    title=f"Auto-heal: Fix {stack} pipeline failure ({source_branch})",
                    body=body,
                    head=autoheal_branch,
                    base=source_branch,
                )
            except GitHubError as exc:
                write_errors.append(f"pull_request: {exc}")
            else:
                pull_request_url = pr.html_url

        fix_summary = state.get("fix_summary", "")
        if write_errors:
            fix_summary = f"{fix_summary} Write errors: {'; '.join(write_errors)}".strip()

        return {
            "fix_summary": fix_summary,
            "modified_files": modified_files,
            "pull_request_url": pull_request_url,
        }

    async def finalize(self, state: AutoHealState) -> AutoHealState:
        final_result = (
            f"Root Cause: {state.get('root_cause', state.get('error', 'Unknown failure'))}\n"
            f"Fix Applied: {state.get('fix_summary', '')}\n"
            f"Files Modified: {', '.join(state.get('modified_files', []))}\n"
            f"Branch: {state.get('autoheal_branch', '')}\n"
            f"Pull Request: {state.get('pull_request_url', state.get('existing_pr_url', 'none'))}\n"
            f"Build ID: {state.get('build_id', '')}"
        )
        return {
            "final_result": final_result,
            "messages": [AIMessage(content=final_result)],
        }


def should_apply_fixes(state: AutoHealState) -> str:
    if state.get("error"):
        return "finalize"
    if state.get("proposed_fixes"):
        return "apply_fixes"
    return "finalize"
