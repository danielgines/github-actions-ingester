"""GitHub Actions ingester.

Pulls repositories, workflows, workflow runs and jobs from the GitHub REST
API into PostgreSQL on a fixed interval, keeps the schema up to date on
its own, and exposes Prometheus metrics about the ingestion itself and
about the liveness of scheduled workflows.
"""

__version__ = "0.2.0"
