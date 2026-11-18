#!/bin/sh
# The "run before saying done" gate: exactly what CI would run (see
# docs/ci.md): lint, then the full test suite including the contract tests.
#
# Uses ./.venv if there is one (override with PYTHON=/path/to/python).
# Zuul must be installed in that environment; the contract tests fail,
# not skip, if it isn't.
set -eu

cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-.venv/bin/python}

if ! "$PYTHON" -m ruff --version >/dev/null 2>&1; then
    echo "ruff is not installed for $PYTHON; run:" >&2
    echo "  uv pip install -p $PYTHON -e '.[test,lint]'" >&2
    exit 2
fi

echo "== lint"
"$PYTHON" -m ruff check .

echo "== tests (including contract tests)"
"$PYTHON" -m pytest
