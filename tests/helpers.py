"""Builders shared by the store / collector / dashboard tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from github_actions_ingester.github import Repository, Workflow, WorkflowJob, WorkflowRun

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def repo(id_: int = 1, full_name: str = "acme/web", archived: bool = False) -> Repository:
    owner, name = full_name.split("/")
    return Repository(
        id_, owner, name, full_name, "main", False, archived, f"https://gh/{full_name}"
    )


def workflow(id_: int = 10, repository_id: int = 1, name: str = "CI") -> Workflow:
    return Workflow(
        id_, repository_id, name, f".github/workflows/{name.lower()}.yml", "active", "", None, None
    )


def run(
    id_: int,
    repository_id: int = 1,
    workflow_id: int = 10,
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "push",
    created_at: datetime = NOW,
    updated_at: datetime | None = None,
    branch: str = "main",
) -> WorkflowRun:
    return WorkflowRun(
        id=id_,
        repository_id=repository_id,
        workflow_id=workflow_id,
        run_number=id_,
        run_attempt=1,
        name="CI",
        display_title="t",
        event=event,
        status=status,
        conclusion=conclusion,
        head_branch=branch,
        head_sha="abc",
        actor="dev",
        triggering_actor="dev",
        created_at=created_at,
        updated_at=updated_at or created_at + timedelta(minutes=5),
        run_started_at=created_at + timedelta(seconds=30),
        html_url="",
    )


def job(
    id_: int,
    run_id: int,
    repository_id: int = 1,
    name: str = "build",
    status: str = "completed",
    conclusion: str | None = "success",
    started_at: datetime | None = NOW + timedelta(seconds=40),
    completed_at: datetime | None = NOW + timedelta(minutes=4),
    runner: str | None = "ubuntu-latest",
) -> WorkflowJob:
    return WorkflowJob(
        id=id_,
        run_id=run_id,
        repository_id=repository_id,
        run_attempt=1,
        name=name,
        status=status,
        conclusion=conclusion,
        runner_name=runner,
        runner_group_name=None,
        labels=["ubuntu-latest"],
        created_at=NOW,
        started_at=started_at,
        completed_at=completed_at,
        steps=3,
        html_url="",
    )


# -- Grafana Postgres datasource simulation --------------------------------------------

INTERVALS = {"1h": 3600, "6h": 21600, "12h": 43200, "1d": 86400, "1w": 604800, "1M": 2592000}


def sql_string(values: list[str]) -> str:
    """Grafana's multi-value formatting for SQL datasources: ``'a','b'``."""
    return ",".join("'" + v.replace("'", "''") + "'" for v in values)


def expand_grafana_sql(
    raw_sql: str, variables: dict[str, str], time_from: datetime, time_to: datetime
) -> str:
    """Interpolate dashboard variables and the Postgres datasource macros.

    Mirrors what Grafana does before the query hits the database:
    ``$var``/``${var}`` substitution, ``$__timeFilter(col)`` and
    ``$__timeGroup(col, interval, fill)``.
    """
    sql = raw_sql
    for name, value in variables.items():
        sql = sql.replace("${" + name + "}", value).replace("$" + name, value)
    f, t = time_from.isoformat(), time_to.isoformat()
    sql = re.sub(
        r"\$__timeFilter\(([^)]+)\)",
        lambda m: f"{m.group(1)} BETWEEN '{f}' AND '{t}'",
        sql,
    )

    def time_group(m: re.Match[str]) -> str:
        col, interval = m.group(1).strip(), m.group(2).strip().strip("'\"")
        secs = INTERVALS[interval]
        return f"floor(extract(epoch from {col})/{secs})*{secs}"

    return re.sub(r"\$__timeGroup\(([^,]+),([^,)]+)(?:,[^)]*)?\)", time_group, sql)


def variable_query(variable: dict[str, Any]) -> str:
    q = variable.get("query")
    return str(q.get("query") if isinstance(q, dict) else q)
