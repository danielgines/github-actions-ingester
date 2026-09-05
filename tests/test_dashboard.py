"""Every query of ``examples/grafana-dashboard.json`` against the views.

Grafana's Postgres datasource expands variables and macros in the browser
and the backend; the test reproduces that expansion (``tests/helpers.py``)
and executes each variable query and each panel query under the same
scenarios a user exercises: All repositories, every aggregation interval,
both display toggles and the repeated per-repository row.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from github_actions_ingester.store import Store
from tests.helpers import (
    INTERVALS,
    NOW,
    expand_grafana_sql,
    job,
    repo,
    run,
    sql_string,
    variable_query,
    workflow,
)

pytestmark = pytest.mark.integration

DASHBOARD = Path(__file__).parent.parent / "examples" / "grafana-dashboard.json"
TIME_FROM, TIME_TO = NOW - timedelta(days=30), NOW + timedelta(hours=1)

REPOS = ["acme/web", "acme/api", "acme/o'reilly-docs"]  # quote must survive formatting
EVENTS = ["push", "pull_request", "schedule", "workflow_dispatch"]
CONCLUSIONS = ["success", "success", "success", "failure", "cancelled", "skipped"]


@pytest.fixture(scope="module")
def dashboard() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def seeded(migrated_store: Store) -> Store:
    rng = random.Random(24157)
    s = migrated_store
    repos = [repo(i + 1, name) for i, name in enumerate(REPOS)]
    s.upsert_repositories(repos)
    wfs = []
    for r in repos:
        wfs += [workflow(r.id * 10 + k, r.id, f"WF{k}") for k in range(2)]
    s.upsert_workflows(wfs)
    run_id = 0
    for wf in wfs:
        for _ in range(40):
            run_id += 1
            created = NOW - timedelta(minutes=rng.randint(0, 30 * 24 * 60))
            concl = rng.choice(CONCLUSIONS)
            status = "completed" if rng.random() > 0.05 else "in_progress"
            s.upsert_runs(
                [
                    run(
                        run_id,
                        repository_id=wf.repository_id,
                        workflow_id=wf.id,
                        status=status,
                        conclusion=concl if status == "completed" else None,
                        event=rng.choice(EVENTS),
                        created_at=created,
                        updated_at=created + timedelta(minutes=rng.randint(1, 60)),
                    )
                ]
            )
            jobs = []
            for j in range(rng.randint(1, 3)):
                started = created + timedelta(seconds=rng.randint(5, 600))
                done = started + timedelta(seconds=rng.randint(10, 3600))
                jobs.append(
                    job(
                        run_id * 10 + j,
                        run_id,
                        repository_id=wf.repository_id,
                        name=f"job-{j}",
                        conclusion=concl,
                        started_at=started,
                        completed_at=None if status != "completed" else done,
                        status=status,
                    )
                )
            s.upsert_jobs(run_id, jobs)
    return s


def _query(store: Store, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d.name for d in cur.description or []]
            return cols, [tuple(r.values()) for r in cur.fetchall()]
    finally:
        conn.rollback()


def _variables(dashboard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in dashboard["templating"]["list"]}


def _resolve(store: Store, dashboard: dict[str, Any], base: dict[str, str]) -> dict[str, str]:
    """Run the query variables in dependency order (repository, then workflow)."""
    tv = _variables(dashboard)
    values = dict(base)
    if "repository" not in values:
        _, rows = _query(
            store, expand_grafana_sql(variable_query(tv["repository"]), values, TIME_FROM, TIME_TO)
        )
        values["repository"] = sql_string([str(r[0]) for r in rows])
    _, rows = _query(
        store, expand_grafana_sql(variable_query(tv["workflow"]), values, TIME_FROM, TIME_TO)
    )
    assert rows, "workflow variable returned nothing"
    assert all(len(r) == 2 for r in rows), "workflow variable must return (__text, __value)"
    values["workflow"] = sql_string([str(r[1]) for r in rows])
    return values


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in dashboard["panels"] if p.get("targets")]


def test_dashboard_file_is_provisionable(dashboard: dict[str, Any]) -> None:
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "${DS_" not in text, "exported datasource inputs break file provisioning"
    assert dashboard["__inputs"] == []
    assert dashboard["id"] is None
    assert dashboard["uid"] == "github-actions-insights"
    assert {v["name"] for v in dashboard["templating"]["list"]} == {
        "datasource",
        "showOrgName",
        "showFullFilePath",
        "repository",
        "workflow",
        "aggregation",
    }
    assert len(_panels(dashboard)) == 10
    assert set(INTERVALS) == set(_variables(dashboard)["aggregation"]["query"].split(","))


def test_repository_variable_lists_every_repository(
    seeded: Store, dashboard: dict[str, Any]
) -> None:
    tv = _variables(dashboard)
    _, rows = _query(
        seeded, expand_grafana_sql(variable_query(tv["repository"]), {}, TIME_FROM, TIME_TO)
    )
    assert sorted(str(r[0]) for r in rows) == sorted(REPOS)


@pytest.mark.parametrize("aggregation", sorted(INTERVALS))
def test_all_repositories_every_aggregation(
    seeded: Store, dashboard: dict[str, Any], aggregation: str
) -> None:
    values = _resolve(
        seeded,
        dashboard,
        {"showOrgName": "no", "showFullFilePath": "no", "aggregation": aggregation},
    )
    _assert_every_panel_returns_rows(seeded, dashboard, values)


@pytest.mark.parametrize(("org", "path"), [("yes", "no"), ("no", "yes"), ("yes", "yes")])
def test_display_toggles(seeded: Store, dashboard: dict[str, Any], org: str, path: str) -> None:
    values = _resolve(
        seeded, dashboard, {"showOrgName": org, "showFullFilePath": path, "aggregation": "1d"}
    )
    _assert_every_panel_returns_rows(seeded, dashboard, values)


@pytest.mark.parametrize("repository", REPOS)
def test_repeated_row_single_repository(
    seeded: Store, dashboard: dict[str, Any], repository: str
) -> None:
    values = _resolve(
        seeded,
        dashboard,
        {
            "showOrgName": "no",
            "showFullFilePath": "no",
            "aggregation": "1d",
            "repository": sql_string([repository]),
        },
    )
    _assert_every_panel_returns_rows(seeded, dashboard, values)


def test_time_series_panels_have_time_first(seeded: Store, dashboard: dict[str, Any]) -> None:
    values = _resolve(
        seeded, dashboard, {"showOrgName": "no", "showFullFilePath": "no", "aggregation": "1d"}
    )
    for panel in _panels(dashboard):
        for target in panel["targets"]:
            if target.get("format") != "time_series":
                continue
            cols, rows = _query(
                seeded, expand_grafana_sql(target["rawSql"], values, TIME_FROM, TIME_TO)
            )
            assert cols[0] == "time", (
                f"panel {panel['id']} {panel['title']!r}: first column must be time"
            )
            assert rows and rows[0][0] is not None


def _assert_every_panel_returns_rows(
    store: Store, dashboard: dict[str, Any], values: dict[str, str]
) -> None:
    empty = []
    for panel in _panels(dashboard):
        for target in panel["targets"]:
            sql = expand_grafana_sql(target["rawSql"], values, TIME_FROM, TIME_TO)
            assert "$" not in sql, f"panel {panel['id']}: unexpanded token in {sql!r}"
            _, rows = _query(store, sql)
            if not rows:
                empty.append(f"{panel['id']} {panel['title']!r}")
    assert not empty, f"panels without data: {empty}"
