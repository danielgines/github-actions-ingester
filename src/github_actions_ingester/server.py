"""HTTP server — Prometheus /metrics + health endpoints.

stdlib ``http.server`` only: the ingester serves one scraper and a couple
of probes, nothing that justifies an async stack.

  /metrics   Prometheus exposition
  /healthz   liveness — 200 while the HTTP server answers
  /readyz    readiness — 200 once the schema is bootstrapped and the first
             cycle finished; 503 before that so a rollout waits for the
             database bootstrap instead of declaring victory early
  /          index
"""

from __future__ import annotations

import http.server
import threading
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

logger = structlog.get_logger(__name__)

_INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head><title>github-actions-ingester</title></head>
<body>
  <h1>github-actions-ingester</h1>
  <p>Ingests GitHub Actions workflow runs and jobs into PostgreSQL and
    exposes ingester health as Prometheus metrics.</p>
  <ul>
    <li><a href="/metrics">/metrics</a> — Prometheus exposition
      (<code>gha_ingester_*</code>, <code>gha_scheduled_workflow_*</code>)</li>
    <li><a href="/healthz">/healthz</a> — liveness</li>
    <li><a href="/readyz">/readyz</a> — readiness (503 until the schema is
      bootstrapped and the first cycle completed)</li>
  </ul>
  <p>The analytical data (minutes, success rate, queue wait) is in the
    database; query it with the Grafana PostgreSQL datasource.</p>
  <p><a href="https://github.com/danielgines/github-actions-ingester">github.com/danielgines/github-actions-ingester</a></p>
</body>
</html>
"""


def make_handler(
    registry: CollectorRegistry, is_ready: Callable[[], bool]
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/metrics":
                self._send(200, generate_latest(registry), CONTENT_TYPE_LATEST)
                return
            if path == "/healthz":
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
                return
            if path == "/readyz":
                if is_ready():
                    self._send(200, b"ready\n", "text/plain; charset=utf-8")
                else:
                    self._send(503, b"not ready\n", "text/plain; charset=utf-8")
                return
            if path in ("/", "/index.html"):
                self._send(200, _INDEX_HTML.encode(), "text/html; charset=utf-8")
                return
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

    return Handler


class MetricsServer:
    """Threaded HTTP server with a controllable lifetime."""

    def __init__(
        self, host: str, port: int, registry: CollectorRegistry, is_ready: Callable[[], bool]
    ) -> None:
        self._httpd = http.server.ThreadingHTTPServer(
            (host, port), make_handler(registry, is_ready)
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="github-actions-ingester-http", daemon=True
        )
        self._host = host
        self._port = port

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        self._thread.start()
        logger.info("http.listening", host=self._host, port=self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        logger.info("http.stopped")
