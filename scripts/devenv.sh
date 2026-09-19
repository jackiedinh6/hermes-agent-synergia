#!/usr/bin/env bash
# S0 — create the host dev environment for the hermes-agent-synergia test suite.
#
# Creates $REPO/.venv-team and installs the pinned test deps. Idempotent:
# re-running refreshes the venv in place.
#
# --system-site-packages is REQUIRED: the substrate's runtime deps (PyYAML et
# al.) are already importable from the host interpreter and are deliberately
# not re-installed into this venv. The pytest pins mirror the substrate's own
# dev extra ($HA/pyproject.toml, [project.optional-dependencies] dev):
#   pytest==9.1.1, pytest-asyncio==1.3.0
#
# Why on the host at all (plan F4): the container has no pip and no pytest,
# and hermes_cli is not importable by its system python; the host imports the
# whole pinned substrate cleanly.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv-team"

python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --upgrade "pytest==9.1.1" "pytest-asyncio==1.3.0"
# S1 exception to the "deps come from the host" rule: hermes_cli.main imports
# hermes_cli.env_loader -> dotenv at module scope, and python-dotenv is the one
# substrate runtime dep the host interpreter lacks. Pure-python, no cascade.
"$VENV/bin/python" -m pip install --upgrade "python-dotenv==1.2.2"
# The dashboard half (S19) imports hermes_cli.web_server, which raises SystemExit
# without fastapi/uvicorn, and tests/test_dashboard_api.py drives it through
# starlette's TestClient (httpx). Pins mirror the substrate's [web] extra
# ($HA/pyproject.toml); the host interpreter ships none of them.
"$VENV/bin/python" -m pip install --upgrade \
    "fastapi==0.133.1" "uvicorn[standard]==0.41.0" "starlette==1.3.1" \
    "python-multipart==0.0.32" "httpx==0.28.1"

echo "dev env ready: $VENV"
"$VENV/bin/python" -m pytest --version
