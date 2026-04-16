"""Shared state types for the LangGraph auto-heal workflow."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class FetchedFile(TypedDict):
    path: str
    ref: str
    status: str
    sha: NotRequired[str]
    content: NotRequired[str]
    content_truncated: NotRequired[bool]
    error: NotRequired[str]


class ProposedFix(TypedDict):
    path: str
    content: str
    sha: str
    error_code: str


class AutoHealState(TypedDict, total=False):
    messages: list[Any]
    raw_input: str
    source_branch: str
    stack: str
    build_id: str
    errors: list[str]
    failing_files: list[str]
    autoheal_branch: str
    existing_pr_url: str
    files: list[FetchedFile]
    root_cause: str
    fix_summary: str
    proposed_fixes: list[ProposedFix]
    modified_files: list[str]
    pull_request_url: str
    final_result: str
    error: str
    debug: dict[str, Any]
