from __future__ import annotations

import calendar

import pytest

from github_actions_ingester.workflow_schedule import (
    WorkflowFacts,
    expected_interval_seconds,
    parse_schedules,
    parse_workflow,
)

H = 3600.0
# Monday 2026-01-05 00:00:00 UTC
MONDAY = float(calendar.timegm((2026, 1, 5, 0, 0, 0)))


def test_parse_plain_on_block() -> None:
    text = """
name: nightly
on:
  schedule:
    - cron: "0 2 * * *"
    - cron:   '30  14 * * 1-5'
  workflow_dispatch:
jobs: {}
"""
    assert parse_schedules(text) == ["0 2 * * *", "30 14 * * 1-5"]


def test_parse_true_key_yaml_1_1() -> None:
    # PyYAML turns the bare key `on` into boolean True; both spellings work.
    assert parse_schedules("true:\n  schedule:\n    - cron: '*/5 * * * *'\n") == ["*/5 * * * *"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "on: push",
        "on:\n  push:\n",
        "on:\n  schedule: not-a-list\n",
        "on:\n  schedule:\n    - nope\n    - cron: ''\n",
        "- just\n- a list\n",
        "on: [\n",  # broken YAML
    ],
)
def test_parse_without_schedule(text: str) -> None:
    assert parse_schedules(text) == []


def test_interval_daily() -> None:
    assert expected_interval_seconds(["0 2 * * *"], now=MONDAY) == 24 * H


def test_interval_weekdays_is_weekend_gap() -> None:
    # Friday 09:00 -> Monday 09:00 is the longest legitimate silence.
    assert expected_interval_seconds(["0 9 * * 1-5"], now=MONDAY) == 72 * H


def test_interval_merges_several_crons() -> None:
    # 08:00 and 20:00: the workflow is never silent for more than 12h.
    assert expected_interval_seconds(["0 8 * * *", "0 20 * * *"], now=MONDAY) == 12 * H


def test_interval_monthly_uses_longest_month() -> None:
    assert expected_interval_seconds(["0 0 1 * *"], now=MONDAY) == 31 * 24 * H


def test_interval_yearly_needs_two_fires_in_horizon() -> None:
    assert expected_interval_seconds(["0 0 1 1 *"], now=MONDAY) == 365 * 24 * H
    assert expected_interval_seconds(["0 0 1 1 *"], now=MONDAY, horizon_days=300) is None


def test_interval_ignores_invalid_crons() -> None:
    assert expected_interval_seconds(["nonsense", "0 * * * *"], now=MONDAY) == H
    assert expected_interval_seconds(["nonsense"], now=MONDAY) is None
    assert expected_interval_seconds([], now=MONDAY) is None


def test_parse_workflow_collects_triggers_and_calls() -> None:
    text = """
name: nightly
on:
  workflow_dispatch: {}
  schedule:
    - cron: "0 2 * * *"
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make
      - uses: actions/checkout@v4
      - name: upload
        uses: actions/upload-artifact@v4
  notify:
    needs: build
    uses: ./.github/workflows/_notify.yml
    secrets: inherit
  deploy:
    uses: acme/infra/.github/workflows/deploy.yml@v3
"""
    facts = parse_workflow(text)
    assert facts.schedules == ["0 2 * * *"]
    assert facts.triggers == ["workflow_dispatch", "schedule"]
    assert facts.reusable_workflows == [
        "./.github/workflows/_notify.yml",
        "acme/infra/.github/workflows/deploy.yml@v3",
    ]
    # deduplicated, document order kept
    assert facts.actions == ["actions/checkout@v4", "actions/upload-artifact@v4"]


@pytest.mark.parametrize(
    ("text", "triggers"),
    [
        ("on: push\njobs: {}\n", ["push"]),
        ("on: [push, pull_request]\n", ["push", "pull_request"]),
        ("on:\n  workflow_call:\n    inputs: {}\n", ["workflow_call"]),
        ("true:\n  push:\n", ["push"]),
    ],
)
def test_parse_workflow_trigger_shapes(text: str, triggers: list[str]) -> None:
    facts = parse_workflow(text)
    assert facts.triggers == triggers
    assert facts.schedules == []


def test_parse_workflow_ignores_malformed_jobs() -> None:
    text = "on: push\njobs:\n  a: not-a-map\n  b:\n    uses: 42\n    steps: nope\n  c:\n    steps:\n      - uses: ''\n"
    assert parse_workflow(text) == WorkflowFacts(triggers=["push"])


def test_parse_workflow_broken_yaml_is_empty() -> None:
    assert parse_workflow("on: [\n") == WorkflowFacts()
