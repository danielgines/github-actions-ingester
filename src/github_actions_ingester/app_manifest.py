"""GitHub App Manifest flow helpers.

Lets every operator create *their own* GitHub App, inside their own
organization, with exactly the permissions the ingester needs and no
private key ever leaving their hands:

  1. ``github-actions-ingester app-manifest`` prints the manifest JSON
     (also embedded in ``examples/github-app/create-app.html``, a static
     page that POSTs it to GitHub);
  2. GitHub creates the App and redirects back with a one-hour ``code``;
  3. ``github-actions-ingester app-convert <code>`` exchanges it for the
     App ID and the private key (``POST /app-manifests/{code}/conversions``).

Reference: https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from .github import API_VERSION

PROJECT_URL = "https://github.com/danielgines/github-actions-ingester"

# Least privilege for everything the collector reads. `contents: read`
# only serves the workflow-file read behind GHA_SYNC_SCHEDULES.
DEFAULT_PERMISSIONS: dict[str, str] = {
    "actions": "read",
    "metadata": "read",
    "contents": "read",
}


def build_manifest(
    name: str = "github-actions-ingester",
    redirect_url: str = "",
    public: bool = False,
    description: str = "Ingests GitHub Actions runs and jobs into PostgreSQL for Grafana.",
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": name,
        "url": PROJECT_URL,
        "description": description,
        "public": public,
        "default_permissions": DEFAULT_PERMISSIONS,
        "default_events": [],
        "hook_attributes": {"active": False},
    }
    if redirect_url:
        manifest["redirect_url"] = redirect_url
    return manifest


def manifest_form_url(org: str = "") -> str:
    """Where the manifest must be POSTed (org-owned or user-owned App)."""
    if org:
        return f"https://github.com/organizations/{org}/settings/apps/new"
    return "https://github.com/settings/apps/new"


def convert_code(
    code: str, api_base: str = "https://api.github.com", timeout: float = 30.0
) -> dict[str, Any]:
    """Exchange the temporary ``code`` for the App credentials.

    The code is the credential: no token is sent. Returns the API body
    (``id``, ``slug``, ``client_id``, ``pem``, ``html_url`` ...).
    """
    resp = httpx.post(
        f"{api_base.rstrip('/')}/app-manifests/{code}/conversions",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "github-actions-ingester",
        },
        timeout=timeout,
    )
    if resp.status_code != 201:
        raise RuntimeError(
            f"conversion failed: HTTP {resp.status_code} {resp.text[:300]} "
            "(the code expires one hour after the App is created)"
        )
    body: dict[str, Any] = resp.json()
    return body


def write_private_key(pem: str, path: Path) -> None:
    path.write_text(pem, encoding="utf-8")
    path.chmod(0o600)


def manifest_json(**kwargs: Any) -> str:
    return json.dumps(build_manifest(**kwargs), indent=2)
