"""Prometheus metric definitions.

Everything is registered on an explicit ``CollectorRegistry`` (plus the
standard process/platform/GC collectors) so ``/metrics`` renders exactly
what this ingester publishes.

Two families:

  ``gha_ingester_*``      — introspection: cycle timing, API budget, rows
                            written, errors. Alert on these to know the
                            ingester itself is healthy.
  ``gha_scheduled_workflow_*`` — one series per workflow with a cron
                            ``on.schedule``: when it last ran and how often
                            it should. Alert on these to catch a scheduled
                            workflow that silently stopped firing.

The analytical data (minutes, success rate, queue time) lives in
PostgreSQL, not here: Grafana reads it with the SQL datasource.
"""

from __future__ import annotations

from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
)

NAMESPACE = "gha_ingester"

WORKFLOW_LABELS = ("repository", "workflow", "workflow_name")


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.registry.register(PROCESS_COLLECTOR)
        self.registry.register(PLATFORM_COLLECTOR)
        self.registry.register(GC_COLLECTOR)

        # ---- Liveness of the ingester itself ----
        self.build_info = Info(
            f"{NAMESPACE}_build",
            "Build information of the running ingester.",
            registry=self.registry,
        )
        self.up = Gauge(
            f"{NAMESPACE}_up",
            "1 when the last ingestion cycle finished without a fatal error, else 0.",
            registry=self.registry,
        )
        self.ready = Gauge(
            f"{NAMESPACE}_ready",
            "1 once the database schema is bootstrapped and the first cycle completed.",
            registry=self.registry,
        )
        self.cycles_total = Counter(
            f"{NAMESPACE}_cycles_total",
            "Ingestion cycles, by outcome.",
            ("result",),
            registry=self.registry,
        )
        self.cycle_duration_seconds = Histogram(
            f"{NAMESPACE}_cycle_duration_seconds",
            "Wall-clock duration of an ingestion cycle.",
            registry=self.registry,
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
        )
        self.last_cycle_timestamp_seconds = Gauge(
            f"{NAMESPACE}_last_cycle_timestamp_seconds",
            "Unix time of the end of the last cycle (any outcome).",
            registry=self.registry,
        )
        self.last_success_timestamp_seconds = Gauge(
            f"{NAMESPACE}_last_success_timestamp_seconds",
            "Unix time of the end of the last SUCCESSFUL cycle.",
            registry=self.registry,
        )
        self.errors_total = Counter(
            f"{NAMESPACE}_errors_total",
            "Errors during ingestion, by stage.",
            ("stage",),
            registry=self.registry,
        )

        # ---- GitHub API budget ----
        self.github_requests_total = Counter(
            f"{NAMESPACE}_github_requests_total",
            "Requests sent to the GitHub REST API, by HTTP status.",
            ("status",),
            registry=self.registry,
        )
        self.github_rate_limit_remaining = Gauge(
            f"{NAMESPACE}_github_rate_limit_remaining",
            "Requests left in the current primary rate-limit window "
            "(X-RateLimit-Remaining of the last response).",
            registry=self.registry,
        )
        self.github_rate_limit_limit = Gauge(
            f"{NAMESPACE}_github_rate_limit_limit",
            "Size of the primary rate-limit window (X-RateLimit-Limit).",
            registry=self.registry,
        )
        self.github_rate_limit_reset_timestamp_seconds = Gauge(
            f"{NAMESPACE}_github_rate_limit_reset_timestamp_seconds",
            "Unix time when the primary rate-limit window resets.",
            registry=self.registry,
        )

        # ---- What was ingested ----
        self.repositories = Gauge(
            f"{NAMESPACE}_repositories",
            "Repositories currently in scope (after exclusions).",
            registry=self.registry,
        )
        self.workflows = Gauge(
            f"{NAMESPACE}_workflows",
            "Workflow files known across all repositories.",
            registry=self.registry,
        )
        self.runs_upserted_total = Counter(
            f"{NAMESPACE}_runs_upserted_total",
            "Workflow runs written (inserted or updated), by repository.",
            ("repository",),
            registry=self.registry,
        )
        self.jobs_upserted_total = Counter(
            f"{NAMESPACE}_jobs_upserted_total",
            "Workflow jobs written (inserted or updated), by repository.",
            ("repository",),
            registry=self.registry,
        )
        self.open_runs = Gauge(
            f"{NAMESPACE}_open_runs",
            "Runs stored that are not yet completed (queued / in progress).",
            registry=self.registry,
        )
        self.stored_runs = Gauge(
            f"{NAMESPACE}_stored_runs",
            "Total workflow runs stored in the database.",
            registry=self.registry,
        )
        self.stored_jobs = Gauge(
            f"{NAMESPACE}_stored_jobs",
            "Total workflow jobs stored in the database.",
            registry=self.registry,
        )
        self.repository_cycle_duration_seconds = Histogram(
            f"{NAMESPACE}_repository_cycle_duration_seconds",
            "Time spent ingesting one repository within a cycle.",
            registry=self.registry,
            buckets=(0.5, 1, 2, 5, 10, 30, 60, 300),
        )

        # ---- Scheduled workflow liveness ----
        self.scheduled_last_run_timestamp_seconds = Gauge(
            "gha_scheduled_workflow_last_run_timestamp_seconds",
            "Unix time of the most recent run triggered by `schedule` for the workflow.",
            WORKFLOW_LABELS,
            registry=self.registry,
        )
        self.scheduled_interval_seconds = Gauge(
            "gha_scheduled_workflow_interval_seconds",
            "Longest gap between two consecutive cron fires of the workflow (all schedules merged).",
            WORKFLOW_LABELS,
            registry=self.registry,
        )
        self.scheduled_last_conclusion = Gauge(
            "gha_scheduled_workflow_last_conclusion",
            "1 for the conclusion of the last completed scheduled run "
            "(success/failure/cancelled/...), 0 for the others.",
            (*WORKFLOW_LABELS, "conclusion"),
            registry=self.registry,
        )
        self.scheduled_workflows = Gauge(
            "gha_scheduled_workflows",
            "Active workflows that declare at least one cron schedule.",
            registry=self.registry,
        )
