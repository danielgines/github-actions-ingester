"""Read what a workflow file declares: its triggers, its crons and what it calls.

The file is fetched once (see ``Collector.sync_schedules``) and every fact
comes out of the same parse:

  * ``on.schedule[].cron`` powers the scheduled-workflow liveness metrics;
  * the event names under ``on`` say what else starts the workflow
    (``push``, ``workflow_dispatch``, ``workflow_call`` ...);
  * ``jobs.*.uses`` lists the reusable workflows it calls, and
    ``jobs.*.steps[].uses`` the actions.

YAML 1.1 parses the bare key ``on`` as the boolean ``True`` (PyYAML follows
that spec), so both spellings are looked up.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import yaml
from croniter import croniter


@dataclass(frozen=True)
class WorkflowFacts:
    """Everything the ingester keeps from a workflow file."""

    schedules: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    reusable_workflows: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


def _load(workflow_yaml: str) -> dict[Any, Any] | None:
    try:
        doc = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None


def _triggers_block(doc: dict[Any, Any]) -> Any:
    triggers = doc.get("on")
    if triggers is None:
        triggers = doc.get(True)
    return triggers


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def parse_workflow(workflow_yaml: str) -> WorkflowFacts:
    """Return the facts declared by a workflow file.

    Returns empty facts for files that do not parse (a broken workflow
    never runs anyway). Lists keep document order and are deduplicated.
    """
    doc = _load(workflow_yaml)
    if doc is None:
        return WorkflowFacts()

    triggers = _triggers_block(doc)
    crons: list[str] = []
    events: list[str] = []
    if isinstance(triggers, str):
        events = [triggers]
    elif isinstance(triggers, list):
        events = [e for e in triggers if isinstance(e, str)]
    elif isinstance(triggers, dict):
        events = [str(k) for k in triggers]
        schedule = triggers.get("schedule")
        if isinstance(schedule, list):
            for entry in schedule:
                if isinstance(entry, dict):
                    cron = entry.get("cron")
                    if isinstance(cron, str) and cron.strip():
                        crons.append(" ".join(cron.split()))

    reusable: list[str] = []
    actions: list[str] = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses")
            if isinstance(uses, str) and uses.strip():
                reusable.append(uses.strip())
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if isinstance(step, dict):
                    uses = step.get("uses")
                    if isinstance(uses, str) and uses.strip():
                        actions.append(uses.strip())

    return WorkflowFacts(
        schedules=crons,
        triggers=_dedupe(events),
        reusable_workflows=_dedupe(reusable),
        actions=_dedupe(actions),
    )


def parse_schedules(workflow_yaml: str) -> list[str]:
    """Return the cron expressions declared under ``on.schedule``."""
    return parse_workflow(workflow_yaml).schedules


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
