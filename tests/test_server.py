from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from prometheus_client import CollectorRegistry

from github_actions_ingester.metrics import Metrics
from github_actions_ingester.server import MetricsServer


@pytest.fixture
def server() -> Iterator[tuple[MetricsServer, Metrics, list[bool]]]:
    metrics = Metrics(CollectorRegistry())
    metrics.build_info.info({"version": "test"})
    ready = [False]
    srv = MetricsServer("127.0.0.1", 0, metrics.registry, lambda: ready[0])
    srv.start()
    try:
        yield srv, metrics, ready
    finally:
        srv.stop()


def test_endpoints(server: tuple[MetricsServer, Metrics, list[bool]]) -> None:
    srv, metrics, ready = server
    base = f"http://127.0.0.1:{srv.port}"
    with httpx.Client(base_url=base) as c:
        assert c.get("/healthz").text == "ok\n"
        r = c.get("/readyz")
        assert r.status_code == 503
        ready[0] = True
        assert c.get("/readyz").status_code == 200
        metrics.up.set(1)
        body = c.get("/metrics").text
        assert "gha_ingester_up 1.0" in body
        assert 'gha_ingester_build_info{version="test"} 1.0' in body
        assert "process_cpu_seconds_total" in body  # default collectors registered
        index = c.get("/")
        assert index.status_code == 200 and "/metrics" in index.text
        assert c.get("/nope").status_code == 404
        assert c.get("/metrics?x=1").status_code == 200  # query string ignored
