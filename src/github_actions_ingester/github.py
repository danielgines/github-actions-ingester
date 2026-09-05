"""GitHub REST API client — auth, pagination, rate-limit handling.

Everything the collector needs is a handful of documented endpoints:

  GET /orgs/{org}/repos                          PAT: repositories of an org
  GET /installation/repositories                 App: repositories the install sees
  GET /repos/{owner}/{repo}                      explicit repositories
  GET /repos/{owner}/{repo}/actions/workflows    workflow files
  GET /repos/{owner}/{repo}/actions/runs         runs, `created` filter
  GET /repos/{owner}/{repo}/actions/runs/{id}    single run refresh
  GET /repos/{owner}/{repo}/actions/runs/{id}/jobs
  GET /repos/{owner}/{repo}/contents/{path}      workflow YAML (schedules)
  GET /app/installations, POST /app/installations/{id}/access_tokens

Shapes verified against https://docs.github.com/en/rest (2022-11-28).

Rate limiting has three layers here:

  1. a client-side leaky bucket (``RateLimiter``) that paces every call;
  2. the primary-limit guard: when ``X-RateLimit-Remaining`` drops under
     ``min_remaining`` the client sleeps until ``X-RateLimit-Reset``;
  3. reactive retries on 403/429 (``Retry-After`` / reset header) and on
     5xx with exponential backoff.

The `runs` listing is capped by GitHub at 1000 results per query
regardless of pagination, so ``list_runs`` splits the ``created`` window
recursively until each slice fits.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import structlog

from .ratelimit import RateLimiter

logger = structlog.get_logger(__name__)

API_VERSION = "2022-11-28"
RUNS_QUERY_CAP = 1000  # documented ceiling of the /actions/runs listing
PER_PAGE = 100


class GitHubAPIError(Exception):
    """Non-retryable upstream failure (4xx other than rate limit)."""

    def __init__(self, endpoint: str, status: int, message: str = "") -> None:
        self.endpoint = endpoint
        self.status = status
        super().__init__(f"{endpoint} → HTTP {status} {message}".strip())


class GitHubRateLimitError(GitHubAPIError):
    """Raised when the retry budget runs out while rate-limited."""


@dataclass
class RateLimitState:
    limit: int = 0
    remaining: int = 0
    reset_at: float = 0.0  # epoch seconds
    used: int = 0

    def update(self, headers: httpx.Headers) -> None:
        try:
            self.limit = int(headers.get("x-ratelimit-limit", self.limit))
            self.remaining = int(headers.get("x-ratelimit-remaining", self.remaining))
            self.reset_at = float(headers.get("x-ratelimit-reset", self.reset_at))
            self.used = int(headers.get("x-ratelimit-used", self.used))
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TokenAuth:
    """Static bearer token (PAT)."""

    kind = "token"

    def __init__(self, token: str) -> None:
        self._token = token

    def token(self, _client: httpx.Client) -> str:
        return self._token


class AppAuth:
    """GitHub App: RS256 JWT → installation access token, cached until expiry."""

    kind = "app"

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        installation_id: str = "",
        preferred_owner: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._pem = private_key_pem
        self._installation_id = installation_id
        self._preferred_owner = preferred_owner.lower()
        self._clock = clock
        self._cached: str = ""
        self._expires_at: float = 0.0

    @property
    def installation_id(self) -> str:
        return self._installation_id

    def app_jwt(self) -> str:
        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self._app_id}
        return jwt.encode(payload, self._pem, algorithm="RS256")

    def token(self, client: httpx.Client) -> str:
        if self._cached and self._clock() < self._expires_at - 120:
            return self._cached
        headers = {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if not self._installation_id:
            self._installation_id = self._discover_installation(client, headers)
        resp = client.post(
            f"/app/installations/{self._installation_id}/access_tokens", headers=headers
        )
        if resp.status_code != 201:
            raise GitHubAPIError("/app/installations/*/access_tokens", resp.status_code, resp.text)
        body = resp.json()
        self._cached = str(body["token"])
        expires = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
        self._expires_at = expires.timestamp()
        logger.info("github.app_token_refreshed", installation_id=self._installation_id)
        return self._cached

    def _discover_installation(self, client: httpx.Client, headers: dict[str, str]) -> str:
        resp = client.get("/app/installations", headers=headers, params={"per_page": PER_PAGE})
        if resp.status_code != 200:
            raise GitHubAPIError("/app/installations", resp.status_code, resp.text)
        installs = resp.json()
        if not installs:
            raise GitHubAPIError("/app/installations", 404, "the App is not installed anywhere")
        if self._preferred_owner:
            for inst in installs:
                login = str(inst.get("account", {}).get("login", "")).lower()
                if login == self._preferred_owner:
                    return str(inst["id"])
        if len(installs) == 1:
            return str(installs[0]["id"])
        logins = [i.get("account", {}).get("login") for i in installs]
        raise GitHubAPIError(
            "/app/installations",
            409,
            f"several installations ({logins}); set GHA_GITHUB_APP_INSTALLATION_ID",
        )


# ---------------------------------------------------------------------------
# Typed shapes (only the fields the store persists)
# ---------------------------------------------------------------------------


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass
class Repository:
    id: int
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool
    archived: bool
    html_url: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Repository:
        return cls(
            id=int(d["id"]),
            owner=str(d["owner"]["login"]),
            name=str(d["name"]),
            full_name=str(d["full_name"]),
            default_branch=str(d.get("default_branch") or "main"),
            private=bool(d.get("private", False)),
            archived=bool(d.get("archived", False)),
            html_url=str(d.get("html_url", "")),
            raw=d,
        )


@dataclass
class Workflow:
    id: int
    repository_id: int
    name: str
    path: str
    state: str
    html_url: str
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_api(cls, repository_id: int, d: dict[str, Any]) -> Workflow:
        return cls(
            id=int(d["id"]),
            repository_id=repository_id,
            name=str(d.get("name") or d.get("path") or ""),
            path=str(d.get("path", "")),
            state=str(d.get("state", "")),
            html_url=str(d.get("html_url", "")),
            created_at=_ts(d.get("created_at")),
            updated_at=_ts(d.get("updated_at")),
        )


@dataclass
class WorkflowRun:
    id: int
    repository_id: int
    workflow_id: int
    run_number: int
    run_attempt: int
    name: str
    display_title: str
    event: str
    status: str
    conclusion: str | None
    head_branch: str | None
    head_sha: str
    actor: str
    triggering_actor: str
    created_at: datetime
    updated_at: datetime | None
    run_started_at: datetime | None
    html_url: str

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> WorkflowRun:
        created = _ts(d.get("created_at"))
        if created is None:
            raise ValueError(f"run {d.get('id')} has no created_at")
        return cls(
            id=int(d["id"]),
            repository_id=int(d["repository"]["id"]),
            workflow_id=int(d["workflow_id"]),
            run_number=int(d.get("run_number", 0)),
            run_attempt=int(d.get("run_attempt", 1)),
            name=str(d.get("name") or ""),
            display_title=str(d.get("display_title") or ""),
            event=str(d.get("event", "")),
            status=str(d.get("status") or ""),
            conclusion=d.get("conclusion"),
            head_branch=d.get("head_branch"),
            head_sha=str(d.get("head_sha", "")),
            actor=str((d.get("actor") or {}).get("login", "")),
            triggering_actor=str((d.get("triggering_actor") or {}).get("login", "")),
            created_at=created,
            updated_at=_ts(d.get("updated_at")),
            run_started_at=_ts(d.get("run_started_at")),
            html_url=str(d.get("html_url", "")),
        )

    @property
    def is_open(self) -> bool:
        return self.status != "completed"


@dataclass
class WorkflowJob:
    id: int
    run_id: int
    repository_id: int
    run_attempt: int
    name: str
    status: str
    conclusion: str | None
    runner_name: str | None
    runner_group_name: str | None
    labels: list[str]
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    steps: int
    html_url: str

    @classmethod
    def from_api(cls, repository_id: int, d: dict[str, Any]) -> WorkflowJob:
        return cls(
            id=int(d["id"]),
            run_id=int(d["run_id"]),
            repository_id=repository_id,
            run_attempt=int(d.get("run_attempt", 1)),
            name=str(d.get("name", "")),
            status=str(d.get("status") or ""),
            conclusion=d.get("conclusion"),
            runner_name=d.get("runner_name"),
            runner_group_name=d.get("runner_group_name"),
            labels=[str(x) for x in d.get("labels") or []],
            created_at=_ts(d.get("created_at")),
            started_at=_ts(d.get("started_at")),
            completed_at=_ts(d.get("completed_at")),
            steps=len(d.get("steps") or []),
            html_url=str(d.get("html_url", "")),
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    def __init__(
        self,
        auth: TokenAuth | AppAuth,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        limiter: RateLimiter | None = None,
        min_remaining: int = 200,
        max_retries: int = 4,
        on_request: Callable[[int], None] | None = None,
        on_rate_limit: Callable[[RateLimitState], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._auth = auth
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "github-actions-ingester",
            },
        )
        self._limiter = limiter or RateLimiter(5.0)
        self._min_remaining = min_remaining
        self._max_retries = max_retries
        self._on_request = on_request
        self._on_rate_limit = on_rate_limit
        self._sleep = sleep
        self._clock = clock
        self.rate_limit = RateLimitState()

    @property
    def auth_kind(self) -> str:
        return self._auth.kind

    def close(self) -> None:
        self._client.close()

    # -- low level ---------------------------------------------------------

    def _wait_for_primary_limit(self) -> None:
        if self.rate_limit.limit and self.rate_limit.remaining < self._min_remaining:
            wait = self.rate_limit.reset_at - self._clock() + 2
            if wait > 0:
                logger.warning(
                    "github.rate_limit_guard",
                    remaining=self.rate_limit.remaining,
                    min_remaining=self._min_remaining,
                    sleep_seconds=round(wait),
                )
                self._sleep(min(wait, 3600))
                # The reset moved us past the window; forget the stale count.
                self.rate_limit.remaining = self.rate_limit.limit

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with pacing, auth, rate-limit retries and 5xx backoff.

        ``path`` may be an absolute URL taken from a ``Link`` header; it is
        re-anchored on the configured base first (see ``_rebase``).
        """
        path = self._rebase(path)
        attempt = 0
        while True:
            self._wait_for_primary_limit()
            self._limiter.acquire()
            headers = {"Authorization": f"Bearer {self._auth.token(self._client)}"}
            try:
                resp = self._client.get(path, params=params, headers=headers)
            except httpx.HTTPError as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise GitHubAPIError(path, 0, f"transport error: {exc}") from exc
                logger.warning("github.transport_retry", path=path, attempt=attempt, error=str(exc))
                self._sleep(min(2**attempt, 30))
                continue
            self.rate_limit.update(resp.headers)
            if self._on_rate_limit is not None:
                self._on_rate_limit(self.rate_limit)
            if self._on_request is not None:
                self._on_request(resp.status_code)
            if resp.status_code < 400:
                return resp
            if resp.status_code in (403, 429) and self._is_rate_limited(resp):
                attempt += 1
                if attempt > self._max_retries:
                    raise GitHubRateLimitError(path, resp.status_code, "rate limit retries")
                delay = self._retry_delay(resp, attempt)
                logger.warning(
                    "github.rate_limited", path=path, status=resp.status_code, sleep=round(delay)
                )
                self._sleep(delay)
                continue
            if resp.status_code >= 500:
                attempt += 1
                if attempt > self._max_retries:
                    raise GitHubAPIError(path, resp.status_code, resp.text[:200])
                self._sleep(min(2**attempt, 30))
                continue
            raise GitHubAPIError(path, resp.status_code, resp.text[:200])

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return True
        if "retry-after" in resp.headers:
            return True
        text = resp.text.lower()
        return "rate limit" in text or "abuse" in text

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after) + 1
            except ValueError:
                pass
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = float(resp.headers.get("x-ratelimit-reset", "0") or 0)
            wait = reset - self._clock() + 2
            if wait > 0:
                return min(wait, 3600)
        return float(min(15 * 2**attempt, 300))

    def paginate(
        self, path: str, params: dict[str, Any] | None = None, key: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """Follow ``Link: rel=next`` until exhausted.

        ``key`` names the list inside an envelope (``workflows``,
        ``workflow_runs``, ``jobs``, ``repositories``); bare-list responses
        pass ``key=None``.
        """
        params = dict(params or {})
        params.setdefault("per_page", PER_PAGE)
        url: str | None = path
        first = True
        while url:
            resp = self.get(url, params=params if first else None)
            first = False
            body = resp.json()
            items = body[key] if key else body
            yield from items
            url = resp.links.get("next", {}).get("url")

    def _rebase(self, path: str) -> str:
        """Keep every request on the configured base URL.

        ``Link`` headers carry absolute URLs on GitHub's own host
        (``https://api.github.com/repositories/{id}/...``). When the client
        talks to something else, a GHES behind a different public name or
        a forwarding proxy, following them verbatim would leave that host
        behind after page 1, so only the path and query of the link are
        kept. Relative paths and same-origin URLs pass through untouched.
        """
        if not path.startswith(("http://", "https://")):
            return path
        link = httpx.URL(path)
        base = self._client.base_url
        if (link.scheme, link.host, link.port) == (base.scheme, base.host, base.port):
            return path
        return str(link.copy_with(scheme=base.scheme, host=base.host, port=base.port))

    # -- repositories --------------------------------------------------------

    def get_repository(self, full_name: str) -> Repository:
        return Repository.from_api(self.get(f"/repos/{full_name}").json())

    def list_org_repositories(self, org: str) -> Iterator[Repository]:
        for d in self.paginate(f"/orgs/{org}/repos", {"type": "all", "sort": "full_name"}):
            yield Repository.from_api(d)

    def list_installation_repositories(self) -> Iterator[Repository]:
        for d in self.paginate("/installation/repositories", key="repositories"):
            yield Repository.from_api(d)

    # -- workflows -----------------------------------------------------------

    def list_workflows(self, repo: Repository) -> Iterator[Workflow]:
        for d in self.paginate(f"/repos/{repo.full_name}/actions/workflows", key="workflows"):
            yield Workflow.from_api(repo.id, d)

    def get_file_text(self, repo: Repository, path: str, ref: str) -> str | None:
        """Raw file content from the default branch; None when missing."""
        try:
            resp = self.get(
                f"/repos/{repo.full_name}/contents/{path.lstrip('/')}",
                {"ref": ref},
            )
        except GitHubAPIError as exc:
            if exc.status == 404:
                return None
            raise
        body = resp.json()
        if body.get("encoding") == "base64" and body.get("content"):
            import base64

            return base64.b64decode(body["content"]).decode("utf-8", errors="replace")
        return None

    # -- runs ----------------------------------------------------------------

    def list_runs(
        self, repo: Repository, since: datetime, until: datetime | None = None
    ) -> Iterator[WorkflowRun]:
        """Runs created in ``[since, until]``, splitting windows over the API cap."""
        until = until or datetime.now(UTC)
        yield from self._list_runs_window(repo, since, until, depth=0)

    def _list_runs_window(
        self, repo: Repository, since: datetime, until: datetime, depth: int
    ) -> Iterator[WorkflowRun]:
        created = f"{_iso(since)}..{_iso(until)}"
        path = f"/repos/{repo.full_name}/actions/runs"
        first = self.get(path, {"created": created, "per_page": PER_PAGE, "page": 1})
        body = first.json()
        total = int(body.get("total_count", 0))
        if total > RUNS_QUERY_CAP and (until - since) > timedelta(minutes=5) and depth < 12:
            mid = since + (until - since) / 2
            logger.info(
                "github.runs_window_split",
                repo=repo.full_name,
                total=total,
                since=_iso(since),
                until=_iso(until),
            )
            yield from self._list_runs_window(repo, since, mid, depth + 1)
            yield from self._list_runs_window(repo, mid + timedelta(seconds=1), until, depth + 1)
            return
        for d in body.get("workflow_runs", []):
            yield WorkflowRun.from_api(d)
        url: str | None = first.links.get("next", {}).get("url")
        while url:
            resp = self.get(url)
            for d in resp.json().get("workflow_runs", []):
                yield WorkflowRun.from_api(d)
            url = resp.links.get("next", {}).get("url")

    def get_run(self, repo_full_name: str, run_id: int) -> WorkflowRun:
        return WorkflowRun.from_api(
            self.get(f"/repos/{repo_full_name}/actions/runs/{run_id}").json()
        )

    def list_jobs(
        self, repo: Repository, run_id: int, jobs_filter: str = "all"
    ) -> Iterator[WorkflowJob]:
        for d in self.paginate(
            f"/repos/{repo.full_name}/actions/runs/{run_id}/jobs",
            {"filter": jobs_filter},
            key="jobs",
        ):
            yield WorkflowJob.from_api(repo.id, d)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
