"""GitHub REST API client using GitHub App authentication."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

import httpx
import jwt
from azure.identity.aio import DefaultAzureCredential

logger = logging.getLogger(__name__)

_BASE = "https://api.github.com"

# JWT lifetime constants
_JWT_ISSUED_AT_SKEW = 60       # seconds in the past (clock-skew buffer)
_JWT_MAX_LIFETIME = 10 * 60    # 10 minutes (GitHub's maximum)
_TOKEN_REFRESH_BUFFER = 300    # refresh 5 minutes before expiry

# Retry configuration for transient GitHub API errors
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0      # seconds (doubles each attempt)
_RETRYABLE_STATUS = {500, 502, 503, 504}


@dataclass
class FileContent:
    path: str
    content: str
    sha: str
    encoding: str


@dataclass
class PullRequest:
    number: int
    html_url: str
    head_ref: str
    base_ref: str
    title: str
    state: str


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"GitHub API {status}: {message}")


class GitHubRateLimitError(GitHubError):
    """Raised when GitHub returns 429 Too Many Requests."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(429, f"Rate limited. Retry after {retry_after:.0f}s")


def _load_private_key() -> str:
    """Load the GitHub App private key from env var (path or PEM string)."""
    raw = os.environ["GITHUB_APP_PRIVATE_KEY"]
    if raw.startswith("-----BEGIN"):
        return raw
    # Treat as file path — resolve relative to project root
    key_path = Path(raw)
    if not key_path.is_absolute():
        key_path = Path(__file__).resolve().parent.parent / raw
    return key_path.read_text(encoding="utf-8")


def _build_jwt(app_id: str, private_key: str) -> str:
    """Create a short-lived JWT (10 min max) for GitHub App authentication."""
    now = int(time.time())
    payload = {
        "iat": now - _JWT_ISSUED_AT_SKEW,
        "exp": now + _JWT_MAX_LIFETIME,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def _parse_expires_at(expires_at: str | None) -> float:
    """
    Parse GitHub's ISO 8601 expires_at string to a Unix timestamp.
    Falls back to 1-hour window minus buffer if parsing fails.
    """
    if expires_at:
        try:
            dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            return dt.timestamp() - _TOKEN_REFRESH_BUFFER
        except ValueError:
            logger.warning("Could not parse expires_at='%s', using fallback.", expires_at)
    return time.time() + 3600 - _TOKEN_REFRESH_BUFFER


def _looks_like_git_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdefABCDEF" for ch in value)


class RepositoryClient(Protocol):
    async def close(self) -> None: ...
    async def branch_exists(self, branch: str) -> bool: ...
    async def get_branch_sha(self, branch: str) -> str: ...
    async def create_branch(self, branch: str, from_sha: str) -> str: ...
    async def get_file_contents(self, path: str, ref: str) -> FileContent: ...
    async def create_or_update_file(
        self,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> str: ...
    async def list_pull_requests(
        self, state: str = "open", head: str | None = None
    ) -> list[PullRequest]: ...
    async def create_pull_request(
        self, title: str, body: str, head: str, base: str
    ) -> PullRequest: ...


class GitHubClient:
    """Async GitHub REST API client using GitHub App installation tokens."""

    def __init__(
        self,
        app_id: str | None = None,
        installation_id: str | None = None,
        private_key: str | None = None,
    ) -> None:
        self._app_id = app_id or os.environ["GITHUB_APP_ID"]
        self._installation_id = installation_id or os.environ["GITHUB_APP_INSTALLATION_ID"]
        self._private_key = private_key or _load_private_key()
        self._owner = os.environ["GITHUB_REPO_OWNER"]
        self._repo = os.environ["GITHUB_REPO_NAME"]
        self._token: str | None = None
        self._token_expires_at: float = 0
        self._client = httpx.AsyncClient(
            base_url=_BASE,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── token management ──────────────────────────────────────────────

    async def _ensure_token(self) -> None:
        """Get or refresh the installation access token."""
        if self._token and time.time() < self._token_expires_at:
            return

        logger.debug("Refreshing GitHub installation token.")
        app_jwt = _build_jwt(self._app_id, self._private_key)
        resp = await self._client.post(
            f"/app/installations/{self._installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
        )
        if resp.status_code >= 400:
            raise GitHubError(
                resp.status_code,
                f"Failed to get installation token: {resp.text[:300]}",
            )
        data = resp.json()
        self._token = data["token"]
        self._token_expires_at = _parse_expires_at(data.get("expires_at"))
        logger.debug("Token refreshed, expires at %.0f.", self._token_expires_at)

    # ── helpers ────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"/repos/{self._owner}/{self._repo}{path}"

    async def _request(
        self, method: str, url: str, *, correlation_id: str | None = None, **kwargs
    ) -> httpx.Response:
        """
        Make an authenticated request with retry + rate-limit handling.

        Retries on transient 5xx errors with exponential backoff.
        Obeys the Retry-After header on 429 responses.
        """
        await self._ensure_token()
        req_id = correlation_id or str(uuid.uuid4())

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"
        headers["X-Correlation-ID"] = req_id

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await self._client.request(method, url, headers=headers, **kwargs)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "Request %s %s attempt %d/%d failed (%s). Retrying in %.1fs.",
                    method, url, attempt, _MAX_RETRIES, exc, wait,
                )
                await asyncio.sleep(wait)
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 60))
                logger.warning(
                    "Rate limited on %s %s. Waiting %.0fs (attempt %d/%d).",
                    method, url, retry_after, attempt, _MAX_RETRIES,
                )
                if attempt == _MAX_RETRIES:
                    raise GitHubRateLimitError(retry_after)
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code in _RETRYABLE_STATUS:
                wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "Got %d on %s %s (attempt %d/%d). Retrying in %.1fs.",
                    resp.status_code, method, url, attempt, _MAX_RETRIES, wait,
                )
                if attempt == _MAX_RETRIES:
                    return resp  # let _raise() handle it
                await asyncio.sleep(wait)
                continue

            logger.debug("Request %s %s → %d [%s]", method, url, resp.status_code, req_id)
            return resp

        # All retries exhausted due to connection/timeout errors
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise GitHubError(resp.status_code, resp.text[:400])

    # ── branch operations ─────────────────────────────────────────────

    async def branch_exists(self, branch: str) -> bool:
        resp = await self._request("GET", self._url(f"/branches/{branch}"))
        if resp.status_code == 404:
            return False
        self._raise(resp)
        return True

    async def get_branch_sha(self, branch: str) -> str:
        resp = await self._request("GET", self._url(f"/branches/{branch}"))
        self._raise(resp)
        return resp.json()["commit"]["sha"]

    async def create_branch(self, branch: str, from_sha: str) -> str:
        if not _looks_like_git_sha(from_sha):
            from_sha = await self.get_branch_sha(from_sha)
        resp = await self._request(
            "POST",
            self._url("/git/refs"),
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )
        self._raise(resp)
        return resp.json()["object"]["sha"]

    # ── file operations ───────────────────────────────────────────────

    async def get_file_contents(self, path: str, ref: str) -> FileContent:
        resp = await self._request(
            "GET",
            self._url(f"/contents/{path}"),
            params={"ref": ref},
        )
        self._raise(resp)
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return FileContent(
            path=data["path"],
            content=content,
            sha=data["sha"],
            encoding=data.get("encoding", "base64"),
        )

    async def create_or_update_file(
        self,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> str:
        body: dict = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        resp = await self._request("PUT", self._url(f"/contents/{path}"), json=body)
        if resp.status_code in {409, 422} and sha:
            logger.warning(
                "Initial update for '%s' on '%s' conflicted; refreshing SHA and retrying once.",
                path,
                branch,
            )
            latest = await self.get_file_contents(path, branch)
            body["sha"] = latest.sha
            resp = await self._request("PUT", self._url(f"/contents/{path}"), json=body)
        self._raise(resp)
        return resp.json()["content"]["sha"]

    # ── pull request operations ───────────────────────────────────────

    async def list_pull_requests(
        self, state: str = "open", head: str | None = None
    ) -> list[PullRequest]:
        params: dict = {"state": state, "per_page": "30"}
        if head:
            params["head"] = f"{self._owner}:{head}"
        resp = await self._request("GET", self._url("/pulls"), params=params)
        self._raise(resp)
        return [
            PullRequest(
                number=pr["number"],
                html_url=pr["html_url"],
                head_ref=pr["head"]["ref"],
                base_ref=pr["base"]["ref"],
                title=pr["title"],
                state=pr["state"],
            )
            for pr in resp.json()
        ]

    async def create_pull_request(
        self, title: str, body: str, head: str, base: str
    ) -> PullRequest:
        resp = await self._request(
            "POST",
            self._url("/pulls"),
            json={"title": title, "body": body, "head": head, "base": base},
        )
        self._raise(resp)
        pr = resp.json()
        return PullRequest(
            number=pr["number"],
            html_url=pr["html_url"],
            head_ref=pr["head"]["ref"],
            base_ref=pr["base"]["ref"],
            title=pr["title"],
            state=pr["state"],
        )

    async def get_pull_request(self, number: int) -> PullRequest:
        resp = await self._request("GET", self._url(f"/pulls/{number}"))
        self._raise(resp)
        pr = resp.json()
        return PullRequest(
            number=pr["number"],
            html_url=pr["html_url"],
            head_ref=pr["head"]["ref"],
            base_ref=pr["base"]["ref"],
            title=pr["title"],
            state=pr["state"],
        )


class MCPGitHubClient:
    """GitHub repository client backed by the official GitHub MCP server."""

    def __init__(self) -> None:
        self._owner = os.environ["GITHUB_REPO_OWNER"]
        self._repo = os.environ["GITHUB_REPO_NAME"]
        self._mcp_url = os.environ.get("GITHUB_MCP_URL", "").strip()
        self._mcp_command = os.environ.get("GITHUB_MCP_COMMAND", "").strip()
        self._mcp_args = self._load_json_env("GITHUB_MCP_ARGS_JSON", default=[])
        self._mcp_env = self._load_json_env("GITHUB_MCP_ENV_JSON", default={})
        self._mcp_headers = self._load_json_env("GITHUB_MCP_HEADERS_JSON", default={})
        if not self._mcp_url and not self._mcp_command:
            raise GitHubError(
                500,
                "GitHub MCP is enabled but no transport is configured. "
                "Set GITHUB_MCP_URL or GITHUB_MCP_COMMAND.",
            )

    @staticmethod
    def _load_json_env(name: str, *, default: Any) -> Any:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubError(500, f"{name} must contain valid JSON: {exc}") from exc

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        if self._mcp_url:
            headers = {
                str(key): str(value) for key, value in dict(self._mcp_headers).items()
            }
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as http_client:
                async with streamable_http_client(self._mcp_url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        yield session
            return

        env = {
            str(key): str(value) for key, value in dict(self._mcp_env).items()
        }
        server_params = StdioServerParameters(
            command=self._mcp_command,
            args=[str(arg) for arg in list(self._mcp_args)],
            env=env or None,
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    @staticmethod
    def _extract_text(result: Any) -> str:
        chunks: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text:
                chunks.append(str(text))
                continue
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text", "")))
        return "\n".join(chunk for chunk in chunks if chunk)

    @classmethod
    def _normalize_tool_result(cls, result: Any) -> Any:
        if result is None:
            return {}
        if isinstance(result, (dict, list)):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {"text": result}

        for attr in ("structuredContent", "structured_content"):
            payload = getattr(result, attr, None)
            if payload is not None:
                return cls._normalize_tool_result(payload)

        if hasattr(result, "model_dump"):
            try:
                return cls._normalize_tool_result(result.model_dump(mode="python"))
            except TypeError:
                return cls._normalize_tool_result(result.model_dump())

        text = cls._extract_text(result)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}

        if hasattr(result, "__dict__"):
            payload = {
                key: value
                for key, value in vars(result).items()
                if not key.startswith("_")
            }
            if payload:
                return cls._normalize_tool_result(payload)

        return {}

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("items", "results", "pull_requests", "pullRequests", "branches"):
                candidate = value.get(key)
                if isinstance(candidate, list):
                    return candidate
        return []

    @staticmethod
    def _dig(value: Any, *paths: tuple[str, ...]) -> str | None:
        for path in paths:
            current = value
            ok = True
            for part in path:
                if not isinstance(current, dict) or part not in current:
                    ok = False
                    break
                current = current[part]
            if ok and current not in (None, ""):
                return str(current)
        return None

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            async with self._session() as session:
                result = await session.call_tool(name, arguments=arguments)
        except Exception as exc:
            raise GitHubError(500, f"MCP tool '{name}' failed: {exc}") from exc

        if getattr(result, "isError", False):
            raise GitHubError(500, self._extract_text(result) or f"MCP tool '{name}' returned an error.")

        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured

        text = self._extract_text(result)
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    def _repo_args(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "owner": self._owner,
            "repo": self._repo,
            **kwargs,
        }

    @staticmethod
    def _normalize_ref(ref: str) -> str:
        if not ref or ref.startswith("refs/") or _looks_like_git_sha(ref):
            return ref
        return f"refs/heads/{ref}"

    async def close(self) -> None:
        return None

    async def branch_exists(self, branch: str) -> bool:
        try:
            await self.get_branch_sha(branch)
            return True
        except GitHubError as exc:
            message = str(exc).lower()
            if any(token in message for token in ("404", "not found", "does not exist", "unknown revision")):
                return False
            raise

    async def get_branch_sha(self, branch: str) -> str:
        result = await self._call_tool(
            "get_commit",
            self._repo_args(sha=branch, include_diff=False),
        )
        sha = self._dig(result, ("sha",), ("commit", "sha"), ("oid",))
        if not sha:
            raise GitHubError(500, f"Unable to resolve SHA for branch '{branch}' through MCP.")
        return sha

    async def create_branch(self, branch: str, from_sha: str) -> str:
        if _looks_like_git_sha(from_sha):
            raise GitHubError(
                400,
                "GitHub MCP create_branch requires a source branch name, not a raw SHA.",
            )
        await self._call_tool(
            "create_branch",
            self._repo_args(branch=branch, from_branch=from_sha),
        )
        return await self.get_branch_sha(branch)

    async def get_file_contents(self, path: str, ref: str) -> FileContent:
        result = await self._call_tool(
            "get_file_contents",
            self._repo_args(path=path, ref=self._normalize_ref(ref)),
        )
        if isinstance(result, list):
            raise GitHubError(400, f"MCP returned directory content for '{path}', expected a file.")

        resolved_path = self._dig(result, ("path",)) or path
        content = self._dig(result, ("content",), ("text",))
        sha = self._dig(result, ("sha",))
        if content is None or sha is None:
            raise GitHubError(500, f"MCP response for '{path}' did not include file content and SHA.")
        return FileContent(
            path=resolved_path,
            content=content,
            sha=sha,
            encoding=str(result.get("encoding", "utf-8")) if isinstance(result, dict) else "utf-8",
        )

    async def create_or_update_file(
        self,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> str:
        result = await self._call_tool(
            "create_or_update_file",
            self._repo_args(
                path=path,
                content=content,
                message=message,
                branch=branch,
                sha=sha,
            ),
        )
        new_sha = self._dig(result, ("content", "sha"), ("sha",))
        if new_sha:
            return new_sha
        latest = await self.get_file_contents(path, branch)
        return latest.sha

    async def list_pull_requests(
        self, state: str = "open", head: str | None = None
    ) -> list[PullRequest]:
        result = await self._call_tool(
            "list_pull_requests",
            self._repo_args(
                state=state,
                head=f"{self._owner}:{head}" if head else None,
                perPage=30,
            ),
        )
        prs: list[PullRequest] = []
        for item in self._as_list(result):
            if not isinstance(item, dict):
                continue
            prs.append(
                PullRequest(
                    number=int(item.get("number", 0)),
                    html_url=str(item.get("html_url") or item.get("url") or ""),
                    head_ref=str(
                        self._dig(item, ("head", "ref"), ("headRefName",), ("head_ref",)) or ""
                    ),
                    base_ref=str(
                        self._dig(item, ("base", "ref"), ("baseRefName",), ("base_ref",)) or ""
                    ),
                    title=str(item.get("title", "")),
                    state=str(item.get("state", state)),
                )
            )
        return prs

    async def create_pull_request(
        self, title: str, body: str, head: str, base: str
    ) -> PullRequest:
        result = await self._call_tool(
            "create_pull_request",
            self._repo_args(title=title, body=body, head=head, base=base),
        )
        number = int(result.get("number", 0)) if isinstance(result, dict) else 0
        return PullRequest(
            number=number,
            html_url=str(result.get("html_url") or result.get("url") or "") if isinstance(result, dict) else "",
            head_ref=str(self._dig(result, ("head", "ref"), ("head",), ("headRefName",)) or head),
            base_ref=str(self._dig(result, ("base", "ref"), ("base",), ("baseRefName",)) or base),
            title=str(result.get("title", title)) if isinstance(result, dict) else title,
            state=str(result.get("state", "open")) if isinstance(result, dict) else "open",
        )


class FoundryMCPGitHubClient(MCPGitHubClient):
    """GitHub repository client backed by a Foundry-managed MCP connection."""

    def __init__(self) -> None:
        self._owner = os.environ["GITHUB_REPO_OWNER"]
        self._repo = os.environ["GITHUB_REPO_NAME"]
        self._project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        self._project_connection_id = os.environ["GITHUB_MCP_CONNECTION_ID"]
        self._server_label = os.environ.get("GITHUB_MCP_SERVER_LABEL", "github")
        self._require_approval = os.environ.get("GITHUB_MCP_REQUIRE_APPROVAL", "never")
        self._allowed_tools = self._load_json_env("GITHUB_MCP_ALLOWED_TOOLS_JSON", default=[])
        self._credential = DefaultAzureCredential()

    @property
    def _tool_definition(self) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "type": "mcp",
            "project_connection_id": self._project_connection_id,
            "server_label": self._server_label,
            "require_approval": self._require_approval,
        }
        if self._allowed_tools:
            tool["allowed_tools"] = self._allowed_tools
        return tool

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from azure.ai.agentserver.core.tools import DefaultFoundryToolRuntime

        runtime = DefaultFoundryToolRuntime(
            project_endpoint=self._project_endpoint,
            credential=self._credential,
        )
        try:
            result = await runtime.invoke(
                self._tool_definition,
                {
                    "tool_name": name,
                    "arguments": arguments,
                },
            )
        except Exception as exc:
            raise GitHubError(500, f"Foundry MCP tool '{name}' failed: {exc}") from exc

        return self._normalize_tool_result(result)

    async def close(self) -> None:
        await self._credential.close()


def create_repository_client() -> RepositoryClient:
    return FoundryMCPGitHubClient()
