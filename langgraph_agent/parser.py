"""Deterministic parsing helpers for pipeline failure messages."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_SOURCE_BRANCH_RE = re.compile(r"Pipeline Source Branch:\s*(?P<value>\S+)")
_BUILD_ID_RE = re.compile(r"Build ID:\s*(?P<value>\S+)")
_STACK_RE = re.compile(r"Stack:\s*(?P<value>\S+)")
_ERROR_LINE_RE = re.compile(r"^.*\berror\s+[A-Z]{2,}\d+:\s+.*$", re.MULTILINE)
_FILE_PATH_RE = re.compile(r"/home/vsts/work/1/s/(?P<path>[^\s:(]+)")


def normalize_repo_path(path: str) -> str:
    normalized = str(PurePosixPath(path.strip()))
    prefix = "/home/vsts/work/1/s/"
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix):]
    return "" if normalized == "." else normalized


def _match_or_empty(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group("value") if match else ""


def parse_failure_input(raw_input: str) -> dict[str, object]:
    source_branch = _match_or_empty(_SOURCE_BRANCH_RE, raw_input)
    build_id = _match_or_empty(_BUILD_ID_RE, raw_input)
    stack = _match_or_empty(_STACK_RE, raw_input)

    errors: list[str] = []
    seen_errors: set[str] = set()
    for match in _ERROR_LINE_RE.finditer(raw_input):
        line = match.group(0).strip()
        if line not in seen_errors:
            errors.append(line)
            seen_errors.add(line)

    failing_files: list[str] = []
    seen_paths: set[str] = set()
    for match in _FILE_PATH_RE.finditer(raw_input):
        path = normalize_repo_path(match.group("path"))
        if path and path not in seen_paths:
            failing_files.append(path)
            seen_paths.add(path)

    return {
        "source_branch": source_branch,
        "build_id": build_id,
        "stack": stack,
        "errors": errors,
        "failing_files": failing_files,
    }

