#!/usr/bin/env bash
set -euo pipefail

# Create venv if missing
VENV_DIR=".venv"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

# Activate and install dev requirements
# shellcheck source=/dev/null
. "$VENV_DIR/bin/activate"
# Run pip in a minimal env to avoid inherited proxy settings
env -i PATH="$PATH" python3 -m pip install --upgrade pip
env -i PATH="$PATH" python3 -m pip install -r requirements-dev.txt

# Run tests
pytest -q

deactivate || true
