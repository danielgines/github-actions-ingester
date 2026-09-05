"""Extract ``on.schedule`` cron expressions from a workflow file.

Only the trigger block is inspected; the rest of the workflow is ignored.
YAML 1.1 parses the bare key ``on`` as the boolean ``True`` (PyYAML follows
that spec), so both spellings are looked up.
"""

from __future__ import annotations

import time
from itertools import pairwise
from typing import Any

import yaml
from croniter import croniter


def parse_schedules(workflow_yaml: str) -> list[str]:
    """Return the cron expressions declared under ``on.schedule``.

    Returns an empty list for workflows without a schedule trigger and for
    files that do not parse (a broken workflow never runs anyway).
    """
    try:
        doc = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    triggers: Any = doc.get("on")
    if triggers is None:
        triggers = doc.get(True)
    if not isinstance(triggers, dict):
        return []
    schedule = triggers.get("schedule")
    if not isinstance(schedule, list):
        return []
    crons: list[str] = []
    for entry in schedule:
        if isinstance(entry, dict):
            cron = entry.get("cron")
            if isinstance(cron, str) and cron.strip():
                crons.append(" ".join(cron.split()))
    return crons


def expected_interval_seconds(
    crons: list[str],
    horizon_days: int = 800,
    max_fires: int = 20000,
    now: float | None = None,
) -> float | None:
    """Longest legitimate silence between two consecutive firings.

    All valid expressions are merged into a single timeline (a workflow
    with ``0 8 * * *`` and ``0 20 * * *`` fires twice a day) and the largest
    gap between neighbours over the horizon is returned. That is the number
    an alert must compare the last run against: for ``0 9 * * 1-5`` the
    answer is 72h (Friday to Monday), not 24h -- using the shortest gap would
    page every weekend.

    Returns None when no expression is valid or fewer than two firings fall
    inside the horizon.
    """
    base = time.time() if now is None else now
    limit = base + horizon_days * 86400
    fires: list[float] = []
    for cron in crons:
        if not croniter.is_valid(cron):
            continue
        it = croniter(cron, start_time=base)
        count = 0
        while count < max_fires:
            nxt = it.get_next(float)
            if nxt > limit:
                break
            fires.append(nxt)
            count += 1
    if len(fires) < 2:
        return None
    fires.sort()
    gaps = (b - a for a, b in pairwise(fires) if b > a)
    return max(gaps, default=None)
