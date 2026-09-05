"""Collector against a real store and a scripted GitHub client."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from github_actions_ingester.collector import Collector
from github_actions_ingester.config import Settings, load_settings
from github_actions_ingester.github import (
    GitHubAPIError,
    GitHubRateLimitError,
    RateLimitState,
    Repository,
    Workflow,
    WorkflowJob,
    WorkflowRun,
)
from github_actions_ingester.metrics import Metrics
from github_actions_ingester.store import Store
from tests.helpers import NOW, job, repo, run, workflow

pytestmark = pytest.mark.integration

WEB, API, OLD = repo(1, "acme/web"), repo(2, "acme/api"), repo(3, "acme/old", archived=True)


class FakeGitHub:
    """Just enough of GitHubClient for the collector; records every call."""

    def __init__(self) -> None:
        self.rate_limit = RateLimitState(limit=5000, remaining=4990, reset_at=1.0)
        self.calls: list[tuple[str, Any]] = []
        self.org_repos: dict[str, list[Repository]] = {"acme": [WEB, API, OLD]}
        self.install_repos: list[Repository] = [WEB, API]
        self.repos: dict[str, Repository] = {r.full_name: r for r in (WEB, API, OLD)}
        self.workflows: dict[int, list[Workflow]] = {
            1: [workflow(10, 1, "CI"), workflow(11, 1, "Nightly")],
            2: [workflow(20, 2, "Deploy")],
        }
        self.files: dict[str, str | None] = {
            "acme/web:.github/workflows/nightly.yml": "on:\n  schedule:\n    - cron: '0 2 * * *'\n",
            "acme/web:.github/workflows/ci.yml": "on: push\n",
            "acme/api:.github/workflows/deploy.yml": None,
        }
        self.runs: dict[int, list[WorkflowRun]] = {
            1: [
                run(100, 1, 10),
                run(101, 1, 11, event="schedule"),
                run(102, 1, 10, status="in_progress", conclusion=None),
            ],
            2: [run(200, 2, 20)],
        }
        self.jobs: dict[int, list[WorkflowJob]] = {
            100: [job(1000, 100, 1)],
            101: [job(1010, 101, 1)],
            102: [job(1020, 102, 1, status="in_progress", conclusion=None, completed_at=None)],
            200: [job(2000, 200, 2)],
        }
        self.single_runs: dict[int, WorkflowRun] = {}
        self.fail_repo_runs: set[str] = set()
        self.raise_rate_limit = False
        self.on_list_runs: Callable[[], None] = lambda: None

    def list_org_repositories(self, org: str) -> Iterator[Repository]:
        self.calls.append(("list_org_repositories", org))
        yield from self.org_repos.get(org, [])

    def list_installation_repositories(self) -> Iterator[Repository]:
        self.calls.append(("list_installation_repositories", None))
        yield from self.install_repos

    def get_repository(self, full_name: str) -> Repository:
        self.calls.append(("get_repository", full_name))
        if full_name not in self.repos:
            raise GitHubAPIError(f"/repos/{full_name}", 404, "nope")
        return self.repos[full_name]

    def list_workflows(self, repository: Repository) -> Iterator[Workflow]:
        self.calls.append(("list_workflows", repository.full_name))
        yield from self.workflows.get(repository.id, [])

    def get_file_text(self, repository: Repository, path: str, ref: str) -> str | None:
        self.calls.append(("get_file_text", f"{repository.full_name}:{path}@{ref}"))
        return self.files.get(f"{repository.full_name}:{path}")

    def list_runs(
        self, repository: Repository, since: datetime, until: datetime | None = None
    ) -> Iterator[WorkflowRun]:
        self.calls.append(("list_runs", (repository.full_name, since)))
        self.on_list_runs()
        if self.raise_rate_limit:
            raise GitHubRateLimitError("/runs", 403, "limited")
        if repository.full_name in self.fail_repo_runs:
            raise GitHubAPIError("/runs", 500, "boom")
        yield from (r for r in self.runs.get(repository.id, []) if r.created_at >= since)

    def get_run(self, repo_full_name: str, run_id: int) -> WorkflowRun:
        self.calls.append(("get_run", run_id))
        if run_id not in self.single_runs:
            raise GitHubAPIError("/run", 404, "gone")
        return self.single_runs[run_id]

    def list_jobs(
        self, repository: Repository, run_id: int, jobs_filter: str = "all"
    ) -> Iterator[WorkflowJob]:
        self.calls.append(("list_jobs", (run_id, jobs_filter)))
        if run_id not in self.jobs:
            raise GitHubAPIError("/jobs", 404, "gone")
        yield from self.jobs[run_id]

    def names(self, kind: str) -> list[Any]:
        return [arg for k, arg in self.calls if k == kind]


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "github_token": "t",
        "database_url": "postgresql://unused",
        "orgs": "acme",
        "backfill_days": 7,
        "lookback_minutes": 60,
        "max_open_run_refresh": 10,
    }
    base.update(over)
    return load_settings(**base)


@pytest.fixture
def clock() -> list[datetime]:
    return [NOW + timedelta(hours=1)]


def make(
    store: Store, gh: FakeGitHub, clock: list[datetime], **over: Any
) -> tuple[Collector, Metrics]:
    m = Metrics(CollectorRegistry())
    c = Collector(gh, store, m, settings(**over), clock=lambda: clock[0])  # type: ignore[arg-type]
    return c, m


def _gauge(m: Metrics, name: str, **labels: str) -> float | None:
    return m.registry.get_sample_value(name, labels or None)


def test_first_cycle_backfills_everything(migrated_store: Store, clock: list[datetime]) -> None:
    gh = FakeGitHub()
    c, m = make(migrated_store, gh, clock)
    assert c.run_cycle() == "ok"

    # archived repo skipped by default, inventory + workflows + schedules read
    assert [r.full_name for r in migrated_store.list_repositories()] == ["acme/api", "acme/web"]
    assert gh.names("list_workflows") == ["acme/api", "acme/web"]
    assert sorted(gh.names("get_file_text")) == [
        "acme/api:.github/workflows/deploy.yml@main",
        "acme/web:.github/workflows/ci.yml@main",
        "acme/web:.github/workflows/nightly.yml@main",
    ]
    # backfill window starts backfill_days before "now"
    (_, since), *_ = gh.names("list_runs")
    assert since == clock[0] - timedelta(days=7)
    # every run got its jobs exactly once
    assert sorted(rid for rid, _ in gh.names("list_jobs")) == [100, 101, 102, 200]
    assert migrated_store.counts() == {
        "repositories": 2,
        "workflows": 3,
        "runs": 4,
        "open_runs": 1,
        "jobs": 4,
    }

    # gauges
    assert _gauge(m, "gha_ingester_up") == 1
    assert _gauge(m, "gha_ingester_ready") == 1
    assert _gauge(m, "gha_ingester_stored_runs") == 4
    assert _gauge(m, "gha_ingester_open_runs") == 1
    assert _gauge(m, "gha_ingester_cycles_total", result="ok") == 1
    assert _gauge(m, "gha_ingester_github_rate_limit_remaining") == 4990
    assert _gauge(m, "gha_scheduled_workflows") == 1
    labels = {
        "repository": "acme/web",
        "workflow": ".github/workflows/nightly.yml",
        "workflow_name": "Nightly",
    }
    assert _gauge(m, "gha_scheduled_workflow_interval_seconds", **labels) == 86400
    assert (
        _gauge(m, "gha_scheduled_workflow_last_run_timestamp_seconds", **labels) == NOW.timestamp()
    )
    assert _gauge(m, "gha_scheduled_workflow_last_conclusion", **labels, conclusion="success") == 1
    assert _gauge(m, "gha_scheduled_workflow_last_conclusion", **labels, conclusion="failure") == 0


def test_second_cycle_is_incremental(migrated_store: Store, clock: list[datetime]) -> None:
    gh = FakeGitHub()
    c, _ = make(migrated_store, gh, clock)
    c.run_cycle()
    gh.calls.clear()
    clock[0] += timedelta(minutes=5)
    assert c.run_cycle() == "ok"
    # inventory not re-listed (repo_refresh_seconds default 3600), schedules not re-read
    assert gh.names("list_workflows") == [] and gh.names("get_file_text") == []
    # window = previous cursor - lookback
    (_, since), *_ = gh.names("list_runs")
    assert since == clock[0] - timedelta(minutes=5) - timedelta(minutes=60)
    # nothing changed → no job fetch
    assert gh.names("list_jobs") == []


def test_changed_run_refetches_jobs_and_completion(
    migrated_store: Store, clock: list[datetime]
) -> None:
    gh = FakeGitHub()
    c, _ = make(migrated_store, gh, clock)
    c.run_cycle()
    gh.calls.clear()
    finished = run(
        102, 1, 10, status="completed", conclusion="failure", updated_at=NOW + timedelta(minutes=20)
    )
    gh.runs[1] = [finished]
    gh.jobs[102] = [
        job(1020, 102, 1, conclusion="failure", completed_at=NOW + timedelta(minutes=15))
    ]
    clock[0] += timedelta(minutes=5)
    c.run_cycle()
    assert gh.names("list_jobs") == [(102, "all")]
    assert migrated_store.counts()["open_runs"] == 0
    conn = migrated_store.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT conclusion, completed_at FROM minion_workflow_runs WHERE id = 102")
        row = cur.fetchone() or {}
    conn.rollback()
    assert row["conclusion"] == "failure"
    assert row["completed_at"] == NOW + timedelta(minutes=15)


def test_open_run_outside_window_is_refreshed_individually(
    migrated_store: Store, clock: list[datetime]
) -> None:
    gh = FakeGitHub()
    stuck = run(300, 1, 10, status="queued", conclusion=None, created_at=NOW - timedelta(days=2))
    gh.runs[1].append(stuck)
    c, _ = make(migrated_store, gh, clock)
    c.run_cycle()
    gh.calls.clear()
    # Next cycle only lists the last hour; run 300 is older but still open.
    gh.single_runs[300] = run(
        300,
        1,
        10,
        status="completed",
        conclusion="cancelled",
        created_at=stuck.created_at,
        updated_at=NOW + timedelta(hours=1),
    )
    clock[0] += timedelta(minutes=5)
    c.run_cycle()
    assert gh.names("get_run") == [300]
    assert migrated_store.counts()["open_runs"] == 1  # only 102 remains open


def test_open_run_deleted_upstream_is_ignored(migrated_store: Store, clock: list[datetime]) -> None:
    gh = FakeGitHub()
    gh.runs[1].append(
        run(300, 1, 10, status="queued", conclusion=None, created_at=NOW - timedelta(days=2))
    )
    c, _ = make(migrated_store, gh, clock)
    c.run_cycle()
    clock[0] += timedelta(minutes=5)
    assert c.run_cycle() == "ok"  # get_run 404 → skipped, not an error


def test_run_deleted_before_jobs_fetch_is_marked_synced(
    migrated_store: Store, clock: list[datetime]
) -> None:
    gh = FakeGitHub()
    del gh.jobs[200]
    c, _ = make(migrated_store, gh, clock)
    assert c.run_cycle() == "ok"
    assert migrated_store.runs_needing_jobs(2) == []


def test_repository_failure_is_partial_not_fatal(
    migrated_store: Store, clock: list[datetime]
) -> None:
    gh = FakeGitHub()
    gh.fail_repo_runs.add("acme/api")
    c, m = make(migrated_store, gh, clock)
    assert c.run_cycle() == "partial"
    assert _gauge(m, "gha_ingester_errors_total", stage="repository") == 1
    assert _gauge(m, "gha_ingester_up") == 1
    assert migrated_store.counts()["runs"] == 3  # acme/web still ingested


def test_rate_limit_aborts_cycle(migrated_store: Store, clock: list[datetime]) -> None:
    gh = FakeGitHub()
    gh.raise_rate_limit = True
    c, m = make(migrated_store, gh, clock)
    assert c.run_cycle() == "error"
    assert _gauge(m, "gha_ingester_up") == 0
    assert _gauge(m, "gha_ingester_errors_total", stage="rate_limit") == 1
    assert _gauge(m, "gha_ingester_last_success_timestamp_seconds") == 0


def test_scope_explicit_repos_exclusions_and_archived(
    migrated_store: Store, clock: list[datetime]
) -> None:
    gh = FakeGitHub()
    c, _ = make(
        migrated_store,
        gh,
        clock,
        orgs="",
        repos="acme/web,acme/old,acme/missing",
        include_archived=True,
        exclude_repos="acme/w*",
    )
    c.run_cycle()
    assert gh.names("get_repository") == ["acme/web", "acme/old", "acme/missing"]
    assert [r.full_name for r in migrated_store.list_repositories()] == ["acme/old"]


def test_app_auth_uses_installation_listing(
    migrated_store: Store, clock: list[datetime], rsa_private_key_pem: str
) -> None:
    gh = FakeGitHub()
    gh.install_repos = [WEB, API, repo(9, "other/x")]
    c, _ = make(
        migrated_store,
        gh,
        clock,
        github_token="",
        github_app_id="1",
        github_app_private_key=rsa_private_key_pem,
    )
    c.run_cycle()
    assert gh.names("list_installation_repositories") == [None]
    assert gh.names("list_org_repositories") == []
    assert [r.full_name for r in migrated_store.list_repositories()] == ["acme/api", "acme/web"]


def test_schedules_can_be_disabled(migrated_store: Store, clock: list[datetime]) -> None:
    gh = FakeGitHub()
    c, m = make(migrated_store, gh, clock, sync_schedules=False)
    c.run_cycle()
    assert gh.names("get_file_text") == []
    assert _gauge(m, "gha_scheduled_workflows") == 0


def test_inventory_refresh_after_interval(migrated_store: Store, clock: list[datetime]) -> None:
    gh = FakeGitHub()
    c, _ = make(migrated_store, gh, clock, repo_refresh_seconds=600)
    c.run_cycle()
    gh.calls.clear()
    clock[0] += timedelta(seconds=601)
    gh.org_repos["acme"].append(repo(4, "acme/new"))
    c.run_cycle()
    assert "acme/new" in gh.names("list_workflows")
    assert "acme/new" in [r.full_name for r in migrated_store.list_repositories()]


def test_run_forever_stops_on_event(migrated_store: Store, clock: list[datetime]) -> None:
    gh = FakeGitHub()
    c, _ = make(migrated_store, gh, clock)
    stop = threading.Event()
    # Ask for the stop from inside the first cycle: the loop must finish the
    # cycle it is in and then return without waiting the poll interval.
    gh.on_list_runs = stop.set
    c.run_forever(stop)
    assert c.cycles == 1
