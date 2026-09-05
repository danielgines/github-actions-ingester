# github-actions-ingester

Helm chart for [github-actions-ingester](https://github.com/danielgines/github-actions-ingester):
a service that ingests GitHub Actions workflows, runs and jobs into
PostgreSQL and exposes Prometheus metrics about the ingestion and about
scheduled-workflow liveness. Deploys a single container that exposes:

- **`/metrics`**: `gha_ingester_*` (health, rate limit, inventory, rows
  written) and `gha_scheduled_*` (last scheduled run / expected interval /
  last conclusion per workflow)
- **`/healthz`, `/readyz`**: kubelet probes. Readiness turns green only
  after the schema is bootstrapped and the first cycle finished.

The chart does **not** deploy PostgreSQL or Grafana. Bring your own
database (CloudNativePG, a managed service, the Bitnami chart) and point
Grafana's PostgreSQL datasource at the ingester's schema with a read-only
role.

Runs single-replica by design; see below.

## TL;DR

```bash
helm install github-actions-ingester \
  oci://ghcr.io/danielgines/charts/github-actions-ingester \
  --version 0.1.0 \
  --namespace github-actions-ingester --create-namespace \
  --set auth.token=$YOUR_GITHUB_TOKEN \
  --set database.url=postgresql://gha:pass@postgres-rw.db.svc:5432/gha \
  --set config.orgs=my-org
```

On start the ingester creates the schema (`database.schema`, default
`gha`), applies its migrations, and begins the backfill
(`config.backfillDays`, default 30). `/readyz` answers 503 until the
first cycle completes; with a large organization that can take several
minutes (roughly 8 minutes per 1000 runs backfilled), which the readiness
`failureThreshold` allows for. `helm install --wait` uses the same signal,
so on a large first backfill it may report a timeout while the pod is
still working: check the logs for `cycle.done`, or pass a longer
`--timeout`.

## Required configuration

| Parameter | Required? | Description |
|---|---|---|
| `auth.existingSecret` OR `auth.token` OR `auth.app.*` | **yes** | GitHub credential. `existingSecret` is a Secret whose keys are the env vars themselves (`GHA_GITHUB_TOKEN`, or `GHA_GITHUB_APP_ID` + `GHA_GITHUB_APP_PRIVATE_KEY` [+ `GHA_GITHUB_APP_INSTALLATION_ID`]); it is mounted with `envFrom`, so any other `GHA_*` key in it is honoured too |
| `database.existingSecret` OR `database.url` | **yes** | libpq URL. `existingSecretKey` (default `GHA_DATABASE_URL`) names the key; CloudNativePG app Secrets use `uri` |
| `config.orgs` and/or `config.repos` | **yes** | what to ingest |

## Common configurations

### Production: GitHub App from External Secrets, database from CloudNativePG

```yaml
auth:
  existingSecret: github-actions-ingester-github   # GHA_GITHUB_APP_ID + GHA_GITHUB_APP_PRIVATE_KEY

database:
  existingSecret: gha-db-app        # CNPG "app" Secret of the database
  existingSecretKey: uri
  schema: gha

config:
  orgs: my-org
  excludeRepos: "my-org/sandbox-*"
  backfillDays: 90

serviceMonitor:
  enabled: true
prometheusRule:
  enabled: true
  labels:
    release: kube-prometheus-stack   # whatever your Prometheus selects on
```

### Explicit repositories with a token, console logs

```yaml
auth:
  token: ghp_...            # local tests only; never commit
database:
  url: postgresql://gha:gha@postgres.default.svc:5432/gha
config:
  repos: "acme/web,acme/api"
  logFormat: console
```

### Locked-down ingress + egress (NetworkPolicy)

```yaml
networkPolicy:
  enabled: true
  ingress:
    from:
      - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: monitoring } }
        podSelector:       { matchLabels: { app.kubernetes.io/name: prometheus } }
  egress:
    allowDNS: true
    allowGitHubAPI: true            # HTTPS to non-RFC1918 (api.github.com rotates IPs)
    database:
      port: 5432
      to:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: cnpg-system } }
          podSelector:       { matchLabels: { cnpg.io/cluster: platform-postgres } }
```

Requires a CNI that enforces NetworkPolicy (Cilium, Calico, Antrea).
For a GitHub Enterprise Server on a private address, set
`allowGitHubAPI: false` and add the host under `egress.extra`.

### Alert thresholds

```yaml
prometheusRule:
  enabled: true
  scheduledMissedMultiplier: 2     # stopped when last run older than 2 × interval ...
  scheduledGraceSeconds: 1800      # ... plus 30 min for GitHub's own scheduling delay
  ingesterStaleSeconds: 1800
  rateLimitRemainingThreshold: 300
  additionalRules: []              # raw PrometheusRule rules appended to the group
```

### Strict Pod Security / Kyverno-policy environments

The chart is compliant by default with the `restricted` Pod Security
Standard: `runAsNonRoot` UID 1000, `readOnlyRootFilesystem`,
`capabilities: drop: [ALL]`, `allowPrivilegeEscalation: false`,
`seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false`
(the ingester never calls the Kubernetes API).

## Single replica, by design

`replicaCount` is bounded to 1 by `values.schema.json`. Each replica
would run its own ingestion loop against the same repositories: double
the GitHub API spend and two writers on the same cursors. `strategy` is
`Recreate` so an upgrade never overlaps two ingesters on one schema.

For clusters that require a PDB and an HPA on every workload:

```yaml
podDisruptionBudget:
  enabled: true              # default; maxUnavailable 1 never blocks a drain
autoscaling:
  enabled: true              # compliance-only: min == max == 1
```

When `autoscaling.enabled` is true `spec.replicas` is omitted from the
Deployment so the HPA owns the field and GitOps tools do not fight it.

## Values

| Key | Default | Description |
|---|---|---|
| `image.repository` | `ghcr.io/danielgines/github-actions-ingester` | |
| `image.tag` | `""` | defaults to `.Chart.appVersion` |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | |
| `replicaCount` | `1` | bounded to 1 |
| `strategy.type` | `Recreate` | |
| `auth.existingSecret` | `""` | Secret with `GHA_GITHUB_TOKEN` or `GHA_GITHUB_APP_*` keys |
| `auth.token` | `""` | inline PAT (wrapped in a Secret) |
| `auth.app.id` / `auth.app.privateKey` / `auth.app.installationId` | `""` | inline GitHub App credential |
| `database.existingSecret` | `""` | Secret holding the connection URL |
| `database.existingSecretKey` | `GHA_DATABASE_URL` | key inside that Secret |
| `database.url` | `""` | inline URL (wrapped in a Secret) |
| `database.schema` | `gha` | schema holding every table |
| `database.connectTimeoutSeconds` | `10` | |
| `config.orgs` / `config.repos` / `config.excludeRepos` | `""` | scope |
| `config.includeArchived` | `false` | |
| `config.apiBase` | `https://api.github.com` | GHES: `https://ghe.example.com/api/v3` |
| `config.apiRateLimitRps` | `5.0` | |
| `config.apiMinRemaining` | `200` | |
| `config.apiTimeoutSeconds` | `30` | |
| `config.apiMaxRetries` | `4` | |
| `config.pollIntervalSeconds` | `300` | min 30 |
| `config.backfillDays` | `30` | 1..3660 |
| `config.lookbackMinutes` | `180` | |
| `config.repoRefreshSeconds` | `3600` | |
| `config.maxOpenRunRefresh` | `200` | |
| `config.jobsFilter` | `all` | `all` / `latest` |
| `config.syncSchedules` | `true` | needs Contents: read |
| `config.scheduleRefreshSeconds` | `21600` | |
| `config.logLevel` | `info` | |
| `config.logFormat` | `json` | |
| `extraEnv` | `[]` | raw EnvVar objects |
| `service.type` / `service.port` | `ClusterIP` / `9619` | |
| `serviceMonitor.enabled` | `false` | Prometheus Operator |
| `serviceMonitor.interval` / `scrapeTimeout` | `60s` / `30s` | |
| `serviceMonitor.labels` / `relabelings` / `metricRelabelings` | `{}` / `[]` / `[]` | |
| `prometheusRule.enabled` | `false` | |
| `prometheusRule.labels` | `{}` | |
| `prometheusRule.scheduledMissedMultiplier` | `2` | |
| `prometheusRule.scheduledGraceSeconds` | `1800` | |
| `prometheusRule.ingesterStaleSeconds` | `1800` | |
| `prometheusRule.rateLimitRemainingThreshold` | `300` | |
| `prometheusRule.additionalRules` | `[]` | |
| `resources` | `50m/96Mi` requests, `500m/256Mi` limits | |
| `podSecurityContext` / `containerSecurityContext` | restricted PSS | |
| `probes.liveness` / `probes.readiness` / `probes.startup` | `/healthz` / `/readyz` / `{}` | |
| `nodeSelector` / `tolerations` / `affinity` | `{}` / `[]` / `{}` | |
| `podLabels` / `podAnnotations` | `{}` | |
| `priorityClassName` | `""` | |
| `terminationGracePeriodSeconds` | `30` | |
| `serviceAccount.create` / `name` / `annotations` | `true` / `""` / `{}` | |
| `serviceAccount.automountServiceAccountToken` | `false` | |
| `podDisruptionBudget.enabled` | `true` | |
| `podDisruptionBudget.maxUnavailable` | `1` | |
| `podDisruptionBudget.unhealthyPodEvictionPolicy` | `AlwaysAllow` | k8s 1.27+ |
| `autoscaling.enabled` | `false` | compliance-only, min == max == 1 |
| `networkPolicy.enabled` | `false` | |
| `networkPolicy.ingress.from` | `[]` | NetworkPolicyPeer list |
| `networkPolicy.egress.allowDNS` | `true` | |
| `networkPolicy.egress.allowGitHubAPI` | `true` | |
| `networkPolicy.egress.database.port` / `to` | `5432` / `[]` | |
| `networkPolicy.egress.extra` | `[]` | raw egress rules |

`values.schema.json` rejects unknown top-level keys and out-of-range
values at `install` / `upgrade` / `template` / `lint` time.

## Upgrading

Schema migrations run inside the ingester on start. An upgrade that ships
a new migration applies it during the rollout; a downgrade does not undo
it (migrations are forward-only). Back up the schema before a major
version bump.
