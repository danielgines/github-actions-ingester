# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [0.2.1] — 2026-09-05

### Added
- Chart: `deploymentAnnotations`, for a Reloader annotation on the
  Deployment.

### Fixed
- A read role with a name different from the schema could not resolve the
  views by bare name, and the Grafana PostgreSQL datasource has no
  search_path setting. After granting read access the ingester now sets
  the database's default `search_path` to the schema (needs the ingester
  role to own the database; skipped with a warning otherwise).

## [0.2.0] — 2026-09-05

### Added
- Chart: `image.digest` renders the image as `repo:tag@digest` for
  clusters that require digest pins.
- Chart: `extraVolumes` and `extraVolumeMounts`, so a CA bundle can be
  mounted for `sslmode=verify-full`.
- `GHA_DATABASE_READ_ROLES` (chart: `database.readRoles`): existing roles
  that get read-only access to the schema after every migration run,
  USAGE plus SELECT on every table and view and a default privilege for
  tables added later. Lets Grafana read through its own role instead of
  the ingester's credentials.

### Fixed
- `examples/github-app/create-app.html` filled the `manifest` field only
  on submit; a viewer that posts before the listener runs sent an empty
  manifest and GitHub answered `"url" wasn't supplied`. The fields are
  now filled on load and on every edit.

## [0.1.0] — 2026-09-05

First release.

### Added

- **Ingester service** (`github-actions-ingester run`): periodic cycles that
  list repositories, workflow files, workflow runs and jobs from the GitHub
  REST API and upsert them into PostgreSQL. Incremental cursors per
  repository, configurable backfill, lookback window for status changes,
  bounded refresh of long-running open runs, automatic splitting of
  listing windows over GitHub's 1 000-run cap.
- **Two credential shapes**: personal access token, or a GitHub App
  (RS256 JWT → installation token, cached and refreshed before expiry,
  installation auto-discovered by account). GitHub Enterprise Server via
  `GHA_GITHUB_API_BASE`.
- **GitHub App manifest flow**: `app-manifest` prints the manifest,
  `app-convert` exchanges the one-time code for the App ID and private
  key; `examples/github-app/create-app.html` does the same from a browser
  so any organization can create its own App in one click.
- **Schema lifecycle**: embedded SQL migrations applied on start
  (bootstrap, upgrade, or no-op) and tracked in `schema_migrations` with
  checksums; `migrate` subcommand for init containers.
- **Compatibility views** `minion_repositories`, `minion_workflow_files`,
  `minion_workflow_runs`, `minion_workflow_jobs` so Grafana dashboard
  24157 "GitHub Actions insights" works unchanged.
- **Scheduled-workflow liveness**: `on.schedule` crons read from each
  workflow file, expected interval computed as the longest gap between
  consecutive fires (all crons merged), and per-workflow gauges for last
  scheduled run and last conclusion.
- **Prometheus metrics** on `/metrics` (`gha_ingester_*`,
  `gha_scheduled_*`), `/healthz` and `/readyz` probes.
- **Rate-limit handling**: client-side pacing, `Retry-After` /
  `X-RateLimit-Reset` on 403/429, exponential backoff on 5xx and
  transport errors, pause when the primary budget drops under
  `GHA_API_MIN_REMAINING`.
- **`check` subcommand**: validates configuration, credential, scope and
  database and prints the remaining rate limit.
- **Helm chart** with `values.schema.json`, ServiceMonitor,
  PrometheusRule (ingester down / stale / rate limit low, scheduled
  workflow stopped / never ran / failing), NetworkPolicy, PDB,
  compliance-only HPA, restricted Pod Security defaults, and
  `existingSecret` shapes for both the GitHub credential and the
  database URL.
- **Examples**: Docker Compose stack (PostgreSQL + ingester + Grafana
  with the dashboard provisioned), plain Kubernetes manifests, Grafana
  provisioning files.
- **Container image** `ghcr.io/danielgines/github-actions-ingester`
  (linux/amd64 + linux/arm64, non-root, read-only root filesystem, tini).
- **CI/CD**: ruff, mypy, pytest with embedded PostgreSQL, helm lint
  and template checks, Trivy, cosign keyless signing, chart published to
  `oci://ghcr.io/danielgines/charts/github-actions-ingester`, package
  published to PyPI through trusted publishing.
