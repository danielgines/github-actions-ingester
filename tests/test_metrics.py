from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from github_actions_ingester.metrics import Metrics

EXPECTED = {
    "gha_ingester_up",
    "gha_ingester_ready",
    "gha_ingester_cycles_total",
    "gha_ingester_cycle_duration_seconds",
    "gha_ingester_last_cycle_timestamp_seconds",
    "gha_ingester_last_success_timestamp_seconds",
    "gha_ingester_errors_total",
    "gha_ingester_github_requests_total",
    "gha_ingester_github_rate_limit_remaining",
    "gha_ingester_github_rate_limit_limit",
    "gha_ingester_github_rate_limit_reset_timestamp_seconds",
    "gha_ingester_repositories",
    "gha_ingester_workflows",
    "gha_ingester_stored_runs",
    "gha_ingester_stored_jobs",
    "gha_ingester_open_runs",
    "gha_ingester_runs_upserted_total",
    "gha_ingester_jobs_upserted_total",
    "gha_ingester_repository_cycle_duration_seconds",
    "gha_scheduled_workflows",
    "gha_scheduled_workflow_last_run_timestamp_seconds",
    "gha_scheduled_workflow_interval_seconds",
    "gha_scheduled_workflow_last_conclusion",
}


def test_every_metric_is_registered_with_help() -> None:
    m = Metrics(CollectorRegistry())
    m.build_info.info({"version": "x"})
    text = generate_latest(m.registry).decode()
    names = {line.split()[2] for line in text.splitlines() if line.startswith("# HELP")}
    missing = EXPECTED - names
    assert not missing, f"metrics without HELP/registration: {sorted(missing)}"
    assert "gha_ingester_build_info" in text  # Info metric renders with the _info suffix


def test_registries_are_independent() -> None:
    a, b = Metrics(CollectorRegistry()), Metrics(CollectorRegistry())
    a.up.set(1)
    assert a.registry.get_sample_value("gha_ingester_up") == 1
    assert b.registry.get_sample_value("gha_ingester_up") == 0
