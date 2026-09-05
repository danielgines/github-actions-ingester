#!/usr/bin/env python3
"""Single source of truth for the version.

``pyproject.toml`` ``[project].version`` is canonical. This script copies
it into the two files that must carry the same literal:

  - ``src/github_actions_ingester/__init__.py`` (``__version__``)
  - ``helm/github-actions-ingester/Chart.yaml`` (``version``,
    ``appVersion`` and the ``artifacthub.io/images`` tag)

It runs as a pre-commit hook (a rewrite makes the commit fail so the
change is staged explicitly) and from ``just release VERSION``.

Exit codes: 0 nothing changed, 1 files rewritten, 2 parse failure.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "github_actions_ingester" / "__init__.py"
CHART_YAML = REPO_ROOT / "helm" / "github-actions-ingester" / "Chart.yaml"


def read_canonical_version() -> str:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        print("ERR: pyproject.toml has no [project].version", file=sys.stderr)
        raise SystemExit(2)
    return version


def replace_in_file(path: Path, pattern: re.Pattern[str], replacement: str) -> bool:
    original = path.read_text()
    new = pattern.sub(replacement, original, count=1)
    if new != original:
        path.write_text(new)
        return True
    return False


def sync_init_py(version: str) -> bool:
    return replace_in_file(
        INIT_PY,
        re.compile(r'^(__version__\s*=\s*)"[^"]*"', re.MULTILINE),
        rf'\g<1>"{version}"',
    )


def sync_chart_yaml(version: str) -> bool:
    changed_version = replace_in_file(
        CHART_YAML,
        re.compile(r"^(version:\s+).*$", re.MULTILINE),
        rf"\g<1>{version}",
    )
    changed_app = replace_in_file(
        CHART_YAML,
        re.compile(r"^(appVersion:\s+).*$", re.MULTILINE),
        rf'\g<1>"{version}"',
    )
    changed_image = replace_in_file(
        CHART_YAML,
        re.compile(r"(image:\s+ghcr\.io/[^\s:]+/github-actions-ingester:)[^\s]+", re.MULTILINE),
        rf"\g<1>{version}",
    )
    return changed_version or changed_app or changed_image


def main() -> int:
    canonical = read_canonical_version()
    changes: list[str] = []
    if sync_init_py(canonical):
        changes.append(str(INIT_PY.relative_to(REPO_ROOT)))
    if sync_chart_yaml(canonical):
        changes.append(str(CHART_YAML.relative_to(REPO_ROOT)))
    if not changes:
        print(f"version sync: pyproject={canonical} - all sources already aligned")
        return 0
    print(f"version sync: pyproject={canonical} - wrote new value to:")
    for f in changes:
        print(f"  - {f}")
    print("git add the changed files and re-commit.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
