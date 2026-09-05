"""GitHub client against a mocked API (pytest-httpx)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from pytest_httpx import HTTPXMock

from github_actions_ingester.github import (
    API_VERSION,
    AppAuth,
    GitHubAPIError,
    GitHubClient,
    GitHubRateLimitError,
    RateLimitState,
    Repository,
    TokenAuth,
    Workflow,
    WorkflowJob,
    WorkflowRun,
)
from github_actions_ingester.ratelimit import RateLimiter

API = "https://api.github.com"
REPO = Repository(1, "acme", "web", "acme/web", "main", False, False, "https://github.com/acme/web")


def _client(auth: TokenAuth | AppAuth | None = None, **kw: Any) -> GitHubClient:
    """Client without real pacing; ``sleep`` calls are recorded in ``SLEEPS``."""
    kw.setdefault("sleep", SLEEPS.append)
    kw.setdefault("limiter", RateLimiter(10_000.0))
    return GitHubClient(auth or TokenAuth("tok"), base_url=API, **kw)


SLEEPS: list[float] = []


@pytest.fixture(autouse=True)
def _reset_sleeps() -> None:
    SLEEPS.clear()


def _run(id_: int, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": id_,
        "repository": {"id": 1},
        "workflow_id": 10,
        "run_number": id_,
        "run_attempt": 1,
        "name": "CI",
        "display_title": "title",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": "abc",
        "actor": {"login": "dev"},
        "triggering_actor": {"login": "dev"},
        "created_at": "2026-01-05T10:00:00Z",
        "updated_at": "2026-01-05T10:05:00Z",
        "run_started_at": "2026-01-05T10:00:30Z",
        "html_url": "https://github.com/acme/web/actions/runs/1",
    }
    d.update(over)
    return d


# -- headers / auth ---------------------------------------------------------


def test_request_headers(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web",
        json={"id": 1, "owner": {"login": "acme"}, "name": "web", "full_name": "acme/web"},
    )
    repo = _client().get_repository("acme/web")
    req = httpx_mock.get_request()
    assert req is not None
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == API_VERSION
    assert req.headers["User-Agent"] == "github-actions-ingester"
    assert repo.full_name == "acme/web"
    assert repo.default_branch == "main"


def test_app_auth_jwt_and_installation_token(
    httpx_mock: HTTPXMock, rsa_private_key_pem: str
) -> None:
    clock = [1_700_000_000.0]
    auth = AppAuth("12345", rsa_private_key_pem, installation_id="77", clock=lambda: clock[0])
    httpx_mock.add_response(
        method="POST",
        url=f"{API}/app/installations/77/access_tokens",
        status_code=201,
        json={"token": "ghs_inst", "expires_at": "2023-11-14T23:13:20Z"},  # +3600s
    )
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web",
        json={"id": 1, "owner": {"login": "acme"}, "name": "web", "full_name": "acme/web"},
        is_reusable=True,
    )
    c = _client(auth)
    c.get_repository("acme/web")
    c.get_repository("acme/web")  # cached token: no second POST
    reqs = httpx_mock.get_requests()
    posts = [r for r in reqs if r.method == "POST"]
    assert len(posts) == 1
    payload = jwt.decode(
        posts[0].headers["Authorization"].removeprefix("Bearer "),
        options={"verify_signature": False},
    )
    assert payload["iss"] == "12345"
    assert payload["exp"] - payload["iat"] == 10 * 60
    assert [r.headers["Authorization"] for r in reqs if r.method == "GET"] == [
        "Bearer ghs_inst"
    ] * 2
    assert c.auth_kind == "app"


def test_app_auth_refreshes_near_expiry(httpx_mock: HTTPXMock, rsa_private_key_pem: str) -> None:
    clock = [1_700_000_000.0]
    auth = AppAuth("1", rsa_private_key_pem, installation_id="77", clock=lambda: clock[0])
    httpx_mock.add_response(
        method="POST",
        url=f"{API}/app/installations/77/access_tokens",
        status_code=201,
        json={"token": "first", "expires_at": "2023-11-14T23:13:20Z"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{API}/app/installations/77/access_tokens",
        status_code=201,
        json={"token": "second", "expires_at": "2023-11-15T00:13:20Z"},
    )
    with httpx.Client(base_url=API) as raw:
        assert auth.token(raw) == "first"
        clock[0] += 3600 - 121  # still inside the 120 s safety margin? no: 1 s before it
        assert auth.token(raw) == "first"
        clock[0] += 2
        assert auth.token(raw) == "second"


def test_app_auth_discovers_installation_by_owner(
    httpx_mock: HTTPXMock, rsa_private_key_pem: str
) -> None:
    auth = AppAuth("1", rsa_private_key_pem, preferred_owner="Acme")
    httpx_mock.add_response(
        url=f"{API}/app/installations?per_page=100",
        json=[{"id": 5, "account": {"login": "other"}}, {"id": 9, "account": {"login": "acme"}}],
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{API}/app/installations/9/access_tokens",
        status_code=201,
        json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"},
    )
    with httpx.Client(base_url=API) as raw:
        assert auth.token(raw) == "t"
    assert auth.installation_id == "9"


def test_app_auth_single_installation_fallback(
    httpx_mock: HTTPXMock, rsa_private_key_pem: str
) -> None:
    auth = AppAuth("1", rsa_private_key_pem, preferred_owner="nomatch")
    httpx_mock.add_response(
        url=f"{API}/app/installations?per_page=100", json=[{"id": 5, "account": {"login": "x"}}]
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{API}/app/installations/5/access_tokens",
        status_code=201,
        json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"},
    )
    with httpx.Client(base_url=API) as raw:
        auth.token(raw)
    assert auth.installation_id == "5"


def test_app_auth_ambiguous_installations(httpx_mock: HTTPXMock, rsa_private_key_pem: str) -> None:
    auth = AppAuth("1", rsa_private_key_pem)
    httpx_mock.add_response(
        url=f"{API}/app/installations?per_page=100",
        json=[{"id": 5, "account": {"login": "a"}}, {"id": 6, "account": {"login": "b"}}],
    )
    with httpx.Client(base_url=API) as raw, pytest.raises(GitHubAPIError) as exc:
        auth.token(raw)
    assert exc.value.status == 409
    assert "GHA_GITHUB_APP_INSTALLATION_ID" in str(exc.value)


def test_app_auth_not_installed(httpx_mock: HTTPXMock, rsa_private_key_pem: str) -> None:
    auth = AppAuth("1", rsa_private_key_pem)
    httpx_mock.add_response(url=f"{API}/app/installations?per_page=100", json=[])
    with httpx.Client(base_url=API) as raw, pytest.raises(GitHubAPIError) as exc:
        auth.token(raw)
    assert exc.value.status == 404


def test_app_auth_token_endpoint_error(httpx_mock: HTTPXMock, rsa_private_key_pem: str) -> None:
    auth = AppAuth("1", rsa_private_key_pem, installation_id="77")
    httpx_mock.add_response(
        method="POST", url=f"{API}/app/installations/77/access_tokens", status_code=401, text="bad"
    )
    with httpx.Client(base_url=API) as raw, pytest.raises(GitHubAPIError) as exc:
        auth.token(raw)
    assert exc.value.status == 401


# -- rate limit / retries ---------------------------------------------------------


def test_rate_limit_state_from_headers() -> None:
    st = RateLimitState()
    st.update(
        httpx.Headers(
            {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "42",
                "x-ratelimit-reset": "1700000000",
                "x-ratelimit-used": "4958",
            }
        )
    )
    assert (st.limit, st.remaining, st.reset_at, st.used) == (5000, 42, 1700000000.0, 4958)
    st.update(httpx.Headers({"x-ratelimit-remaining": "garbage"}))
    assert st.remaining == 42  # unparsable header leaves the state untouched


def test_403_rate_limited_retries_until_reset(httpx_mock: HTTPXMock) -> None:
    now = 1_000.0
    httpx_mock.add_response(
        url=f"{API}/x",
        status_code=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now + 30))},
        text="API rate limit exceeded",
    )
    httpx_mock.add_response(url=f"{API}/x", json={"ok": True})
    c = _client(clock=lambda: now)
    assert c.get("/x").json() == {"ok": True}
    assert SLEEPS == [32.0]  # reset - now + 2


def test_429_uses_retry_after(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{API}/x", status_code=429, headers={"retry-after": "7"})
    httpx_mock.add_response(url=f"{API}/x", json={})
    c = _client()
    c.get("/x")
    assert SLEEPS == [8.0]


def test_rate_limit_retry_budget_exhausted(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/x", status_code=429, headers={"retry-after": "1"}, is_reusable=True
    )
    c = _client(max_retries=2)
    with pytest.raises(GitHubRateLimitError):
        c.get("/x")
    assert len(httpx_mock.get_requests()) == 3


def test_403_without_rate_limit_markers_is_not_retried(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/x", status_code=403, text="Resource not accessible by integration"
    )
    c = _client()
    with pytest.raises(GitHubAPIError) as exc:
        c.get("/x")
    assert exc.value.status == 403
    assert not isinstance(exc.value, GitHubRateLimitError)
    assert SLEEPS == []


def test_5xx_backoff_then_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{API}/x", status_code=502)
    httpx_mock.add_response(url=f"{API}/x", status_code=503)
    httpx_mock.add_response(url=f"{API}/x", json={"n": 1})
    c = _client()
    assert c.get("/x").json() == {"n": 1}
    assert SLEEPS == [2.0, 4.0]


def test_transport_error_retried(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=f"{API}/x")
    httpx_mock.add_response(url=f"{API}/x", json={})
    c = _client()
    c.get("/x")
    assert SLEEPS == [2.0]


def test_transport_error_budget(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("slow"), url=f"{API}/x", is_reusable=True)
    c = _client(max_retries=1)
    with pytest.raises(GitHubAPIError) as exc:
        c.get("/x")
    assert exc.value.status == 0


def test_primary_limit_guard_sleeps_before_next_call(httpx_mock: HTTPXMock) -> None:
    now = 5_000.0
    httpx_mock.add_response(
        url=f"{API}/a",
        json={},
        headers={
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "10",
            "x-ratelimit-reset": str(int(now + 100)),
        },
    )
    httpx_mock.add_response(url=f"{API}/b", json={}, headers={"x-ratelimit-remaining": "4999"})
    c = _client(min_remaining=200, clock=lambda: now)
    c.get("/a")
    assert SLEEPS == []
    c.get("/b")
    assert SLEEPS == [102.0]


def test_on_request_hook_receives_status(httpx_mock: HTTPXMock) -> None:
    seen: list[int] = []
    httpx_mock.add_response(url=f"{API}/x", status_code=404)
    with pytest.raises(GitHubAPIError):
        _client(on_request=seen.append).get("/x")
    assert seen == [404]


# -- pagination / listings ---------------------------------------------------------


def test_paginate_follows_link_header(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/orgs/acme/repos?type=all&sort=full_name&per_page=100",
        json=[{"id": 1, "owner": {"login": "acme"}, "name": "a", "full_name": "acme/a"}],
        headers={"Link": f'<{API}/orgs/acme/repos?per_page=100&page=2>; rel="next"'},
    )
    httpx_mock.add_response(
        url=f"{API}/orgs/acme/repos?per_page=100&page=2",
        json=[
            {
                "id": 2,
                "owner": {"login": "acme"},
                "name": "b",
                "full_name": "acme/b",
                "archived": True,
            }
        ],
    )
    repos = list(_client().list_org_repositories("acme"))
    assert [r.full_name for r in repos] == ["acme/a", "acme/b"]
    assert repos[1].archived is True


def test_paginate_keeps_the_configured_base_when_link_points_elsewhere(
    httpx_mock: HTTPXMock,
) -> None:
    """GitHub's Link header is absolute on its own host; a proxy or a GHES
    with a public name must not be left behind on page 2."""
    proxy = "http://gh-proxy.local:18097"
    httpx_mock.add_response(
        url=f"{proxy}/orgs/acme/repos?type=all&sort=full_name&per_page=100",
        json=[{"id": 1, "owner": {"login": "acme"}, "name": "a", "full_name": "acme/a"}],
        headers={
            "Link": '<https://api.github.com/repositories/9/repos?per_page=100&page=2>; rel="next"'
        },
    )
    httpx_mock.add_response(
        url=f"{proxy}/repositories/9/repos?per_page=100&page=2",
        json=[{"id": 2, "owner": {"login": "acme"}, "name": "b", "full_name": "acme/b"}],
    )
    client = GitHubClient(TokenAuth("tok"), base_url=proxy, limiter=RateLimiter(10_000))
    assert [r.full_name for r in client.list_org_repositories("acme")] == ["acme/a", "acme/b"]
    # list_runs pages on its own (window splitting) and must rebase as well
    httpx_mock.add_response(
        url=f"{proxy}/repos/acme/web/actions/runs?created=2026-01-01T00%3A00%3A00Z..2026-01-02T00%3A00%3A00Z&per_page=100&page=1",
        json={"total_count": 2, "workflow_runs": [_run(1)]},
        headers={
            "Link": '<https://api.github.com/repositories/1/actions/runs?created=x&per_page=100&page=2>; rel="next"'
        },
    )
    httpx_mock.add_response(
        url=f"{proxy}/repositories/1/actions/runs?created=x&per_page=100&page=2",
        json={"total_count": 2, "workflow_runs": [_run(2)]},
    )
    since = datetime(2026, 1, 1, tzinfo=UTC)
    runs = list(client.list_runs(REPO, since, datetime(2026, 1, 2, tzinfo=UTC)))
    assert [r.id for r in runs] == [1, 2]


def test_paginate_preserves_path_prefix_of_enterprise_base(httpx_mock: HTTPXMock) -> None:
    base = "https://ghe.example.com/api/v3"
    httpx_mock.add_response(
        url=f"{base}/orgs/acme/repos?type=all&sort=full_name&per_page=100",
        json=[],
        headers={
            "Link": '<https://ghe-internal.example.com/api/v3/orgs/acme/repos?per_page=100&page=2>; rel="next"'
        },
    )
    httpx_mock.add_response(url=f"{base}/orgs/acme/repos?per_page=100&page=2", json=[])
    client = GitHubClient(TokenAuth("tok"), base_url=base, limiter=RateLimiter(10_000))
    assert list(client.list_org_repositories("acme")) == []


def test_list_installation_repositories_envelope(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/installation/repositories?per_page=100",
        json={
            "total_count": 1,
            "repositories": [
                {"id": 3, "owner": {"login": "acme"}, "name": "c", "full_name": "acme/c"}
            ],
        },
    )
    assert [r.id for r in _client().list_installation_repositories()] == [3]


def test_list_workflows(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/actions/workflows?per_page=100",
        json={
            "total_count": 1,
            "workflows": [
                {
                    "id": 10,
                    "name": "CI",
                    "path": ".github/workflows/ci.yml",
                    "state": "active",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        },
    )
    wfs = list(_client().list_workflows(REPO))
    assert wfs == [
        Workflow(
            10,
            1,
            "CI",
            ".github/workflows/ci.yml",
            "active",
            "",
            datetime(2026, 1, 1, tzinfo=UTC),
            None,
        )
    ]


def test_get_file_text_decodes_base64(httpx_mock: HTTPXMock) -> None:
    content = base64.b64encode(b"on:\n  schedule:\n    - cron: '0 * * * *'\n").decode()
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/contents/.github/workflows/ci.yml?ref=main",
        json={"encoding": "base64", "content": content},
    )
    text = _client().get_file_text(REPO, ".github/workflows/ci.yml", "main")
    assert text is not None and "cron" in text


def test_get_file_text_missing_is_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/contents/.github/workflows/gone.yml?ref=main", status_code=404
    )
    assert _client().get_file_text(REPO, ".github/workflows/gone.yml", "main") is None


def test_list_runs_single_window_with_pages(httpx_mock: HTTPXMock) -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = datetime(2026, 1, 2, tzinfo=UTC)
    base = f"{API}/repos/acme/web/actions/runs"
    httpx_mock.add_response(
        url=f"{base}?created=2026-01-01T00%3A00%3A00Z..2026-01-02T00%3A00%3A00Z&per_page=100&page=1",
        json={"total_count": 2, "workflow_runs": [_run(1)]},
        headers={"Link": f'<{base}?page=2>; rel="next"'},
    )
    httpx_mock.add_response(
        url=f"{base}?page=2",
        json={"total_count": 2, "workflow_runs": [_run(2, status="in_progress", conclusion=None)]},
    )
    runs = list(_client().list_runs(REPO, since, until))
    assert [r.id for r in runs] == [1, 2]
    assert runs[0].is_open is False and runs[1].is_open is True
    assert runs[0].actor == "dev"


def test_list_runs_splits_window_over_api_cap(httpx_mock: HTTPXMock) -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = datetime(2026, 1, 3, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        created = request.url.params["created"]
        lo, hi = created.split("..")
        if lo == "2026-01-01T00:00:00Z" and hi == "2026-01-03T00:00:00Z":
            return httpx.Response(200, json={"total_count": 1500, "workflow_runs": [_run(999)]})
        # halves: [01-01, 01-02] and [01-02 +1s, 01-03]
        n = 1 if lo == "2026-01-01T00:00:00Z" else 2
        return httpx.Response(200, json={"total_count": 700, "workflow_runs": [_run(n)]})

    httpx_mock.add_callback(handler, is_reusable=True)
    runs = list(_client().list_runs(REPO, since, until))
    assert [r.id for r in runs] == [1, 2]  # the capped first page is discarded
    windows = [r.url.params["created"] for r in httpx_mock.get_requests()]
    assert windows == [
        "2026-01-01T00:00:00Z..2026-01-03T00:00:00Z",
        "2026-01-01T00:00:00Z..2026-01-02T00:00:00Z",
        "2026-01-02T00:00:01Z..2026-01-03T00:00:00Z",
    ]


def test_get_run_and_list_jobs(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{API}/repos/acme/web/actions/runs/1", json=_run(1))
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/actions/runs/1/jobs?filter=latest&per_page=100",
        json={
            "total_count": 1,
            "jobs": [
                {
                    "id": 100,
                    "run_id": 1,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "runner_name": "r1",
                    "labels": ["ubuntu-latest"],
                    "started_at": "2026-01-05T10:00:40Z",
                    "completed_at": "2026-01-05T10:04:00Z",
                    "steps": [{}, {}, {}],
                }
            ],
        },
    )
    c = _client()
    run = c.get_run("acme/web", 1)
    assert isinstance(run, WorkflowRun) and run.workflow_id == 10
    jobs = list(c.list_jobs(REPO, 1, "latest"))
    assert jobs[0] == WorkflowJob(
        100,
        1,
        1,
        1,
        "build",
        "completed",
        "success",
        "r1",
        None,
        ["ubuntu-latest"],
        None,
        datetime(2026, 1, 5, 10, 0, 40, tzinfo=UTC),
        datetime(2026, 1, 5, 10, 4, tzinfo=UTC),
        3,
        "",
    )


def test_run_without_created_at_rejected() -> None:
    with pytest.raises(ValueError, match="created_at"):
        WorkflowRun.from_api({"id": 1, "repository": {"id": 1}, "workflow_id": 1})


def test_error_message_is_truncated(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{API}/x", status_code=422, text=json.dumps({"m": "x" * 500}))
    with pytest.raises(GitHubAPIError) as exc:
        _client().get("/x")
    assert len(str(exc.value)) <= 230
    assert exc.value.endpoint == "/x" and exc.value.status == 422
