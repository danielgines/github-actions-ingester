from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from github_actions_ingester.app_manifest import (
    DEFAULT_PERMISSIONS,
    PROJECT_URL,
    build_manifest,
    convert_code,
    manifest_form_url,
    manifest_json,
    write_private_key,
)


def test_manifest_shape_matches_github_contract() -> None:
    m = build_manifest(name="my-ingester", redirect_url="https://example.com/cb")
    assert m["name"] == "my-ingester"
    assert m["url"] == PROJECT_URL  # required by GitHub
    assert m["redirect_url"] == "https://example.com/cb"
    assert m["public"] is False
    assert (
        m["default_permissions"]
        == DEFAULT_PERMISSIONS
        == {
            "actions": "read",
            "metadata": "read",
            "contents": "read",
        }
    )
    assert m["default_events"] == []
    assert m["hook_attributes"] == {"active": False}  # no webhook needed


def test_redirect_url_is_optional() -> None:
    assert "redirect_url" not in build_manifest()
    assert json.loads(manifest_json())["name"] == "github-actions-ingester"


def test_form_url() -> None:
    assert manifest_form_url("acme") == "https://github.com/organizations/acme/settings/apps/new"
    assert manifest_form_url() == "https://github.com/settings/apps/new"


def test_convert_code_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/app-manifests/abc123/conversions",
        status_code=201,
        json={
            "id": 42,
            "slug": "my-ingester",
            "client_id": "Iv1.x",
            "pem": "PEM",
            "html_url": "https://github.com/apps/my-ingester",
        },
    )
    body = convert_code("abc123")
    assert body["id"] == 42 and body["pem"] == "PEM"
    req = httpx_mock.get_request()
    assert req is not None and "Authorization" not in req.headers  # the code is the credential


def test_convert_code_failure_mentions_expiry(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://ghe.example.com/api/v3/app-manifests/bad/conversions",
        status_code=404,
        text="Not Found",
    )
    with pytest.raises(RuntimeError, match="expires one hour"):
        convert_code("bad", "https://ghe.example.com/api/v3/")


def test_write_private_key_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "app.pem"
    write_private_key("PEM\n", target)
    assert target.read_text() == "PEM\n"
    assert oct(target.stat().st_mode & 0o777) == "0o600"
