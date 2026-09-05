# github-actions-ingester

[![PyPI](https://img.shields.io/pypi/v/github-actions-ingester)](https://pypi.org/project/github-actions-ingester/)
[![Container](https://img.shields.io/badge/ghcr.io-github--actions--ingester-blue)](https://github.com/danielgines/github-actions-ingester/pkgs/container/github-actions-ingester)
[![Artifact Hub](https://img.shields.io/endpoint?url=https://artifacthub.io/badge/repository/github-actions-ingester)](https://artifacthub.io/packages/search?repo=github-actions-ingester)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Standalone ingester for **GitHub Actions**: a long-running service that pulls
repositories, workflows, workflow runs and jobs from the GitHub REST API into
**PostgreSQL**, and exposes **Prometheus metrics** about the ingestion itself
and about the liveness of your scheduled workflows.

  - **PostgreSQL** holds the history. Query it from Grafana with the bundled
    dashboard (a drop-in for the community dashboard
    [24157 "GitHub Actions insights"](https://grafana.com/grafana/dashboards/24157)),
    from SQL for FinOps (minutes per repository, per runner, per event),
    or from anything else that speaks SQL.

  - **Prometheus** (`/metrics`) answers the questions that need an alert
    rather than a chart: is the ingester alive and current, is the GitHub
    rate limit about to run out, and *did the nightly job actually run
    last night* (per workflow, using the `on.schedule` cron read from the
    workflow file).

## Why

GitHub keeps 90 days of run history (less on some plans), the UI shows one
repository at a time, and the API caps a listing at 1 000 runs. Getting
"how long does CI take across the organization, and how much of it is
retries" out of that means paging the API on a schedule and keeping the
rows yourself. Existing solutions are either SaaS, tied to a specific
GitOps or dashboard product, or need a webhook endpoint reachable from
GitHub.

`github-actions-ingester` is the boring alternative: one container, one
PostgreSQL schema, one read-only GitHub App. No webhooks, no inbound
traffic, no state outside the database. Point Grafana's PostgreSQL
datasource at the schema and the dashboard works.

| Use case | Interface |
|---|---|
| "Show duration, success rate and queue time per workflow over the last quarter" | PostgreSQL + `examples/grafana-dashboard.json` |
| "Minutes consumed per repository / runner label this month" | PostgreSQL, one `GROUP BY` on `workflow_jobs` |
| "Page me when the nightly backup workflow did not run" | Prometheus rule on `gha_scheduled_workflow_last_run_timestamp_seconds` |
| "Page me when the ingester stopped or the API budget is nearly gone" | Prometheus rules on `gha_ingester_*` |

## How it works

The ingester runs a **cycle** every `GHA_POLL_INTERVAL_SECONDS` (default 5
minutes):

1. **Inventory** (refreshed every `GHA_REPO_REFRESH_SECONDS`): list the
   repositories in scope (organizations and/or explicit `owner/name`,
   minus exclusion globs and archived repositories) and the workflow
   files of each one. When `GHA_SYNC_SCHEDULES=true` the workflow file is
   read from the default branch and its `on.schedule` cron expressions
   are stored, together with the *expected interval*: the longest gap
   between two consecutive fires with all crons merged (for
   `0 9 * * 1-5` that is 72 h, Friday to Monday, so the alert stays quiet
   over the weekend).
2. **Runs**: for each repository, list the runs created since the
   repository's cursor minus `GHA_LOOKBACK_MINUTES`. The first cycle of a
   repository goes back `GHA_BACKFILL_DAYS`. Windows that would exceed
   GitHub's 1 000-run cap are split in half recursively, so nothing is
   lost on busy repositories.
3. **Jobs**: for every run that is new or changed since the last cycle,
   fetch its jobs (`filter=all` to match GitHub billing, or `latest`).
   Runs still open outside the lookback window (long queues, multi-hour
   jobs) are refreshed individually, bounded by `GHA_MAX_OPEN_RUN_REFRESH`.
4. **Persist**: upserts inside one transaction per repository, then move
   the cursor. `completed_at` of a run is derived from its jobs
   (`MAX(completed_at)`) or from `updated_at` once the run is completed.
5. **Publish**: gauges for the inventory, counters for what was written,
   the rate-limit headers of the last response, and one series per
   scheduled workflow with its last scheduled run and last conclusion.

Pacing is client-side (`GHA_API_RATE_LIMIT_RPS`, default 5) plus the
server's own signals: `403`/`429` with rate-limit markers honour
`Retry-After` / `X-RateLimit-Reset`, `5xx` and transport errors back off
exponentially, and when the primary budget drops under
`GHA_API_MIN_REMAINING` the cycle waits for the reset instead of spending
the last requests other tools may need.

### Schema lifecycle

On every start the ingester connects, creates the schema named by
`GHA_DATABASE_SCHEMA` if missing, and applies the migrations embedded in
the package that have not been applied yet (tracked in
`schema_migrations`, with a checksum). A fresh database is bootstrapped, an
upgrade applies only what is new, and a start on a current schema does
nothing. `github-actions-ingester migrate` does the same and exits, for
init containers or pipelines that want the schema in place before the
service starts.

The ingester owns the schema and writes to it; dashboards should read
through a role of their own. `GHA_DATABASE_READ_ROLES` names existing
roles (comma-separated) that get USAGE on the schema and SELECT on every
table and view after each migration run, plus a default privilege so
tables added by later migrations are readable too. Point Grafana at the
database with one of those roles and it never needs the ingester's
credentials. The roles themselves are created by whoever manages the
database (the ingester never runs `CREATE ROLE`).

### Tables and views

| Object | Content |
|---|---|
| `repositories` | id, owner, name, default branch, archived, first/last seen |
| `workflows` | one row per workflow file: name, path, state, `schedules[]`, `schedule_interval_seconds` |
| `workflow_runs` | one row per run: event, status, conclusion, branch, actor, `created_at`, `run_started_at`, `completed_at`, attempt |
| `workflow_jobs` | one row per job: runner name / group / labels, `started_at`, `completed_at`, steps |
| `ingest_cursors` | per-repository resume point |
| `schema_migrations` | applied migrations, with checksum |
| `minion_repositories`, `minion_workflow_files`, `minion_workflow_runs`, `minion_workflow_jobs` | views with the column contract of dashboard 24157 |

Everything lives in the configured schema; the role needs `CREATE` on the
database for the first start and plain read/write afterwards. Give
Grafana a separate read-only role.

## Quick start

### Docker

```bash
docker run --rm -p 9619:9619 \
  -e GHA_GITHUB_TOKEN=$YOUR_TOKEN \
  -e GHA_ORGS=my-org \
  -e GHA_DATABASE_URL=postgresql://gha:gha@db.example:5432/gha \
  ghcr.io/danielgines/github-actions-ingester:latest

curl http://localhost:9619/metrics
```

### Docker Compose (PostgreSQL + ingester + Grafana with the dashboard)

```bash
cd examples
GHA_GITHUB_TOKEN=$YOUR_TOKEN GHA_ORGS=my-org docker compose up -d
# Grafana on :3000 (admin/admin) → "GitHub Actions insights"
```

### pip

```bash
pip install github-actions-ingester
GHA_GITHUB_TOKEN=$YOUR_TOKEN GHA_ORGS=my-org \
  GHA_DATABASE_URL=postgresql://gha:gha@localhost:5432/gha \
  github-actions-ingester run
```

### Helm (Kubernetes)

```bash
helm install github-actions-ingester \
  oci://ghcr.io/danielgines/charts/github-actions-ingester \
  --namespace github-actions-ingester --create-namespace \
  --set auth.token=$YOUR_TOKEN \
  --set database.url=postgresql://gha:gha@postgres.db.svc:5432/gha \
  --set config.orgs=my-org \
  --set serviceMonitor.enabled=true \
  --set prometheusRule.enabled=true
```

For production, keep credentials out of values: reference existing
Secrets (fed by External Secrets Operator, CloudNativePG, sealed-secrets,
whatever you already run):

```bash
helm install github-actions-ingester \
  oci://ghcr.io/danielgines/charts/github-actions-ingester \
  --set auth.existingSecret=github-actions-ingester-github \
  --set database.existingSecret=gha-db-app \
  --set database.existingSecretKey=uri \
  --set config.orgs=my-org
```

Full chart reference in [`helm/github-actions-ingester/README.md`](helm/github-actions-ingester/README.md).

## GitHub credential

Two shapes are accepted; set exactly one.

**GitHub App (recommended).** Least privilege, no human account in the
loop, tokens rotate hourly on their own. Create the App inside your own
organization in one click from the manifest page in
[`examples/github-app/`](examples/github-app/README.md) (GitHub generates
the private key and hands it only to you), install it on the repositories
you want, and set:

```bash
GHA_GITHUB_APP_ID=123456
GHA_GITHUB_APP_PRIVATE_KEY_FILE=/secrets/github-app.pem   # or GHA_GITHUB_APP_PRIVATE_KEY inline
GHA_ORGS=my-org
```

Permissions: **Actions: read**, **Metadata: read**, and **Contents: read**
(only for `on.schedule` discovery; set `GHA_SYNC_SCHEDULES=false` to drop
it). The installation is discovered automatically.

**Personal access token.** Fine for a first look. A fine-grained token
needs *Actions: read* and *Metadata: read* (plus *Contents: read* for
schedules) on the repositories in scope; a classic token needs `repo`
for private repositories.

`github-actions-ingester check` validates the credential, lists what it
can see and prints the remaining rate limit before you commit to a
backfill.

## Configuration

All settings are env vars prefixed `GHA_` (a `.env` in the working
directory is read too; see [`.env.example`](.env.example)).

| Variable | Default | Description |
|---|---|---|
| `GHA_GITHUB_TOKEN` | `""` | Personal access token. Mutually exclusive with the App settings |
| `GHA_GITHUB_APP_ID` | `""` | GitHub App ID (or client ID) |
| `GHA_GITHUB_APP_PRIVATE_KEY` | `""` | PEM private key of the App; literal newlines or `\n` escapes |
| `GHA_GITHUB_APP_PRIVATE_KEY_FILE` | `""` | Path to the PEM file (wins over the inline value) |
| `GHA_GITHUB_APP_INSTALLATION_ID` | `""` | Installation to use; auto-discovered when empty |
| `GHA_GITHUB_API_BASE` | `https://api.github.com` | `https://ghe.example.com/api/v3` for GitHub Enterprise Server |
| `GHA_ORGS` | `""` | Comma-separated organizations (or user accounts) to ingest |
| `GHA_REPOS` | `""` | Comma-separated explicit `owner/name`, union with `GHA_ORGS` |
| `GHA_EXCLUDE_REPOS` | `""` | Comma-separated globs on `owner/name`, case-insensitive |
| `GHA_INCLUDE_ARCHIVED` | `false` | Also ingest archived repositories |
| `GHA_DATABASE_URL` | _required_ | libpq URL, e.g. `postgresql://user:pass@host:5432/db?sslmode=require` |
| `GHA_DATABASE_SCHEMA` | `gha` | Schema holding every table; created on first start |
| `GHA_DATABASE_CONNECT_TIMEOUT_SECONDS` | `10` | Connection timeout |
| `GHA_DATABASE_READ_ROLES` | `""` | Comma-separated existing roles granted read-only access to the schema on every start |
| `GHA_POLL_INTERVAL_SECONDS` | `300` | Seconds between cycles (min 30) |
| `GHA_BACKFILL_DAYS` | `30` | How far back the first cycle of a repository goes (1..3660) |
| `GHA_LOOKBACK_MINUTES` | `180` | Window before the cursor re-listed every cycle to catch status changes |
| `GHA_REPO_REFRESH_SECONDS` | `3600` | Repository / workflow inventory refresh (min 60) |
| `GHA_MAX_OPEN_RUN_REFRESH` | `200` | Per-cycle cap on `GET /runs/{id}` for stale open runs; 0 disables |
| `GHA_JOBS_FILTER` | `all` | `all` = jobs from every attempt (matches billing); `latest` = last attempt only |
| `GHA_SYNC_SCHEDULES` | `true` | Read workflow files for `on.schedule` (needs Contents: read) |
| `GHA_SCHEDULE_REFRESH_SECONDS` | `21600` | How often workflow files are re-read (min 300) |
| `GHA_API_RATE_LIMIT_RPS` | `5.0` | Client-side pacing toward the GitHub API |
| `GHA_API_MIN_REMAINING` | `200` | Pause until reset when the primary budget drops under this |
| `GHA_API_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `GHA_API_MAX_RETRIES` | `4` | Retries on 5xx / transport errors / secondary limits |
| `GHA_LISTEN_HOST` | `0.0.0.0` | HTTP bind address |
| `GHA_LISTEN_PORT` | `9619` | HTTP bind port |
| `GHA_LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error` |
| `GHA_LOG_FORMAT` | `json` | `json` (production) / `console` (dev) |

### API budget

A cycle costs roughly **1 request per repository** (the runs listing;
more only when the window holds more than 100 runs) **plus 1 per run
that changed** since the previous cycle (its jobs). The inventory adds
one request per organization page and one per repository (workflows),
once an hour by default, and schedule discovery one request per
workflow file every six hours. With 50 repositories and 500 runs a day,
that is well under 10 % of the 5 000 req/h an App installation gets.
The backfill is the expensive part: 30 days of a busy repository can be
a few thousand requests, spread across cycles by the pacing.

## CLI

```
github-actions-ingester run [--once]   # the service (default). --once: one cycle and exit
github-actions-ingester migrate        # bootstrap / upgrade the schema and exit
github-actions-ingester check          # validate config, credential, scope and database
github-actions-ingester app-manifest   # print the GitHub App manifest (--org, --redirect-url)
github-actions-ingester app-convert CODE --key-file github-app.pem
                                       # exchange a manifest code for App ID + private key
```

Exit codes: `0` ok, `1` runtime failure (`check` found a problem, `run
--once` could not bootstrap), `2` invalid configuration.

## Endpoints

| Path | Purpose |
|---|---|
| `/metrics` | Prometheus text format |
| `/healthz` | liveness: the HTTP server is up (`200 ok`) |
| `/readyz` | readiness: `503` until the schema is bootstrapped and the first cycle finished, then `200 ready` |
| `/` | index |

## Metrics

Ingester health (`gha_ingester_*`):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `gha_ingester_build_info` | info | `version` | build information |
| `gha_ingester_up` | gauge | | 1 when the last cycle finished without a fatal error |
| `gha_ingester_ready` | gauge | | 1 once the schema is bootstrapped and the first cycle completed |
| `gha_ingester_cycles_total` | counter | `result` (`ok`/`partial`/`error`) | cycles by outcome |
| `gha_ingester_cycle_duration_seconds` | histogram | | wall-clock duration of a cycle |
| `gha_ingester_last_cycle_timestamp_seconds` | gauge | | end of the last cycle, any outcome |
| `gha_ingester_last_success_timestamp_seconds` | gauge | | end of the last successful cycle |
| `gha_ingester_errors_total` | counter | `stage` | errors by stage |
| `gha_ingester_github_requests_total` | counter | `status` | requests to the GitHub API by HTTP status |
| `gha_ingester_github_rate_limit_remaining` | gauge | | `X-RateLimit-Remaining` of the last response |
| `gha_ingester_github_rate_limit_limit` | gauge | | `X-RateLimit-Limit` |
| `gha_ingester_github_rate_limit_reset_timestamp_seconds` | gauge | | when the primary window resets |
| `gha_ingester_repositories` | gauge | | repositories in scope after exclusions |
| `gha_ingester_workflows` | gauge | | workflow files known |
| `gha_ingester_stored_runs` / `_stored_jobs` | gauge | | rows in the database |
| `gha_ingester_open_runs` | gauge | | stored runs not yet completed |
| `gha_ingester_runs_upserted_total` / `_jobs_upserted_total` | counter | `repository` | rows written |
| `gha_ingester_repository_cycle_duration_seconds` | histogram | | time per repository within a cycle |

Scheduled-workflow liveness (`gha_scheduled_*`, labels `repository`,
`workflow`, `workflow_name`):

| Metric | Description |
|---|---|
| `gha_scheduled_workflows` | active workflows declaring at least one cron |
| `gha_scheduled_workflow_interval_seconds` | expected interval: longest gap between consecutive fires, all crons merged |
| `gha_scheduled_workflow_last_run_timestamp_seconds` | most recent run triggered by `schedule` (0 = never observed) |
| `gha_scheduled_workflow_last_conclusion{conclusion}` | 1 for the conclusion of the last completed scheduled run |

## Grafana dashboard

[`examples/grafana-dashboard.json`](examples/grafana-dashboard.json) is
dashboard 24157 "GitHub Actions insights" made provisionable (no
`__inputs`, fixed uid `github-actions-insights`, datasource picked by the
`datasource` variable). It queries the `minion_*` views, so it works on
this ingester's schema unchanged: success rate, duration percentiles,
queue time, runs by event and conclusion, per-repository rows, and job
level detail, with an `aggregation` interval variable from 1h to 1M.

Provisioning files for both the PostgreSQL datasource and the dashboard
are in [`examples/grafana-provisioning/`](examples/grafana-provisioning/);
the Compose stack above mounts them. In Kubernetes, ship the JSON in a
ConfigMap with the label your Grafana sidecar watches.

Every panel is exercised in the test suite against a seeded database
(all repositories × every aggregation, single-repository rows, display
toggles), so a schema change that would break the dashboard fails CI.

## Sample alert rules

The chart renders these when `prometheusRule.enabled=true`; the raw
manifest is in [`examples/kubernetes/prometheusrule.yaml`](examples/kubernetes/prometheusrule.yaml).

```yaml
- alert: GitHubActionsIngesterDown
  expr: gha_ingester_up == 0
  for: 15m
- alert: GitHubActionsIngesterStale
  expr: time() - gha_ingester_last_success_timestamp_seconds > 1800
  for: 5m
- alert: GitHubActionsIngesterRateLimitLow
  expr: gha_ingester_github_rate_limit_remaining < 300
  for: 10m
- alert: GitHubScheduledWorkflowStopped
  expr: |
    (time() - gha_scheduled_workflow_last_run_timestamp_seconds)
      > 2 * gha_scheduled_workflow_interval_seconds + 1800
    and gha_scheduled_workflow_last_run_timestamp_seconds > 0
  for: 10m
- alert: GitHubScheduledWorkflowNeverRan
  expr: gha_scheduled_workflow_last_run_timestamp_seconds == 0 and gha_scheduled_workflow_interval_seconds > 0
  for: 6h
- alert: GitHubScheduledWorkflowFailing
  expr: gha_scheduled_workflow_last_conclusion{conclusion="failure"} == 1
  for: 30m
```

## Limitations

- **Single writer.** One replica per schema. Two ingesters on the same
  schema would double the API spend and fight over the cursors; the chart
  pins `replicaCount: 1` and uses `Recreate`.
- **No webhooks, so latency is the poll interval.** A run appears at most
  `GHA_POLL_INTERVAL_SECONDS` after it is created and its final status at
  most one interval after it finishes.
- **History starts at the backfill.** GitHub only serves what it still
  keeps (90 days on most plans); the ingester cannot recover older runs.
  Raising `GHA_BACKFILL_DAYS` later only affects repositories without a
  cursor.
- **Job-level billing is approximated.** Minutes are computed from
  `started_at`/`completed_at`; GitHub rounds per job and applies OS
  multipliers. Good enough to rank repositories, not to reconcile an
  invoice.
- **Schedule discovery reads the default branch only.** A cron that exists
  on another branch is not tracked.
- **Deleted runs stay.** GitHub does not report deletions; a run removed
  from GitHub keeps its last known state in the database.
- **Requests are sequential.** The client issues one request at a time
  and the jobs endpoint is called once per run, so the first cycle is
  bound by round-trip latency rather than by `GHA_API_RATE_LIMIT_RPS`
  (about 2 requests/s in practice, or roughly 8 minutes per 1000 runs).
  Readiness turns green only after that first cycle, so `helm install
  --wait` can time out on a large backfill while the pod is perfectly
  fine; watch the logs for `cycle.done` instead, or raise `--timeout`.

## Development

```bash
git clone https://github.com/danielgines/github-actions-ingester.git
cd github-actions-ingester

just install          # dev deps + pre-commit hooks
just ci               # ruff + mypy + pytest + helm lint + helm template
just test             # pytest only (integration tests start an embedded PostgreSQL)
just run-once         # one cycle against the .env configuration
just compose-up       # local stack: PostgreSQL + ingester + Grafana
just                  # all recipes
```

Tests need no external database on Python 3.11/3.12: `pgserver` starts a
throwaway PostgreSQL in a temporary directory. On 3.13 (no `pgserver`
wheel yet) or to use an existing server, set `GHA_TEST_DATABASE_URL`;
integration tests are skipped when neither is available. GitHub traffic is mocked with `pytest-httpx`; the
CLI tests run `migrate`, `check`, `run --once` and the service mode
end-to-end against the mock API.

Pre-commit runs ruff, mypy, yamllint, hadolint, actionlint, gitleaks and
helm validation on every commit, and the full pytest suite on every push.
The same checks run in CI as `just ci`.

## Releasing

`pyproject.toml` is the canonical version source. `__init__.py` and
`Chart.yaml` (`version` + `appVersion`) are synced from it by
`scripts/sync-version.py`, enforced as a pre-commit hook.

1. Add a `## [X.Y.Z]` section to `CHANGELOG.md`
2. `just release X.Y.Z` (clean tree on `main`, bumps, syncs, commits
   `chore(release): vX.Y.Z`)
3. `git push origin main`
4. CI succeeds → `release.yaml` builds the linux/amd64 + linux/arm64 image
   with SBOM and provenance, pushes
   `ghcr.io/<owner>/github-actions-ingester:X.Y.Z` (+ semver + `latest`),
   Trivy-scans it (fails on unfixed CRITICAL), signs it with cosign
   keyless, pushes the chart to
   `oci://ghcr.io/<owner>/charts/github-actions-ingester`, publishes the
   sdist and wheel to [PyPI](https://pypi.org/project/github-actions-ingester/),
   tags `vX.Y.Z` and creates the GitHub Release.

No PAT and no PyPI token: everything happens inside one workflow run, and
PyPI accepts the upload through [trusted
publishing](https://docs.pypi.org/trusted-publishers/) (OIDC). The
one-time setup for a fork is to register the trusted publisher on
pypi.org with owner `<owner>`, repository `github-actions-ingester`,
workflow `release.yaml` and environment `pypi`; do it as a pending
publisher before the first release so the first upload claims the
project name. The release workflow also accepts `workflow_dispatch` with
a `version` input and `push: tags ["v*"]`.

```bash
cosign verify ghcr.io/<owner>/github-actions-ingester@<digest> \
  --certificate-identity-regexp 'https://github\.com/<owner>/github-actions-ingester/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

### ArtifactHub indexing

ArtifactHub treats each chart as its own repository, so registration is
per chart with the full OCI URL:

1. Sign in at https://artifacthub.io with GitHub
2. Add a Helm OCI repository pointing to
   `oci://ghcr.io/<owner>/charts/github-actions-ingester`
3. Copy the assigned `repositoryID` into `helm/artifacthub-repo.yml`
4. Commit, push, and run `just artifacthub-publish` (or the
   `artifacthub-publish` workflow)

Every later chart release is indexed on ArtifactHub's next scrape.

## License

[MIT](LICENSE) — © 2026 Daniel Gines.
