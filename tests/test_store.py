from __future__ import annotations

import re
from datetime import timedelta

import psycopg
import pytest

from github_actions_ingester.store import Migration, Store, load_migrations
from tests.helpers import NOW, job, repo, run, workflow

pytestmark = pytest.mark.integration


def test_migrations_load_in_order() -> None:
    ms = load_migrations()
    assert [m.version for m in ms] == sorted(m.version for m in ms)
    assert ms[0].version == 1 and ms[0].name == "0001_initial.sql"
    assert len(ms[0].checksum) == 64


def test_migrate_bootstraps_then_is_a_noop(store: Store) -> None:
    assert store.schema_version() is None
    first = store.migrate()
    assert first.applied == ["0001_initial.sql"]
    assert first.current_version == 1
    second = store.migrate()
    assert second.applied == []
    assert store.schema_version() == 1
    assert store.ping()


def test_migrate_applies_only_pending_and_keeps_order(store: Store) -> None:
    ms = load_migrations()
    store.migrate(ms)
    extra = Migration(2, "0002_extra.sql", "CREATE TABLE extra (id INT)")
    report = store.migrate([*ms, extra])
    assert report.applied == ["0002_extra.sql"]
    assert store.schema_version() == 2
    assert store.migrate([*ms, extra]).applied == []


def test_migrate_detects_checksum_drift_without_rerunning(store: Store) -> None:
    ms = load_migrations()
    store.migrate(ms)
    drifted = Migration(ms[0].version, ms[0].name, ms[0].body + "\n-- edited")
    assert drifted.checksum != ms[0].checksum
    assert store.migrate([drifted]).applied == []


def test_schema_is_isolated_per_store(database_url: str, migrated_store: Store) -> None:
    other = Store(database_url, migrated_store.schema + "_b")
    try:
        assert other.schema_version() is None
    finally:
        other.close()


def test_repositories_and_workflows_roundtrip(migrated_store: Store) -> None:
    s = migrated_store
    assert s.upsert_repositories([repo(1), repo(2, "acme/api", archived=True)]) == 2
    listed = s.list_repositories()
    assert [r.full_name for r in listed] == ["acme/api", "acme/web"]
    assert listed[0].archived is True
    assert s.upsert_workflows([workflow(10, 1), workflow(11, 2, "Deploy")]) == 2
    # re-upsert keeps ids stable, no duplicates
    s.upsert_repositories([repo(1)])
    assert s.counts() == {"repositories": 2, "workflows": 2, "runs": 0, "open_runs": 0, "jobs": 0}


def test_schedule_sync_bookkeeping(migrated_store: Store) -> None:
    s = migrated_store
    s.upsert_repositories([repo(1)])
    s.upsert_workflows([workflow(10, 1), workflow(11, 1, "Nightly")])
    pending = s.workflows_needing_schedule_sync(NOW)
    assert {int(r["id"]) for r in pending} == {10, 11}
    assert pending[0]["full_name"] == "acme/web" and pending[0]["default_branch"] == "main"
    s.set_workflow_schedules(11, ["0 2 * * *"], 86400.0)
    s.set_workflow_schedules(10, [], None)
    assert s.workflows_needing_schedule_sync(NOW - timedelta(days=1)) == []
    status = s.scheduled_workflow_status()
    assert len(status) == 1
    assert status[0]["name"] == "Nightly" and status[0]["interval_seconds"] == 86400.0
    assert status[0]["last_scheduled_run_at"] is None


def test_runs_jobs_and_completion_derivation(migrated_store: Store) -> None:
    s = migrated_store
    s.upsert_repositories([repo(1)])
    s.upsert_workflows([workflow(10, 1)])
    open_run = run(100, status="in_progress", conclusion=None)
    s.upsert_runs([open_run])
    assert s.runs_needing_jobs(1) == [100]
    assert s.counts()["open_runs"] == 1

    s.upsert_jobs(100, [job(1000, 100, completed_at=None, status="in_progress", conclusion=None)])
    assert s.runs_needing_jobs(1) == []  # synced
    row = _run_row(s, 100)
    assert row["completed_at"] is None  # still open

    # The run completes: a changed updated_at/status invalidates the job sync.
    done = run(100, status="completed", conclusion="failure", updated_at=NOW + timedelta(minutes=6))
    s.upsert_runs([done])
    assert s.runs_needing_jobs(1) == [100]
    row = _run_row(s, 100)
    assert row["completed_at"] == NOW + timedelta(minutes=6)  # updated_at until jobs arrive

    s.upsert_jobs(
        100, [job(1000, 100, conclusion="failure", completed_at=NOW + timedelta(minutes=4))]
    )
    row = _run_row(s, 100)
    assert row["completed_at"] == NOW + timedelta(minutes=4)  # MAX(jobs.completed_at) wins
    assert row["conclusion"] == "failure"

    # Re-upserting an unchanged run keeps the job-derived completion and the sync mark.
    s.upsert_runs([done])
    assert s.runs_needing_jobs(1) == []
    assert _run_row(s, 100)["completed_at"] == NOW + timedelta(minutes=4)


def test_completed_run_with_unfinished_job_falls_back_to_updated_at(migrated_store: Store) -> None:
    s = migrated_store
    s.upsert_repositories([repo(1)])
    s.upsert_runs([run(1, updated_at=NOW + timedelta(minutes=9))])
    s.upsert_jobs(
        1, [job(10, 1, completed_at=NOW + timedelta(minutes=2)), job(11, 1, completed_at=None)]
    )
    assert _run_row(s, 1)["completed_at"] == NOW + timedelta(minutes=9)


def test_upsert_jobs_with_no_jobs_marks_run_synced(migrated_store: Store) -> None:
    s = migrated_store
    s.upsert_repositories([repo(1)])
    s.upsert_runs([run(1)])
    assert s.upsert_jobs(1, []) == 0
    assert s.runs_needing_jobs(1) == []


def test_open_runs_before_and_cursor(migrated_store: Store) -> None:
    s = migrated_store
    s.upsert_repositories([repo(1)])
    s.upsert_runs(
        [
            run(1, status="queued", conclusion=None, created_at=NOW - timedelta(days=2)),
            run(2, status="in_progress", conclusion=None, created_at=NOW - timedelta(hours=1)),
            run(3, created_at=NOW - timedelta(days=3)),  # completed: never refreshed
        ]
    )
    assert s.open_runs_before(1, NOW - timedelta(hours=2), limit=10) == [1]
    assert s.open_runs_before(1, NOW, limit=10) == [2, 1]
    assert s.open_runs_before(1, NOW, limit=0) == []
    assert s.get_cursor(1) is None
    s.set_cursor(1, NOW, 3)
    assert s.get_cursor(1) == NOW
    s.set_cursor(1, NOW + timedelta(minutes=5), 0)
    assert s.get_cursor(1) == NOW + timedelta(minutes=5)


def test_views_expose_dashboard_contract(migrated_store: Store) -> None:
    s = migrated_store
    s.upsert_repositories([repo(1)])
    s.upsert_workflows([workflow(10, 1)])
    s.upsert_runs([run(1, event="schedule")])
    s.upsert_jobs(1, [job(5, 1)])
    conn = s.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM minion_repositories")
            assert set(cur.fetchone() or {}) == {"id", "full_name", "owner", "name", "archived"}
            cur.execute("SELECT * FROM minion_workflow_files")
            assert set(cur.fetchone() or {}) == {"id", "repository_id", "name", "path"}
            cur.execute("SELECT * FROM minion_workflow_runs")
            r = cur.fetchone() or {}
            assert set(r) == {
                "id",
                "repository_id",
                "workflow_file_id",
                "event",
                "status",
                "conclusion",
                "head_branch",
                "created_at",
                "started_at",
                "completed_at",
            }
            assert r["workflow_file_id"] == 10 and r["started_at"] == NOW + timedelta(seconds=30)
            cur.execute("SELECT * FROM minion_workflow_jobs")
            assert set(cur.fetchone() or {}) == {
                "id",
                "run_id",
                "name",
                "status",
                "conclusion",
                "runner_name",
                "created_at",
                "started_at",
                "completed_at",
            }
    finally:
        conn.rollback()


def test_scheduled_status_reports_last_scheduled_run(migrated_store: Store) -> None:
    s = migrated_store
    s.upsert_repositories([repo(1)])
    s.upsert_workflows([workflow(10, 1)])
    s.set_workflow_schedules(10, ["0 * * * *"], 3600.0)
    s.upsert_runs(
        [
            run(1, event="schedule", created_at=NOW - timedelta(hours=3), conclusion="failure"),
            run(
                2,
                event="schedule",
                created_at=NOW - timedelta(hours=1),
                status="in_progress",
                conclusion=None,
            ),
            run(3, event="push", created_at=NOW),  # not scheduled: ignored
        ]
    )
    (row,) = s.scheduled_workflow_status()
    assert row["last_scheduled_run_at"] == NOW - timedelta(hours=1)
    assert row["last_conclusion"] == "failure"  # last *completed* scheduled run


def _run_row(store: Store, run_id: int) -> dict[str, object]:
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM workflow_runs WHERE id = %s", (run_id,))
            return dict(cur.fetchone() or {})
    finally:
        conn.rollback()


def test_grant_read_access_lets_a_role_read_views_and_future_tables(
    database_url: str, migrated_store: Store
) -> None:
    role = "r_" + migrated_store.schema
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'reader'")
    try:
        assert migrated_store.grant_read_access([role]) == [role]
        # Idempotent: a second start must not fail on grants already given.
        assert migrated_store.grant_read_access([role]) == [role]
        # A table created by the ingester AFTER the grant is covered too
        # (default privileges), which is what a later migration looks like.
        with migrated_store.connect().transaction(), migrated_store.connect().cursor() as cur:
            cur.execute("CREATE TABLE later_migration (id INT)")
        schema = migrated_store.schema
        reader_url = re.sub(r"//[^@]*@", f"//{role}:reader@", _with_userinfo(database_url))
        with psycopg.connect(reader_url, autocommit=True) as reader:
            # No SET search_path on purpose: Grafana cannot send one, so the
            # database-level default the ingester sets must resolve the views.
            path = reader.execute("SHOW search_path").fetchone()[0]
            assert path.split(",")[0].strip().strip('"') == schema
            assert reader.execute("SELECT count(*) FROM minion_workflow_runs").fetchone()[0] == 0
            assert reader.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0] == 0
            assert reader.execute("SELECT count(*) FROM later_migration").fetchone()[0] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute(
                    "INSERT INTO repositories (id, owner, name, full_name) VALUES (1,'a','b','a/b')"
                )
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f"DROP OWNED BY {role}")
            admin.execute(f"DROP ROLE {role}")
            db = admin.execute("SELECT current_database()").fetchone()[0]
            admin.execute(f'ALTER DATABASE "{db}" RESET search_path')


def test_grant_read_access_requires_an_existing_role(migrated_store: Store) -> None:
    with pytest.raises(psycopg.errors.UndefinedObject):
        migrated_store.grant_read_access(["no_such_role_" + migrated_store.schema])
    assert migrated_store.grant_read_access([]) == []


def _with_userinfo(url: str) -> str:
    """pgserver URIs carry no user:pass; give the substitution something to replace."""
    return url if "@" in url else url.replace("://", "://x:y@", 1)
