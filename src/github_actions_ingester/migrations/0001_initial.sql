-- 0001_initial: core tables, indexes and the compatibility views.
--
-- Every statement runs inside the schema selected by GHA_DATABASE_SCHEMA
-- (search_path is set by the migration runner), so nothing here is
-- schema-qualified.

CREATE TABLE IF NOT EXISTS repositories (
    id              BIGINT PRIMARY KEY,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    full_name       TEXT NOT NULL UNIQUE,
    default_branch  TEXT NOT NULL DEFAULT 'main',
    private         BOOLEAN NOT NULL DEFAULT FALSE,
    archived        BOOLEAN NOT NULL DEFAULT FALSE,
    html_url        TEXT NOT NULL DEFAULT '',
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflows (
    id              BIGINT PRIMARY KEY,
    repository_id   BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT '',
    html_url        TEXT NOT NULL DEFAULT '',
    schedules       TEXT[] NOT NULL DEFAULT '{}',
    -- Longest gap between two consecutive scheduled firings (all crons merged).
    schedule_interval_seconds DOUBLE PRECISION,
    schedules_synced_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workflows_repository_idx ON workflows (repository_id);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id              BIGINT PRIMARY KEY,
    repository_id   BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    workflow_id     BIGINT NOT NULL,
    run_number      INTEGER NOT NULL DEFAULT 0,
    run_attempt     INTEGER NOT NULL DEFAULT 1,
    name            TEXT NOT NULL DEFAULT '',
    display_title   TEXT NOT NULL DEFAULT '',
    event           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT '',
    conclusion      TEXT,
    head_branch     TEXT,
    head_sha        TEXT NOT NULL DEFAULT '',
    actor           TEXT NOT NULL DEFAULT '',
    triggering_actor TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ,
    run_started_at  TIMESTAMPTZ,
    -- Derived on write: MAX(jobs.completed_at) once every job finished, or
    -- updated_at when the run is completed and no job reported a time.
    completed_at    TIMESTAMPTZ,
    html_url        TEXT NOT NULL DEFAULT '',
    jobs_synced_at  TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workflow_runs_repo_created_idx
    ON workflow_runs (repository_id, created_at DESC);
CREATE INDEX IF NOT EXISTS workflow_runs_workflow_created_idx
    ON workflow_runs (workflow_id, created_at DESC);
CREATE INDEX IF NOT EXISTS workflow_runs_created_idx ON workflow_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS workflow_runs_open_idx
    ON workflow_runs (repository_id, created_at) WHERE status <> 'completed';

CREATE TABLE IF NOT EXISTS workflow_jobs (
    id              BIGINT PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
    repository_id   BIGINT NOT NULL,
    run_attempt     INTEGER NOT NULL DEFAULT 1,
    name            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT '',
    conclusion      TEXT,
    runner_name     TEXT,
    runner_group_name TEXT,
    labels          TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    steps           INTEGER NOT NULL DEFAULT 0,
    html_url        TEXT NOT NULL DEFAULT '',
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workflow_jobs_run_idx ON workflow_jobs (run_id);
CREATE INDEX IF NOT EXISTS workflow_jobs_repo_started_idx
    ON workflow_jobs (repository_id, started_at DESC);

-- One row per repository: where the incremental listing resumes from.
CREATE TABLE IF NOT EXISTS ingest_cursors (
    repository_id   BIGINT PRIMARY KEY REFERENCES repositories (id) ON DELETE CASCADE,
    runs_created_since TIMESTAMPTZ NOT NULL,
    last_cycle_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_cycle_runs INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Compatibility views: the column set expected by Grafana dashboard 24157
-- ("GitHub Actions insights"). Point its Postgres datasource at this schema
-- and every panel works unchanged.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW minion_repositories AS
SELECT id, full_name, owner, name, archived
FROM repositories;

CREATE OR REPLACE VIEW minion_workflow_files AS
SELECT id, repository_id, name, path
FROM workflows;

CREATE OR REPLACE VIEW minion_workflow_runs AS
SELECT
    r.id,
    r.repository_id,
    r.workflow_id AS workflow_file_id,
    r.event,
    r.status,
    r.conclusion,
    r.head_branch,
    r.created_at,
    COALESCE(r.run_started_at, r.created_at) AS started_at,
    r.completed_at
FROM workflow_runs r;

CREATE OR REPLACE VIEW minion_workflow_jobs AS
SELECT
    j.id,
    j.run_id,
    j.name,
    j.status,
    j.conclusion,
    j.runner_name,
    j.created_at,
    j.started_at,
    j.completed_at
FROM workflow_jobs j;
