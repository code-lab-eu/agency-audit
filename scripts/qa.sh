#!/usr/bin/env bash
# qa.sh — merge-gate quality checks for agency-audit
# Run via: uv run --extra dev scripts/qa.sh
set -euo pipefail

echo "==> ruff lint"
uvx ruff check src/ tests/

echo "==> ruff format check"
uvx ruff format --check src/ tests/

echo "==> mypy"
uvx --from mypy mypy src/ --disable-error-code no-any-return

echo "==> pytest + coverage"
uv run --extra dev pytest \
  --cov=src/agency_audit \
  --cov-report=term-missing \
  --no-header \
  tests/

echo "==> Coverage threshold (≥80%)"
uv run --extra dev coverage report --fail-under=80

echo "==> All checks passed"
