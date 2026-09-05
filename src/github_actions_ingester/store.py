"""PostgreSQL persistence: schema bootstrap, migrations and upserts.

Bootstrap contract (what the operator asked for):

  * first start  → connects, creates the schema and applies every
                   migration under ``migrations/`` in order;
  * upgrade      → applies only the migrations not yet recorded;
  * steady state → does nothing.

Migrations are plain SQL files named ``NNNN_description.sql``. The set
applied so far is recorded in ``<schema>.schema_migrations``. A
transaction-level advisory lock serializes concurrent starters (two
replicas, a ``migrate`` job racing the deployment) so only one of them
runs the DDL; the others wait and find everything already applied.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import psycopg
import structlog
from psycopg import sql
from psycopg.rows import dict_row

from .github import Repository, Workflow, WorkflowJob, WorkflowRun

logger = structlog.get_logger(__name__)

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# Arbitrary but stable: hashtext('github-actions-ingester') style constant.
_ADVISORY_LOCK_KEY = 0x6768612D696E67  # "gha-ing"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    body: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def load_migrations() -> list[Migration]:
    """Read the embedded SQL files, sorted by version."""
    out: list[Migration] = []
    pkg = resources.files("github_actions_ingester") / "migrations"
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if not m:
            continue
        out.append(Migration(int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    out.sort(key=lambda mig: mig.version)
    return out


@dataclass
class MigrationReport:
    applied: list[str]
    current_version: int
    pending_before: int


class Store:
    def __init__(self, database_url: str, schema: str = "gha", connect_timeout: int = 10) -> None:
        self._url = database_url
        self._schema = schema
        self._connect_timeout = connect_timeout
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # -- connection ------------------------------------------------------

    @property
    def schema(self) -> str:
        return self._schema

    def connect(self) -> psycopg.Connection[dict[str, Any]]:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        conn = psycopg.connect(
            self._url,
            connect_timeout=self._connect_timeout,
            row_factory=dict_row,
            autocommit=False,
            application_name="github-actions-ingester",
        )
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self._schema))
            )
        conn.commit()
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def ping(self) -> bool:
        try:
            with self.connect().cursor() as cur:
                cur.execute("SELECT 1")
            self.connect().rollback()
            return True
        except psycopg.Error as exc:
            logger.warning("store.ping_failed", error=str(exc))
            self.close()
            return False

    # -- migrations ----------------------------------------------------------

    def migrate(self, migrations: Sequence[Migration] | None = None) -> MigrationReport:
        migrations = list(migrations if migrations is not None else load_migrations())
        conn = self.connect()
        applied_now: list[str] = []
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            cur.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self._schema))
            )
            cur.execute(
                sql.SQL(
                    """
                        CREATE TABLE IF NOT EXISTS {}.schema_migrations (
                            version     INTEGER PRIMARY KEY,
                            name        TEXT NOT NULL,
                            checksum    TEXT NOT NULL,
                            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                        """
                ).format(sql.Identifier(self._schema))
            )
            cur.execute(
                sql.SQL("SELECT version, checksum FROM {}.schema_migrations").format(
                    sql.Identifier(self._schema)
                )
            )
            done = {int(r["version"]): str(r["checksum"]) for r in cur.fetchall()}
            pending = [m for m in migrations if m.version not in done]
            for m in migrations:
                if m.version in done and done[m.version] != m.checksum:
                    logger.warning(
                        "store.migration_checksum_drift",
                        version=m.version,
                        name=m.name,
                        hint="an applied migration file changed; it is NOT re-run",
                    )
            for m in pending:
                logger.info("store.migration_apply", version=m.version, name=m.name)
                cur.execute(m.body)
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {}.schema_migrations (version, name, checksum) "
                        "VALUES (%s, %s, %s)"
                    ).format(sql.Identifier(self._schema)),
                    (m.version, m.name, m.checksum),
                )
                applied_now.append(m.name)
            current = max([m.version for m in migrations] + list(done), default=0)
        if applied_now:
            logger.info("store.migrations_applied", count=len(applied_now), version=current)
        else:
            logger.info("store.migrations_up_to_date", version=current)
        return MigrationReport(applied_now, current, len(pending))

    def grant_read_access(self, roles: Sequence[str]) -> list[str]:
        """Give ``roles`` read-only access to the schema.

        USAGE on the schema, SELECT on every table and view in it, and a
        default privilege so tables created by later migrations are
        readable too. Idempotent; run after every ``migrate``. The roles
        must exist already: creating roles is the operator's job, and a
        missing one raises like any other bootstrap error.
        """
        granted: list[str] = []
        if not roles:
            return granted
        conn = self.connect()
        schema = sql.Identifier(self._schema)
        with conn.transaction(), conn.cursor() as cur:
            for role in roles:
                ident = sql.Identifier(role)
                cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, ident))
                cur.execute(
                    sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(schema, ident)
                )
                cur.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
                    ).format(schema, ident)
                )
                granted.append(role)
        logger.info("store.read_access_granted", roles=granted, schema=self._schema)
        self._set_database_search_path()
        return granted

    def _set_database_search_path(self) -> None:
        """Make the schema resolve by bare name for every session on the database.

        The ingester sets ``search_path`` on its own connections, and a role
        named like the schema gets it for free (``"$user"`` is first in the
        default path), which is why the Compose example works without this.
        A read role with a different name (``grafana`` reading schema
        ``gha``) resolves ``minion_workflow_runs`` to nothing, and the
        Grafana PostgreSQL datasource has no search_path setting.

        ``ALTER DATABASE ... SET`` needs the database owner. When the ingester
        is not the owner this logs and moves on: the grants above still hold,
        and the operator can run the statement once by hand.
        """
        conn = self.connect()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT current_database() AS db")
                row = cur.fetchone()
                database = row["db"] if row else ""
                cur.execute(
                    sql.SQL("ALTER DATABASE {} SET search_path TO {}, public").format(
                        sql.Identifier(database), sql.Identifier(self._schema)
                    )
                )
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            logger.warning(
                "store.search_path_not_set",
                schema=self._schema,
                hint="ingester role does not own the database; run "
                "ALTER DATABASE <db> SET search_path TO <schema>, public as the owner",
            )
            return
        logger.info("store.search_path_set", schema=self._schema)

    def schema_version(self) -> int | None:
        """Highest applied migration, or None when the schema was never bootstrapped."""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) AS t", (f"{self._schema}.schema_migrations",))
                row = cur.fetchone()
                if row is None or row["t"] is None:
                    return None
                cur.execute(
                    sql.SQL(
                        "SELECT COALESCE(MAX(version), 0) AS v FROM {}.schema_migrations"
                    ).format(sql.Identifier(self._schema))
                )
                row = cur.fetchone()
                return int(row["v"]) if row else 0
        finally:
            conn.rollback()

    # -- repositories / workflows ------------------------------------------------

    def upsert_repositories(self, repos: Iterable[Repository]) -> int:
        rows = [
            (
                r.id,
                r.owner,
                r.name,
                r.full_name,
                r.default_branch,
                r.private,
                r.archived,
                r.html_url,
            )
            for r in repos
        ]
        if not rows:
            return 0
        conn = self.connect()
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO repositories
                    (id, owner, name, full_name, default_branch, private, archived, html_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    owner = EXCLUDED.owner,
                    name = EXCLUDED.name,
                    full_name = EXCLUDED.full_name,
                    default_branch = EXCLUDED.default_branch,
                    private = EXCLUDED.private,
                    archived = EXCLUDED.archived,
                    html_url = EXCLUDED.html_url,
                    last_seen_at = now()
                """,
                rows,
            )
        return len(rows)

    def upsert_workflows(self, workflows: Iterable[Workflow]) -> int:
        rows = [
            (
                w.id,
                w.repository_id,
                w.name,
                w.path,
                w.state,
                w.html_url,
                w.created_at,
                w.updated_at,
            )
            for w in workflows
        ]
        if not rows:
            return 0
        conn = self.connect()
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO workflows
                    (id, repository_id, name, path, state, html_url, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    repository_id = EXCLUDED.repository_id,
                    name = EXCLUDED.name,
                    path = EXCLUDED.path,
                    state = EXCLUDED.state,
                    html_url = EXCLUDED.html_url,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    last_seen_at = now()
                """,
                rows,
            )
        return len(rows)

    def set_workflow_schedules(
        self, workflow_id: int, schedules: list[str], interval_seconds: float | None
    ) -> None:
        conn = self.connect()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflows
                SET schedules = %s, schedule_interval_seconds = %s, schedules_synced_at = now()
                WHERE id = %s
                """,
                (schedules, interval_seconds, workflow_id),
            )

    def workflows_needing_schedule_sync(self, older_than: datetime) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT w.id, w.path, w.repository_id, r.full_name, r.default_branch
                    FROM workflows w
                    JOIN repositories r ON r.id = w.repository_id
                    WHERE w.schedules_synced_at IS NULL OR w.schedules_synced_at < %s
                    ORDER BY w.schedules_synced_at NULLS FIRST, w.id
                    """,
                    (older_than,),
                )
                return list(cur.fetchall())
        finally:
            conn.rollback()

    def list_repositories(self) -> list[Repository]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, owner, name, full_name, default_branch, private, archived, html_url "
                    "FROM repositories ORDER BY full_name"
                )
                return [
                    Repository(
                        id=int(r["id"]),
                        owner=str(r["owner"]),
                        name=str(r["name"]),
                        full_name=str(r["full_name"]),
                        default_branch=str(r["default_branch"]),
                        private=bool(r["private"]),
                        archived=bool(r["archived"]),
                        html_url=str(r["html_url"]),
                    )
                    for r in cur.fetchall()
                ]
        finally:
            conn.rollback()

    # -- runs / jobs -----------------------------------------------------------------

    def upsert_runs(self, runs: Iterable[WorkflowRun]) -> int:
        rows = [
            (
                r.id,
                r.repository_id,
                r.workflow_id,
                r.run_number,
                r.run_attempt,
                r.name,
                r.display_title,
                r.event,
                r.status,
                r.conclusion,
                r.head_branch,
                r.head_sha,
                r.actor,
                r.triggering_actor,
                r.created_at,
                r.updated_at,
                r.run_started_at,
                r.updated_at if r.status == "completed" else None,
                r.html_url,
            )
            for r in runs
        ]
        if not rows:
            return 0
        conn = self.connect()
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO workflow_runs
                    (id, repository_id, workflow_id, run_number, run_attempt, name,
                     display_title, event, status, conclusion, head_branch, head_sha, actor,
                     triggering_actor, created_at, updated_at, run_started_at, completed_at,
                     html_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    repository_id = EXCLUDED.repository_id,
                    workflow_id = EXCLUDED.workflow_id,
                    run_number = EXCLUDED.run_number,
                    run_attempt = EXCLUDED.run_attempt,
                    name = EXCLUDED.name,
                    display_title = EXCLUDED.display_title,
                    event = EXCLUDED.event,
                    status = EXCLUDED.status,
                    conclusion = EXCLUDED.conclusion,
                    head_branch = EXCLUDED.head_branch,
                    head_sha = EXCLUDED.head_sha,
                    actor = EXCLUDED.actor,
                    triggering_actor = EXCLUDED.triggering_actor,
                    updated_at = EXCLUDED.updated_at,
                    run_started_at = EXCLUDED.run_started_at,
                    -- keep the job-derived completion when we already have one
                    completed_at = COALESCE(workflow_runs.completed_at, EXCLUDED.completed_at),
                    html_url = EXCLUDED.html_url,
                    -- a run that changed since the last jobs sync needs its jobs again
                    jobs_synced_at = CASE
                        WHEN workflow_runs.updated_at IS DISTINCT FROM EXCLUDED.updated_at
                          OR workflow_runs.status IS DISTINCT FROM EXCLUDED.status
                        THEN NULL ELSE workflow_runs.jobs_synced_at END
                """,
                rows,
            )
        return len(rows)

    def upsert_jobs(self, run_id: int, jobs: Sequence[WorkflowJob]) -> int:
        rows = [
            (
                j.id,
                j.run_id,
                j.repository_id,
                j.run_attempt,
                j.name,
                j.status,
                j.conclusion,
                j.runner_name,
                j.runner_group_name,
                j.labels,
                j.created_at,
                j.started_at,
                j.completed_at,
                j.steps,
                j.html_url,
            )
            for j in jobs
        ]
        conn = self.connect()
        with conn.transaction(), conn.cursor() as cur:
            if rows:
                cur.executemany(
                    """
                    INSERT INTO workflow_jobs
                        (id, run_id, repository_id, run_attempt, name, status, conclusion,
                         runner_name, runner_group_name, labels, created_at, started_at,
                         completed_at, steps, html_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        run_attempt = EXCLUDED.run_attempt,
                        name = EXCLUDED.name,
                        status = EXCLUDED.status,
                        conclusion = EXCLUDED.conclusion,
                        runner_name = EXCLUDED.runner_name,
                        runner_group_name = EXCLUDED.runner_group_name,
                        labels = EXCLUDED.labels,
                        created_at = EXCLUDED.created_at,
                        started_at = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at,
                        steps = EXCLUDED.steps,
                        html_url = EXCLUDED.html_url,
                        ingested_at = now()
                    """,
                    rows,
                )
            # Completion time of the run = last job to finish, when every job finished.
            cur.execute(
                """
                UPDATE workflow_runs r
                SET jobs_synced_at = now(),
                    completed_at = CASE
                        WHEN r.status = 'completed' THEN COALESCE(
                            (SELECT MAX(j.completed_at) FROM workflow_jobs j
                             WHERE j.run_id = r.id
                               AND NOT EXISTS (SELECT 1 FROM workflow_jobs k
                                               WHERE k.run_id = r.id AND k.completed_at IS NULL)),
                            r.completed_at, r.updated_at)
                        ELSE NULL END
                WHERE r.id = %s
                """,
                (run_id,),
            )
        return len(rows)

    def runs_needing_jobs(self, repository_id: int, limit: int = 5000) -> list[int]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM workflow_runs
                    WHERE repository_id = %s AND jobs_synced_at IS NULL
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (repository_id, limit),
                )
                return [int(r["id"]) for r in cur.fetchall()]
        finally:
            conn.rollback()

    def open_runs_before(self, repository_id: int, before: datetime, limit: int) -> list[int]:
        """Runs still not completed that fall outside the lookback window."""
        if limit <= 0:
            return []
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM workflow_runs
                    WHERE repository_id = %s AND status <> 'completed' AND created_at < %s
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (repository_id, before, limit),
                )
                return [int(r["id"]) for r in cur.fetchall()]
        finally:
            conn.rollback()

    # -- cursors -------------------------------------------------------------------

    def get_cursor(self, repository_id: int) -> datetime | None:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT runs_created_since FROM ingest_cursors WHERE repository_id = %s",
                    (repository_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                value = row["runs_created_since"]
                return value if isinstance(value, datetime) else None
        finally:
            conn.rollback()

    def set_cursor(self, repository_id: int, since: datetime, runs: int) -> None:
        conn = self.connect()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_cursors (repository_id, runs_created_since, last_cycle_at,
                                            last_cycle_runs)
                VALUES (%s, %s, now(), %s)
                ON CONFLICT (repository_id) DO UPDATE SET
                    runs_created_since = EXCLUDED.runs_created_since,
                    last_cycle_at = now(),
                    last_cycle_runs = EXCLUDED.last_cycle_runs
                """,
                (repository_id, since, runs),
            )

    # -- read models for metrics ---------------------------------------------------------

    def scheduled_workflow_status(self) -> list[dict[str, Any]]:
        """One row per workflow with a cron schedule: last scheduled run + interval."""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.full_name AS repository, w.path, w.name,
                           w.schedule_interval_seconds AS interval_seconds,
                           (SELECT MAX(run.created_at) FROM workflow_runs run
                             WHERE run.workflow_id = w.id AND run.event = 'schedule')
                               AS last_scheduled_run_at,
                           (SELECT run.conclusion FROM workflow_runs run
                             WHERE run.workflow_id = w.id AND run.event = 'schedule'
                               AND run.status = 'completed'
                             ORDER BY run.created_at DESC LIMIT 1) AS last_conclusion
                    FROM workflows w
                    JOIN repositories r ON r.id = w.repository_id
                    WHERE cardinality(w.schedules) > 0 AND w.state = 'active'
                    ORDER BY r.full_name, w.path
                    """
                )
                return list(cur.fetchall())
        finally:
            conn.rollback()

    def counts(self) -> dict[str, int]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT (SELECT count(*) FROM repositories) AS repositories,
                           (SELECT count(*) FROM workflows) AS workflows,
                           (SELECT count(*) FROM workflow_runs) AS runs,
                           (SELECT count(*) FROM workflow_runs WHERE status <> 'completed')
                               AS open_runs,
                           (SELECT count(*) FROM workflow_jobs) AS jobs
                    """
                )
                row = cur.fetchone() or {}
                return {k: int(v) for k, v in row.items()}
        finally:
            conn.rollback()


def utcnow() -> datetime:
    return datetime.now(UTC)
