"""Prompt builders for the LangGraph fix-planning step."""

from __future__ import annotations

import json


def build_fix_planning_prompt(context: dict[str, object]) -> str:
    context_block = json.dumps(context, indent=2)
    return f"""
You are generating a minimal safe code fix for a CI/CD auto-heal workflow.

Read the JSON context and produce a JSON object with this shape:
{{
  "root_cause": "one sentence",
  "fix_summary": "what changed and why",
  "proposed_fixes": [
    {{
      "path": "repo-relative file path",
      "content": "complete corrected file content",
      "sha": "sha from the context",
      "error_code": "best matching error code such as CS0103"
    }}
  ]
}}

Rules:
- Change only the lines needed to fix the reported build error.
- Do not invent file contents for files whose status is "error".
- If no safe fix can be made, return an empty proposed_fixes array and explain why.

Context:
{context_block}
""".strip()

