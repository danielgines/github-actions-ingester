from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from github_actions_ingester.config import load_settings

BASE = {"database_url": "postgresql://u:p@db/gha", "orgs": "acme"}


def test_token_auth_minimal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env file around
    monkeypatch.setenv("GHA_GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("GHA_DATABASE_URL", BASE["database_url"])
    monkeypatch.setenv("GHA_ORGS", "acme, Beta ")
    s = load_settings()
    assert s.uses_app() is False
    assert s.org_list() == ["acme", "Beta"]
    assert s.poll_interval_seconds == 300
    assert s.database_schema == "gha"


def test_token_and_app_are_exclusive() -> None:
    with pytest.raises(ValidationError, match="not both"):
        load_settings(github_token="x", github_app_id="1", github_app_private_key="k", **BASE)


def test_no_credential_rejected() -> None:
    with pytest.raises(ValidationError, match="no GitHub credential"):
        load_settings(**BASE)


def test_app_needs_private_key() -> None:
    with pytest.raises(ValidationError, match="PRIVATE_KEY"):
        load_settings(github_app_id="1", **BASE)


def test_nothing_to_ingest() -> None:
    with pytest.raises(ValidationError, match="nothing to ingest"):
        load_settings(github_token="x", database_url=BASE["database_url"])


def test_repo_and_org_shape_validated() -> None:
    with pytest.raises(ValidationError, match="owner/name"):
        load_settings(github_token="x", database_url=BASE["database_url"], repos="not-a-repo")
    with pytest.raises(ValidationError, match="valid GitHub login"):
        load_settings(github_token="x", database_url=BASE["database_url"], orgs="bad org")


def test_exclude_patterns_are_globs_case_insensitive() -> None:
    s = load_settings(github_token="x", exclude_repos="acme/legacy-*,ACME/sandbox", **BASE)
    assert s.is_excluded("acme/legacy-api")
    assert s.is_excluded("acme/Sandbox")
    assert not s.is_excluded("acme/web")


def test_private_key_escaped_newlines_normalized() -> None:
    s = load_settings(
        github_app_id="1",
        github_app_private_key="-----BEGIN\\nabc\\n-----END",
        **BASE,
    )
    assert s.app_private_key_pem() == "-----BEGIN\nabc\n-----END"
    assert s.uses_app()


def test_private_key_file_wins(tmp_path: Path) -> None:
    key = tmp_path / "k.pem"
    key.write_text("PEMDATA", encoding="utf-8")
    s = load_settings(github_app_id="1", github_app_private_key_file=str(key), **BASE)
    assert s.app_private_key_pem() == "PEMDATA"


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("poll_interval_seconds", 5, "greater than or equal to 30"),
        ("backfill_days", 0, "greater than or equal to 1"),
        ("backfill_days", 5000, "less than or equal to 3660"),
        ("jobs_filter", "some", "all or latest"),
        ("log_level", "loud", "log_level must be"),
        ("log_format", "xml", "json or console"),
        ("database_schema", "Bad-Name", "plain lowercase identifier"),
        ("listen_port", 70000, "less than or equal to 65535"),
    ],
)
def test_bounds(field: str, value: object, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        load_settings(github_token="x", **BASE, **{field: value})


def test_normalizations() -> None:
    s = load_settings(
        github_token="x",
        log_level="WARN",
        log_format="Console",
        jobs_filter="LATEST",
        github_api_base="https://ghe.example.com/api/v3/",
        **BASE,
    )
    assert s.log_level == "warning"
    assert s.log_format == "console"
    assert s.jobs_filter == "latest"
    assert s.github_api_base == "https://ghe.example.com/api/v3"


def test_read_roles_parsed_and_validated() -> None:
    s = load_settings(github_token="x", database_read_roles=" grafana, reporting_ro ", **BASE)
    assert s.read_role_list() == ["grafana", "reporting_ro"]
    assert load_settings(github_token="x", **BASE).read_role_list() == []
    with pytest.raises(ValidationError, match="plain identifier"):
        load_settings(github_token="x", database_read_roles="grafana; drop", **BASE)
