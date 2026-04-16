"""GitHub REST API client using GitHub App authentication."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt

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
