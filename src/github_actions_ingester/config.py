"""Configuration — env-var driven via pydantic-settings.

Env vars are prefixed ``GHA_`` so ``GHA_GITHUB_TOKEN`` / ``GHA_DATABASE_URL``
/ ``GHA_POLL_INTERVAL_SECONDS`` etc. are picked up automatically.

Two credential shapes are accepted, pick ONE:

  - a classic / fine-grained personal access token (``GHA_GITHUB_TOKEN``)
  - a GitHub App (``GHA_GITHUB_APP_ID`` + ``GHA_GITHUB_APP_PRIVATE_KEY`` or
    ``..._PRIVATE_KEY_FILE``, optionally ``GHA_GITHUB_APP_INSTALLATION_ID``)

The App is the recommended shape for an organization: least privilege
(``Actions: read`` + ``Metadata: read`` + optional ``Contents: read`` for
schedule discovery), no human account in the loop, and installation
tokens rotate hourly on their own.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GHA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- GitHub auth (pick one) ---
    github_token: str = Field(
        default="",
        description="Personal access token (classic or fine-grained) with "
        "`actions:read` on the target repositories. Mutually exclusive with "
        "the GitHub App settings.",
    )
    github_app_id: str = Field(
        default="",
        description="GitHub App ID (or client ID). Requires "
        "github_app_private_key / github_app_private_key_file.",
    )
    github_app_private_key: str = Field(
        default="",
        description="PEM-encoded private key of the GitHub App. Newlines may "
        "be literal or escaped as `\\n` (both are accepted).",
    )
    github_app_private_key_file: str = Field(
        default="",
        description="Path to the PEM private key. Alternative to the inline "
        "value — pair with a Secret volume mount in Kubernetes.",
    )
    github_app_installation_id: str = Field(
        default="",
        description="Installation ID of the App. When empty the ingester "
        "lists /app/installations and picks the one matching the first "
        "entry in `orgs` (or the only installation, if there is one).",
    )
    github_api_base: str = Field(
        default="https://api.github.com",
        description="Override the API base URL — set to "
        "https://ghe.example.com/api/v3 for GitHub Enterprise Server.",
    )

    # --- Scope ---
    orgs: str = Field(
        default="",
        description="Comma-separated organizations (or user accounts) whose "
        "repositories are ingested. Every repository the credential can see "
        "in each org is included unless excluded below.",
    )
    repos: str = Field(
        default="",
        description="Comma-separated explicit repositories as `owner/name`. "
        "Combined with `orgs` (union).",
    )
    exclude_repos: str = Field(
        default="",
        description="Comma-separated glob patterns matched against "
        "`owner/name` (fnmatch): e.g. `acme/legacy-*,acme/sandbox`.",
    )
    include_archived: bool = Field(
        default=False,
        description="Also ingest archived repositories. They rarely run "
        "workflows, so skipping them saves API budget.",
    )

    # --- Database ---
    database_url: str = Field(
        ...,
        description="PostgreSQL connection URL (libpq form): "
        "postgresql://user:pass@host:5432/dbname?sslmode=require. The role "
        "needs CREATE on the database the first time (schema bootstrap) "
        "and plain read/write afterwards.",
    )
    database_schema: str = Field(
        default="gha",
        description="Schema that holds every ingester table. Created on first start if missing.",
    )
    database_connect_timeout_seconds: int = Field(default=10, ge=1)

    # --- Collection ---
    poll_interval_seconds: int = Field(
        default=300,
        ge=30,
        description="Seconds between ingestion cycles. Each cycle costs "
        "roughly 1 request per repository (runs listing) + 1 per run that "
        "changed since the previous cycle (jobs).",
    )
    backfill_days: int = Field(
        default=30,
        ge=1,
        le=3660,
        description="How far back the FIRST cycle for a repository goes. "
        "Later cycles are incremental. Raising it later only affects "
        "repositories that have no cursor yet.",
    )
    lookback_minutes: int = Field(
        default=180,
        ge=1,
        description="Every cycle re-lists runs created within this window "
        "before the cursor, so runs that changed status (queued → in "
        "progress → completed) are refreshed even if a cycle was missed.",
    )
    repo_refresh_seconds: int = Field(
        default=3600,
        ge=60,
        description="How often the repository + workflow inventory is "
        "re-listed. New repositories / workflow files show up after at "
        "most this delay.",
    )
    max_open_run_refresh: int = Field(
        default=200,
        ge=0,
        description="Upper bound on individual `GET /runs/{id}` refreshes "
        "per cycle for runs still open outside the lookback window (long "
        "queues, multi-hour jobs). 0 disables.",
    )
    jobs_filter: str = Field(
        default="all",
        description="`all` ingests jobs from every run attempt (matches "
        "GitHub billing); `latest` keeps only the last attempt.",
    )
    sync_schedules: bool = Field(
        default=True,
        description="Read each workflow file from the default branch and "
        "record its `on.schedule` cron expressions (needs `Contents: read`). "
        "Powers the scheduled-workflow liveness metrics.",
    )
    schedule_refresh_seconds: int = Field(default=21600, ge=300)

    # --- API pacing ---
    api_rate_limit_rps: float = Field(
        default=5.0,
        gt=0.0,
        description="Max requests per second to the GitHub API. The "
        "documented secondary limit is ~900 points/min for REST; 5 rps is "
        "comfortably below it.",
    )
    api_min_remaining: int = Field(
        default=200,
        ge=0,
        description="When the primary rate-limit `remaining` drops under "
        "this value the ingester pauses until the limit resets instead of "
        "burning the last requests other tools may need.",
    )
    api_timeout_seconds: float = Field(default=30.0, gt=0.0)
    api_max_retries: int = Field(default=4, ge=0)

    # --- Server ---
    listen_host: str = Field(default="0.0.0.0", description="Bind address for /metrics.")
    listen_port: int = Field(default=9619, ge=1, le=65535)

    # --- Logging ---
    log_level: str = Field(default="info")
    log_format: str = Field(default="json", description="json or console")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @field_validator("log_level")
    @classmethod
    def _normalize_level(cls, v: str) -> str:
        v = v.lower()
        if v not in {"debug", "info", "warning", "warn", "error"}:
            raise ValueError(f"log_level must be debug/info/warning/error (got {v!r})")
        return "warning" if v == "warn" else v

    @field_validator("log_format")
    @classmethod
    def _normalize_format(cls, v: str) -> str:
        v = v.lower()
        if v not in {"json", "console"}:
            raise ValueError(f"log_format must be json or console (got {v!r})")
        return v

    @field_validator("jobs_filter")
    @classmethod
    def _jobs_filter(cls, v: str) -> str:
        v = v.lower()
        if v not in {"all", "latest"}:
            raise ValueError(f"jobs_filter must be all or latest (got {v!r})")
        return v

    @field_validator("database_schema")
    @classmethod
    def _schema_ident(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", v):
            raise ValueError(
                "database_schema must be a plain lowercase identifier (letters, digits, _)"
            )
        return v

    @field_validator("github_api_base")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _check_auth_and_scope(self) -> Settings:
        has_token = bool(self.github_token)
        has_app = bool(self.github_app_id)
        if has_token and has_app:
            raise ValueError("set either GHA_GITHUB_TOKEN or GHA_GITHUB_APP_ID, not both")
        if not has_token and not has_app:
            raise ValueError("no GitHub credential: set GHA_GITHUB_TOKEN or GHA_GITHUB_APP_ID")
        if has_app and not (self.github_app_private_key or self.github_app_private_key_file):
            raise ValueError(
                "GHA_GITHUB_APP_ID needs GHA_GITHUB_APP_PRIVATE_KEY or "
                "GHA_GITHUB_APP_PRIVATE_KEY_FILE"
            )
        for org in self.org_list():
            if not _OWNER_RE.match(org):
                raise ValueError(f"orgs entry {org!r} is not a valid GitHub login")
        for repo in self.repo_list():
            if not _REPO_RE.match(repo):
                raise ValueError(f"repos entry {repo!r} must look like owner/name")
        if not self.org_list() and not self.repo_list():
            raise ValueError("nothing to ingest: set GHA_ORGS and/or GHA_REPOS")
        return self

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def org_list(self) -> list[str]:
        return _split_csv(self.orgs)

    def repo_list(self) -> list[str]:
        return _split_csv(self.repos)

    def exclude_patterns(self) -> list[str]:
        return _split_csv(self.exclude_repos)

    def is_excluded(self, full_name: str) -> bool:
        name = full_name.lower()
        return any(fnmatch.fnmatchcase(name, pat.lower()) for pat in self.exclude_patterns())

    def app_private_key_pem(self) -> str:
        """Return the PEM text, reading the file when configured.

        Inline values often arrive with ``\\n`` escapes (Helm ``--set``,
        some secret managers); normalize them back to real newlines.
        """
        if self.github_app_private_key_file:
            return Path(self.github_app_private_key_file).read_text(encoding="utf-8")
        return self.github_app_private_key.replace("\\n", "\n")

    def uses_app(self) -> bool:
        return bool(self.github_app_id)


def load_settings(**overrides: Any) -> Settings:
    """Load settings, applying optional overrides on top of env."""
    return Settings(**overrides)
