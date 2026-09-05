"""Entry point — CLI with a handful of subcommands.

github-actions-ingester                 run forever (default)
github-actions-ingester run --once      one cycle, then exit
github-actions-ingester migrate         bootstrap/upgrade the schema only
github-actions-ingester check           validate config, GitHub auth, DB
github-actions-ingester app-manifest    print the GitHub App manifest
github-actions-ingester app-convert X   exchange a manifest code for creds
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path
from types import FrameType

import structlog
from pydantic import ValidationError

from . import __version__
from .app_manifest import convert_code, manifest_form_url, manifest_json, write_private_key
from .collector import Collector
from .config import Settings, load_settings
from .github import AppAuth, GitHubAPIError, GitHubClient, TokenAuth
from .metrics import Metrics
from .ratelimit import RateLimiter
from .server import MetricsServer
from .store import Store


def _setup_logging(level: str, fmt: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(message)s", stream=sys.stderr)
    # httpx logs one INFO line per request; the client has its own metrics
    # and structured events for that, so keep the library quiet unless
    # debugging.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(log_level, logging.WARNING))
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        # Not cached: a reconfigured stream (tests, supervisors that swap
        # stderr) must be picked up; the volume here is far too low to matter.
        cache_logger_on_first_use=False,
    )


def _build_client(settings: Settings, metrics: Metrics | None = None) -> GitHubClient:
    auth: TokenAuth | AppAuth
    if settings.uses_app():
        orgs = settings.org_list()
        auth = AppAuth(
            app_id=settings.github_app_id,
            private_key_pem=settings.app_private_key_pem(),
            installation_id=settings.github_app_installation_id,
            preferred_owner=orgs[0] if orgs else "",
        )
    else:
        auth = TokenAuth(settings.github_token)

    def on_request(status: int) -> None:
        if metrics is not None:
            metrics.github_requests_total.labels(status=str(status)).inc()

    return GitHubClient(
        auth=auth,
        base_url=settings.github_api_base,
        timeout=settings.api_timeout_seconds,
        limiter=RateLimiter(settings.api_rate_limit_rps),
        min_remaining=settings.api_min_remaining,
        max_retries=settings.api_max_retries,
        on_request=on_request,
    )


def _load(log_to_console: bool = False) -> Settings | None:
    try:
        settings = load_settings()
    except ValidationError as exc:
        sys.stderr.write(f"configuration error:\n{exc}\n")
        return None
    _setup_logging(settings.log_level, "console" if log_to_console else settings.log_format)
    return settings


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_run(once: bool) -> int:
    settings = _load()
    if settings is None:
        return 2
    log = structlog.get_logger("github_actions_ingester")
    log.info(
        "ingester.start",
        version=__version__,
        auth="app" if settings.uses_app() else "token",
        orgs=settings.org_list(),
        repos=settings.repo_list(),
        poll_interval=settings.poll_interval_seconds,
        backfill_days=settings.backfill_days,
        listen=f"{settings.listen_host}:{settings.listen_port}",
    )

    metrics = Metrics()
    metrics.build_info.info({"version": __version__, "python": sys.version.split()[0]})
    metrics.up.set(0)
    metrics.ready.set(0)

    store = Store(
        settings.database_url,
        settings.database_schema,
        settings.database_connect_timeout_seconds,
    )
    ready = threading.Event()
    server = MetricsServer(
        settings.listen_host, settings.listen_port, metrics.registry, ready.is_set
    )
    server.start()

    stop = threading.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("ingester.signal", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    exit_code = 0
    client: GitHubClient | None = None
    try:
        # Bootstrap the schema before anything else; retry while the
        # database is not there yet (rollouts, fresh clusters).
        while not stop.is_set():
            try:
                store.migrate()
                store.grant_read_access(settings.read_role_list())
                break
            except Exception as exc:
                log.error("store.bootstrap_failed", error=str(exc), retry_in=15)
                metrics.errors_total.labels(stage="bootstrap").inc()
                if once:
                    return 1
                stop.wait(15)
        if stop.is_set():
            return 0
        client = _build_client(settings, metrics)
        collector = Collector(client, store, metrics, settings)
        if once:
            result = collector.run_cycle()
            ready.set()
            exit_code = 0 if result != "error" else 1
        else:
            first = threading.Thread(
                target=lambda: collector.run_forever(stop), name="ingest", daemon=True
            )
            first.start()
            # Readiness flips after the first cycle, whatever its outcome:
            # the schema is up and metrics are meaningful from here on.
            while not stop.is_set() and collector.cycles == 0:
                stop.wait(1)
            ready.set()
            while not stop.is_set():
                stop.wait(1)
            first.join(timeout=30)
    finally:
        server.stop()
        if client is not None:
            client.close()
        store.close()
        log.info("ingester.stopped")
    return exit_code


def cmd_migrate() -> int:
    settings = _load(log_to_console=True)
    if settings is None:
        return 2
    store = Store(
        settings.database_url,
        settings.database_schema,
        settings.database_connect_timeout_seconds,
    )
    try:
        report = store.migrate()
        granted = store.grant_read_access(settings.read_role_list())
    finally:
        store.close()
    print(
        f"schema {settings.database_schema}: version {report.current_version}, "
        f"applied now: {report.applied or 'nothing'}"
    )
    if granted:
        print(f"read access granted to: {', '.join(granted)}")
    return 0


def cmd_check() -> int:
    settings = _load(log_to_console=True)
    if settings is None:
        return 2
    ok = True
    store = Store(
        settings.database_url,
        settings.database_schema,
        settings.database_connect_timeout_seconds,
    )
    try:
        version = store.schema_version()
        print(f"database: ok (schema {settings.database_schema} version {version})")
    except Exception as exc:
        print(f"database: FAILED ({exc})")
        ok = False
    finally:
        store.close()

    client = _build_client(settings)
    try:
        if settings.uses_app():
            repos = list(client.list_installation_repositories())
            print(f"github: ok (App, installation sees {len(repos)} repositories)")
        else:
            for org in settings.org_list():
                n = sum(1 for _ in client.list_org_repositories(org))
                print(f"github: ok (token, org {org}: {n} repositories)")
            for full_name in settings.repo_list():
                client.get_repository(full_name)
                print(f"github: ok (token, repo {full_name})")
        rl = client.rate_limit
        print(f"rate limit: {rl.remaining}/{rl.limit}")
    except GitHubAPIError as exc:
        print(f"github: FAILED ({exc})")
        ok = False
    finally:
        client.close()
    return 0 if ok else 1


def cmd_app_manifest(org: str, redirect_url: str, name: str) -> int:
    print(manifest_json(name=name, redirect_url=redirect_url))
    sys.stderr.write(f"\nPOST this manifest (form field `manifest`) to {manifest_form_url(org)}\n")
    return 0


def cmd_app_convert(code: str, key_file: str, api_base: str) -> int:
    try:
        body = convert_code(code, api_base)
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    pem = str(body.get("pem", ""))
    target = Path(key_file)
    write_private_key(pem, target)
    summary = {
        "app_id": body.get("id"),
        "client_id": body.get("client_id"),
        "slug": body.get("slug"),
        "html_url": body.get("html_url"),
        "private_key_file": str(target),
    }
    print(json.dumps(summary, indent=2))
    sys.stderr.write(
        "\nNext: install the App on your organization "
        f"({body.get('html_url')}/installations/new), then set\n"
        f"  GHA_GITHUB_APP_ID={body.get('id')}\n"
        f"  GHA_GITHUB_APP_PRIVATE_KEY_FILE={target}\n"
    )
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-actions-ingester",
        description="Ingest GitHub Actions runs and jobs into PostgreSQL.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the ingester (default)")
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")

    sub.add_parser("migrate", help="bootstrap or upgrade the database schema and exit")
    sub.add_parser("check", help="validate configuration, GitHub credentials and database")

    man = sub.add_parser("app-manifest", help="print the GitHub App manifest JSON")
    man.add_argument("--org", default="", help="organization that will own the App")
    man.add_argument("--redirect-url", default="", help="where GitHub sends the code")
    man.add_argument("--name", default="github-actions-ingester")

    conv = sub.add_parser("app-convert", help="exchange a manifest code for App credentials")
    conv.add_argument("code")
    conv.add_argument("--key-file", default="github-app.pem")
    conv.add_argument("--api-base", default="https://api.github.com")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in (None, "run"):
        return cmd_run(once=bool(getattr(args, "once", False)))
    if args.command == "migrate":
        return cmd_migrate()
    if args.command == "check":
        return cmd_check()
    if args.command == "app-manifest":
        return cmd_app_manifest(args.org, args.redirect_url, args.name)
    if args.command == "app-convert":
        return cmd_app_convert(args.code, args.key_file, args.api_base)
    return 2


if __name__ == "__main__":
    sys.exit(main())
