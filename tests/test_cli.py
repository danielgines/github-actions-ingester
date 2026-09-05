"""CLI entry points, including ``run --once`` end to end (mocked GitHub, real database)."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from github_actions_ingester import __version__
from github_actions_ingester.__main__ import build_parser, main
from github_actions_ingester.store import Store

API = "https://api.github.com"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_parser_defaults() -> None:
    p = build_parser()
    assert p.parse_args([]).command is None
    assert p.parse_args(["run", "--once"]).once is True
    assert p.parse_args(["app-convert", "c"]).key_file == "github-app.pem"


def test_app_manifest_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["app-manifest", "--org", "acme", "--redirect-url", "https://x/cb", "--name", "n"])
        == 0
    )
    out, err = capsys.readouterr()
    manifest = json.loads(out)
    assert manifest["name"] == "n" and manifest["redirect_url"] == "https://x/cb"
    assert "organizations/acme/settings/apps/new" in err


def test_app_convert_writes_key(
    httpx_mock: HTTPXMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{API}/app-manifests/code1/conversions",
        status_code=201,
        json={
            "id": 7,
            "slug": "s",
            "client_id": "Iv1.c",
            "pem": "PEMDATA",
            "html_url": "https://github.com/apps/s",
        },
    )
    key = tmp_path / "k.pem"
    assert main(["app-convert", "code1", "--key-file", str(key)]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["app_id"] == 7
    assert key.read_text() == "PEMDATA"
    assert "GHA_GITHUB_APP_ID=7" in err


def test_app_convert_failure_exit_code(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    httpx_mock.add_response(
        method="POST", url=f"{API}/app-manifests/x/conversions", status_code=404
    )
    assert main(["app-convert", "x", "--key-file", str(tmp_path / "k")]) == 1


def test_invalid_configuration_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("GHA_GITHUB_TOKEN", "GHA_DATABASE_URL", "GHA_ORGS"):
        monkeypatch.delenv(name, raising=False)
    assert main(["migrate"]) == 2
    assert "configuration error" in capsys.readouterr().err


@pytest.mark.integration
def test_migrate_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
    schema_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHA_GITHUB_TOKEN", "t")
    monkeypatch.setenv("GHA_DATABASE_URL", database_url)
    monkeypatch.setenv("GHA_DATABASE_SCHEMA", schema_name)
    monkeypatch.setenv("GHA_ORGS", "acme")
    assert main(["migrate"]) == 0
    assert "applied now: ['0001_initial.sql']" in capsys.readouterr().out
    assert main(["migrate"]) == 0
    assert "applied now: nothing" in capsys.readouterr().out


@pytest.mark.integration
def test_check_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
    schema_name: str,
    httpx_mock: HTTPXMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHA_GITHUB_TOKEN", "t")
    monkeypatch.setenv("GHA_DATABASE_URL", database_url)
    monkeypatch.setenv("GHA_DATABASE_SCHEMA", schema_name)
    monkeypatch.setenv("GHA_ORGS", "acme")
    monkeypatch.setenv("GHA_REPOS", "acme/web")
    httpx_mock.add_response(
        url=f"{API}/orgs/acme/repos?type=all&sort=full_name&per_page=100",
        json=[_repo()],
        headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4999"},
    )
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web",
        json=_repo(),
        headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4998"},
    )
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "database: ok" in out and "version None" in out  # not bootstrapped yet
    assert "github: ok (token, org acme: 1 repositories)" in out
    assert "github: ok (token, repo acme/web)" in out
    assert "rate limit: 4998/5000" in out


@pytest.mark.integration
def test_check_reports_github_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
    schema_name: str,
    httpx_mock: HTTPXMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHA_GITHUB_TOKEN", "bad")
    monkeypatch.setenv("GHA_DATABASE_URL", database_url)
    monkeypatch.setenv("GHA_DATABASE_SCHEMA", schema_name)
    monkeypatch.setenv("GHA_ORGS", "acme")
    httpx_mock.add_response(
        url=f"{API}/orgs/acme/repos?type=all&sort=full_name&per_page=100",
        status_code=401,
        text="Bad credentials",
    )
    assert main(["check"]) == 1
    assert "github: FAILED" in capsys.readouterr().out


@pytest.mark.integration
def test_run_once_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
    schema_name: str,
    httpx_mock: HTTPXMock,
) -> None:
    """Fresh database → schema bootstrapped → inventory, runs, jobs, schedules ingested."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHA_GITHUB_TOKEN", "t")
    monkeypatch.setenv("GHA_DATABASE_URL", database_url)
    monkeypatch.setenv("GHA_DATABASE_SCHEMA", schema_name)
    monkeypatch.setenv("GHA_ORGS", "acme")
    monkeypatch.setenv("GHA_LISTEN_PORT", str(_free_port()))
    monkeypatch.setenv("GHA_LOG_FORMAT", "console")
    httpx_mock.add_response(
        url=f"{API}/orgs/acme/repos?type=all&sort=full_name&per_page=100", json=[_repo()]
    )
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/actions/workflows?per_page=100",
        json={
            "total_count": 1,
            "workflows": [
                {
                    "id": 10,
                    "name": "Nightly",
                    "path": ".github/workflows/nightly.yml",
                    "state": "active",
                }
            ],
        },
    )
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/contents/.github/workflows/nightly.yml?ref=main",
        json={
            "encoding": "base64",
            "content": "b246CiAgc2NoZWR1bGU6CiAgICAtIGNyb246ICcwIDIgKiAqIConCg==",
        },
    )

    def runs(request: httpx.Request) -> httpx.Response:
        assert "created" in request.url.params
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 500,
                        "repository": {"id": 1},
                        "workflow_id": 10,
                        "event": "schedule",
                        "status": "completed",
                        "conclusion": "success",
                        "head_branch": "main",
                        "created_at": "2026-03-10T02:00:00Z",
                        "updated_at": "2026-03-10T02:04:00Z",
                        "run_started_at": "2026-03-10T02:00:10Z",
                    }
                ],
            },
        )

    httpx_mock.add_callback(
        runs, method="GET", url=re.compile(rf"{API}/repos/acme/web/actions/runs\?.*")
    )
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/actions/runs/500/jobs?filter=all&per_page=100",
        json={
            "total_count": 1,
            "jobs": [
                {
                    "id": 900,
                    "run_id": 500,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "runner_name": "ubuntu",
                    "started_at": "2026-03-10T02:00:20Z",
                    "completed_at": "2026-03-10T02:03:30Z",
                }
            ],
        },
    )

    assert main(["run", "--once"]) == 0

    store = Store(database_url, schema_name)
    try:
        assert store.schema_version() == 1
        assert store.counts() == {
            "repositories": 1,
            "workflows": 1,
            "runs": 1,
            "open_runs": 0,
            "jobs": 1,
        }
        (row,) = store.scheduled_workflow_status()
        assert row["interval_seconds"] == 86400.0
        assert row["last_conclusion"] == "success"
        assert store.get_cursor(1) is not None
    finally:
        store.close()

    # Second start: nothing to migrate, cycle still succeeds (incremental, same responses).
    httpx_mock.reset()
    httpx_mock.add_callback(
        runs, method="GET", url=re.compile(rf"{API}/repos/acme/web/actions/runs\?.*")
    )
    httpx_mock.add_response(
        url=f"{API}/orgs/acme/repos?type=all&sort=full_name&per_page=100", json=[_repo()]
    )
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/actions/workflows?per_page=100",
        json={
            "total_count": 1,
            "workflows": [
                {
                    "id": 10,
                    "name": "Nightly",
                    "path": ".github/workflows/nightly.yml",
                    "state": "active",
                }
            ],
        },
    )
    assert main(["run", "--once"]) == 0
    assert all(not str(r.url).endswith("/jobs") for r in httpx_mock.get_requests())


@pytest.mark.integration
def test_run_service_mode_serves_readiness_and_stops_on_sigterm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
    schema_name: str,
    httpx_mock: HTTPXMock,
) -> None:
    """``run`` (no --once): /readyz flips after the first cycle; SIGTERM exits 0."""
    port = _free_port()
    _env(monkeypatch, tmp_path, database_url, schema_name, port)
    _mock_inventory(httpx_mock)
    httpx_mock.add_response(
        url=re.compile(rf"{API}/repos/acme/web/actions/runs\?.*"),
        json={"total_count": 0, "workflow_runs": []},
        is_reusable=True,
    )
    seen: dict[str, int] = {}

    def probe(path: str) -> int:
        # urllib, not httpx: the httpx mock would swallow these local calls.
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as r:
                return int(r.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except OSError:
            return 0

    def watcher() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            code = probe("/readyz")
            if code:
                seen.setdefault("first_readyz", code)
            if code == 200:
                seen["ready"] = 200
                seen["metrics"] = probe("/metrics")
                break
            time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=watcher, daemon=True)
    t.start()
    assert main(["run"]) == 0
    t.join(timeout=5)
    assert seen.get("ready") == 200 and seen.get("metrics") == 200


@pytest.mark.integration
def test_run_once_fails_fast_when_database_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHA_GITHUB_TOKEN", "t")
    monkeypatch.setenv("GHA_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x?connect_timeout=1")
    monkeypatch.setenv("GHA_ORGS", "acme")
    monkeypatch.setenv("GHA_LISTEN_PORT", str(_free_port()))
    assert main(["run", "--once"]) == 1


def _env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str, schema: str, port: int
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHA_GITHUB_TOKEN", "t")
    monkeypatch.setenv("GHA_DATABASE_URL", database_url)
    monkeypatch.setenv("GHA_DATABASE_SCHEMA", schema)
    monkeypatch.setenv("GHA_ORGS", "acme")
    monkeypatch.setenv("GHA_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("GHA_LISTEN_PORT", str(port))
    monkeypatch.setenv("GHA_LOG_FORMAT", "console")


def _mock_inventory(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/orgs/acme/repos?type=all&sort=full_name&per_page=100", json=[_repo()]
    )
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/actions/workflows?per_page=100",
        json={
            "total_count": 1,
            "workflows": [
                {
                    "id": 10,
                    "name": "Nightly",
                    "path": ".github/workflows/nightly.yml",
                    "state": "active",
                }
            ],
        },
    )
    httpx_mock.add_response(
        url=f"{API}/repos/acme/web/contents/.github/workflows/nightly.yml?ref=main",
        json={
            "encoding": "base64",
            "content": "b246CiAgc2NoZWR1bGU6CiAgICAtIGNyb246ICcwIDIgKiAqIConCg==",
        },
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _repo() -> dict[str, object]:
    return {
        "id": 1,
        "owner": {"login": "acme"},
        "name": "web",
        "full_name": "acme/web",
        "default_branch": "main",
    }
