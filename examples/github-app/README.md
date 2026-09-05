# Creating the GitHub App

`github-actions-ingester` reads through a GitHub App you create inside your
own organization. No personal access token, no shared credential: GitHub
generates the private key and hands it only to you. The App needs three
read-only permissions:

| Permission | Why |
|---|---|
| Actions: read | workflows, runs and jobs |
| Metadata: read | repository listing (mandatory for every App) |
| Contents: read | reads each workflow file once to learn its `on.schedule` (only when `GHA_SYNC_SCHEDULES=true`, the default) |

## Option A: the manifest page (one click)

1. Open [`create-app.html`](create-app.html) in a browser (download it or serve
   the folder; it is a static page with no dependencies).
2. Fill in the organization and click **Create GitHub App on github.com**.
   GitHub shows the App to be created; confirm.
3. GitHub redirects to the redirect URL with `?code=XXXX` in the address bar.
   Copy the code and, within one hour, run:

   ```bash
   github-actions-ingester app-convert XXXX --key-file github-app.pem
   # or, without installing anything:
   docker run --rm -v "$PWD:/out" -w /out ghcr.io/danielgines/github-actions-ingester \
     github-actions-ingester app-convert XXXX --key-file github-app.pem
   ```

   The command prints the App ID and writes the private key (mode 600).
4. Install the App on the repositories you want to ingest (**Install App**
   on the App page).

## Option B: the CLI

```bash
github-actions-ingester app-manifest --org my-org --redirect-url https://example.invalid/done
```

prints the manifest JSON and the form URL. Submit it as an HTML form with a
`manifest` field (that is what the page above does), then follow step 3.

## Option C: by hand

Settings → Developer settings → GitHub Apps → New GitHub App. Disable the
webhook, set the three permissions above, generate a private key, install
the App. Note the App ID.

## Configure the ingester

```bash
GHA_GITHUB_APP_ID=123456
GHA_GITHUB_APP_PRIVATE_KEY_FILE=/secrets/github-app.pem   # or GHA_GITHUB_APP_PRIVATE_KEY with the PEM inline
GHA_ORGS=my-org
```

The installation is discovered automatically (the one whose account matches
the first entry of `GHA_ORGS`, or the only one). Set
`GHA_GITHUB_APP_INSTALLATION_ID` when the App is installed in several
accounts and none matches.

Reference: [Registering a GitHub App from a manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest).
