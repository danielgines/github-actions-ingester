from __future__ import annotations

import calendar

import pytest

from github_actions_ingester.workflow_schedule import expected_interval_seconds, parse_schedules

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
