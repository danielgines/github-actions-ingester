"""Ingestion cycle: GitHub → PostgreSQL, incrementally.

One cycle does, in order:

  1. **Inventory** (every ``repo_refresh_seconds``): list the repositories
     in scope, then the workflow files of each, upsert both. Optionally
     read each workflow YAML from the default branch to record its cron
     schedules.
  2. **Runs**, per repository: list runs created since
     ``cursor - lookback`` (``now - backfill_days`` on the first cycle),
     upsert. Runs whose status/updated_at changed are flagged for a jobs
     refresh by the store itself.
  3. **Jobs**: fetch the jobs of every run flagged above and upsert them;
     the run's ``completed_at`` is derived from the last job to finish.
  4. **Stale open runs**: runs still open but older than the lookback
     window (long queues, multi-hour jobs) are refreshed one by one,
     bounded by ``max_open_run_refresh``.
  5. **Gauges**: table counts and scheduled-workflow liveness.

Failures in one repository are logged and counted; the cycle carries on
with the next one. Only a rate-limit exhaustion aborts the whole cycle.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from .config import Settings
from .github import (
    GitHubAPIError,
    GitHubClient,
    GitHubRateLimitError,
    Repository,
    WorkflowRun,
)
from .metrics import Metrics
from .store import Store
from .workflow_schedule import expected_interval_seconds, parse_schedules

logger = structlog.get_logger(__name__)

CONCLUSIONS = (
    "success",
    "failure",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "neutral",
    "stale",
    "startup_failure",
)
_RUN_BATCH = 500


def _batched(items: Iterable[WorkflowRun], size: int) -> Iterable[list[WorkflowRun]]:
    batch: list[WorkflowRun] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class Collector:
    def __init__(
        self,
        client: GitHubClient,
        store: Store,
        metrics: Metrics,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._metrics = metrics
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._repos: list[Repository] = []
        self._inventory_at: datetime | None = None
        self.cycles = 0

    # -- inventory ----------------------------------------------------------

    def _discover_repositories(self) -> list[Repository]:
        s = self._settings
        found: dict[int, Repository] = {}
        orgs = {o.lower() for o in s.org_list()}
        if orgs:
            if s.uses_app():
                for repo in self._client.list_installation_repositories():
                    if repo.owner.lower() in orgs:
                        found[repo.id] = repo
            else:
                for org in s.org_list():
                    for repo in self._client.list_org_repositories(org):
                        found[repo.id] = repo
        for full_name in s.repo_list():
            if any(r.full_name.lower() == full_name.lower() for r in found.values()):
                continue
            try:
                repo = self._client.get_repository(full_name)
            except GitHubAPIError as exc:
                logger.warning("inventory.repo_unreachable", repo=full_name, error=str(exc))
                self._metrics.errors_total.labels(stage="inventory").inc()
                continue
            found[repo.id] = repo
        repos = []
        for repo in found.values():
            if s.is_excluded(repo.full_name):
                continue
            if repo.archived and not s.include_archived:
                continue
            repos.append(repo)
        repos.sort(key=lambda r: r.full_name.lower())
        return repos

    def refresh_inventory(self) -> None:
        repos = self._discover_repositories()
        self._store.upsert_repositories(repos)
        workflows_total = 0
        for repo in repos:
            try:
                workflows = list(self._client.list_workflows(repo))
            except GitHubAPIError as exc:
                logger.warning("inventory.workflows_failed", repo=repo.full_name, error=str(exc))
                self._metrics.errors_total.labels(stage="inventory").inc()
                continue
            workflows_total += self._store.upsert_workflows(workflows)
        self._repos = repos
        self._inventory_at = self._clock()
        self._metrics.repositories.set(len(repos))
        self._metrics.workflows.set(workflows_total)
        logger.info("inventory.refreshed", repositories=len(repos), workflows=workflows_total)
        if self._settings.sync_schedules:
            self.sync_schedules()

    def sync_schedules(self) -> None:
        older_than = self._clock() - timedelta(seconds=self._settings.schedule_refresh_seconds)
        rows = self._store.workflows_needing_schedule_sync(older_than)
        synced = 0
        for row in rows:
            repo = Repository(
                id=int(row["repository_id"]),
                owner="",
                name="",
                full_name=str(row["full_name"]),
                default_branch=str(row["default_branch"]),
                private=False,
                archived=False,
                html_url="",
            )
            path = str(row["path"])
            # Dynamic workflows (e.g. "dynamic/pages/pages-build-deployment")
            # have no file in the tree; record an empty schedule.
            crons: list[str] = []
            if path.startswith(".github/workflows/"):
                try:
                    text = self._client.get_file_text(repo, path, repo.default_branch)
                except GitHubAPIError as exc:
                    logger.warning(
                        "schedules.read_failed", repo=repo.full_name, path=path, error=str(exc)
                    )
                    self._metrics.errors_total.labels(stage="schedules").inc()
                    continue
                crons = parse_schedules(text) if text else []
            interval = expected_interval_seconds(crons) if crons else None
            self._store.set_workflow_schedules(int(row["id"]), crons, interval)
            synced += 1
        if rows:
            logger.info("schedules.synced", workflows=synced, of=len(rows))

    def _inventory_stale(self) -> bool:
        if self._inventory_at is None:
            return True
        age = (self._clock() - self._inventory_at).total_seconds()
        return age >= self._settings.repo_refresh_seconds

    # -- runs / jobs -------------------------------------------------------------

    def ingest_repository(self, repo: Repository) -> int:
        s = self._settings
        now = self._clock()
        cursor = self._store.get_cursor(repo.id)
        if cursor is None:
            since = now - timedelta(days=s.backfill_days)
            logger.info("runs.backfill", repo=repo.full_name, since=since.isoformat())
        else:
            since = cursor - timedelta(minutes=s.lookback_minutes)

        written = 0
        for batch in _batched(self._client.list_runs(repo, since, now), _RUN_BATCH):
            written += self._store.upsert_runs(batch)
        if written:
            self._metrics.runs_upserted_total.labels(repository=repo.full_name).inc(written)

        # Open runs that fell out of the window: refresh them individually.
        stale = self._store.open_runs_before(repo.id, since, s.max_open_run_refresh)
        for run_id in stale:
            try:
                run = self._client.get_run(repo.full_name, run_id)
            except GitHubAPIError as exc:
                if exc.status == 404:
                    continue
                raise
            self._store.upsert_runs([run])

        jobs_written = 0
        for run_id in self._store.runs_needing_jobs(repo.id):
            try:
                jobs = list(self._client.list_jobs(repo, run_id, s.jobs_filter))
            except GitHubAPIError as exc:
                if exc.status == 404:
                    # Run deleted between listing and jobs fetch; mark as synced.
                    self._store.upsert_jobs(run_id, [])
                    continue
                raise
            jobs_written += self._store.upsert_jobs(run_id, jobs)
        if jobs_written:
            self._metrics.jobs_upserted_total.labels(repository=repo.full_name).inc(jobs_written)

        self._store.set_cursor(repo.id, now, written)
        logger.info(
            "runs.ingested",
            repo=repo.full_name,
            runs=written,
            jobs=jobs_written,
            refreshed_open=len(stale),
            since=since.isoformat(),
        )
        return written

    # -- gauges ------------------------------------------------------------------

    def update_gauges(self) -> None:
        m = self._metrics
        counts = self._store.counts()
        m.stored_runs.set(counts.get("runs", 0))
        m.stored_jobs.set(counts.get("jobs", 0))
        m.open_runs.set(counts.get("open_runs", 0))
        m.repositories.set(counts.get("repositories", 0))
        m.workflows.set(counts.get("workflows", 0))

        rows = self._store.scheduled_workflow_status()
        m.scheduled_last_run_timestamp_seconds.clear()
        m.scheduled_interval_seconds.clear()
        m.scheduled_last_conclusion.clear()
        m.scheduled_workflows.set(len(rows))
        for row in rows:
            labels: dict[str, Any] = {
                "repository": row["repository"],
                "workflow": row["path"],
                "workflow_name": row["name"],
            }
            last = row.get("last_scheduled_run_at")
            m.scheduled_last_run_timestamp_seconds.labels(**labels).set(
                last.timestamp() if isinstance(last, datetime) else 0
            )
            interval = row.get("interval_seconds")
            m.scheduled_interval_seconds.labels(**labels).set(
                float(interval) if interval is not None else 0
            )
            last_conclusion = row.get("last_conclusion")
            for c in CONCLUSIONS:
                m.scheduled_last_conclusion.labels(**labels, conclusion=c).set(
                    1 if c == last_conclusion else 0
                )

    def _update_rate_limit_gauges(self) -> None:
        rl = self._client.rate_limit
        self._metrics.github_rate_limit_remaining.set(rl.remaining)
        self._metrics.github_rate_limit_limit.set(rl.limit)
        self._metrics.github_rate_limit_reset_timestamp_seconds.set(rl.reset_at)

    # -- cycle ---------------------------------------------------------------------

    def run_cycle(self) -> str:
        """Run one full cycle; returns ``ok``, ``partial`` or ``error``."""
        m = self._metrics
        started = time.monotonic()
        result = "ok"
        try:
            if self._inventory_stale():
                self.refresh_inventory()
            failures = 0
            for repo in self._repos:
                t0 = time.monotonic()
                try:
                    self.ingest_repository(repo)
                except GitHubRateLimitError:
                    raise
                except Exception as exc:
                    failures += 1
                    m.errors_total.labels(stage="repository").inc()
                    logger.error("runs.repository_failed", repo=repo.full_name, error=str(exc))
                finally:
                    m.repository_cycle_duration_seconds.observe(time.monotonic() - t0)
                self._update_rate_limit_gauges()
            self.update_gauges()
            if failures:
                result = "partial"
        except GitHubRateLimitError as exc:
            logger.error("cycle.rate_limited", error=str(exc))
            m.errors_total.labels(stage="rate_limit").inc()
            result = "error"
        except Exception as exc:
            logger.exception("cycle.failed", error=str(exc))
            m.errors_total.labels(stage="cycle").inc()
            result = "error"
        finally:
            self._update_rate_limit_gauges()
        elapsed = time.monotonic() - started
        now_ts = time.time()
        m.cycle_duration_seconds.observe(elapsed)
        m.cycles_total.labels(result=result).inc()
        m.last_cycle_timestamp_seconds.set(now_ts)
        if result != "error":
            m.last_success_timestamp_seconds.set(now_ts)
            m.up.set(1)
            m.ready.set(1)
        else:
            m.up.set(0)
        self.cycles += 1
        logger.info("cycle.done", result=result, seconds=round(elapsed, 2), cycle=self.cycles)
        return result

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_cycle()
            stop.wait(self._settings.poll_interval_seconds)
