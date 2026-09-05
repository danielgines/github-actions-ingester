"""Shared fixtures.

Unit tests need nothing. Tests marked ``integration`` need a PostgreSQL:
set ``GHA_TEST_DATABASE_URL`` (CI does, via a service container) or let
``pgserver`` start an embedded instance in a temporary directory.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from github_actions_ingester.store import Store


def _embedded_database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    try:
        import pgserver
    except ImportError:  # pragma: no cover - CI always has the service container
        pytest.skip("no GHA_TEST_DATABASE_URL and pgserver is not installed")
    server = pgserver.get_server(  # type: ignore[attr-defined]
        str(tmp_path_factory.mktemp("pg"))
    )
    try:
        yield str(server.get_uri())
    finally:
        server.cleanup()


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    url = os.environ.get("GHA_TEST_DATABASE_URL", "")
    if url:
        yield url
        return
    yield from _embedded_database_url(tmp_path_factory)


@pytest.fixture
def schema_name(database_url: str) -> Iterator[str]:
    """A fresh schema per test, dropped afterwards."""
    name = "t_" + uuid.uuid4().hex[:12]
    yield name
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


@pytest.fixture
def store(database_url: str, schema_name: str) -> Iterator[Store]:
    s = Store(database_url, schema_name)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def migrated_store(store: Store) -> Store:
    store.migrate()
    return store


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> str:
    """Throwaway RSA key for the GitHub App JWT tests (never persisted)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
