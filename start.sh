#!/usr/bin/env bash
# Install dependencies, build the desk, seed the shop if needed, and serve it.
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3.12 >/dev/null 2>&1; then
  PYTHON=python3.12
else
  PYTHON=python3
fi

if [[ ! -d backend/.venv ]]; then
  "$PYTHON" -m venv backend/.venv
fi
backend/.venv/bin/pip install -e backend

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

(cd frontend && npm ci && npm run build)

exec backend/.venv/bin/python -m uvicorn app.serve:create_runtime_app --factory --host 127.0.0.1 --port "${PORT:-8000}"
