#!/usr/bin/env bash
#
# qa.sh — run the full local QA gate for agency-audit.
#
# Mirrors the CI `quality` job (ruff lint, ruff format check, mypy) plus the
# local test suite, so a green run here means CI should pass too. This is the
# single source of truth for "is my branch ready to push": AGENTS.md, agents,
# and a future pre-commit hook should all call this script rather than listing
# the individual commands.
#
# Usage:
#   scripts/qa.sh          Run every check; exit non-zero if any fail.
#   scripts/qa.sh --fix    Apply ruff auto-fixes + formatting first, then check.
#   scripts/qa.sh --help   Show this help.
#
# All checks run even when an earlier one fails, so a single run surfaces every
# problem. Add new gates to the CHECKS section below.

set -uo pipefail

# Run from the repo root regardless of where the script is invoked from.
cd "$(dirname "$0")/.." || exit 1

FIX=0
for arg in "$@"; do
  case "$arg" in
    --fix) FIX=1 ;;
    -h | --help)
      sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "qa.sh: unknown argument: $arg" >&2
      echo "Try 'scripts/qa.sh --help'." >&2
      exit 2
      ;;
  esac
done

# --- Colors (disabled when not a TTY) ---------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; RED=""; DIM=""; RESET=""
fi

FAILED=()

# run <label> <command...> — print a header, run the command, record failures.
run() {
  local label="$1"; shift
  printf '\n%s==> %s%s\n%s$ %s%s\n' "$BOLD" "$label" "$RESET" "$DIM" "$*" "$RESET"
  if "$@"; then
    printf '%s    PASS%s  %s\n' "$GREEN" "$RESET" "$label"
  else
    printf '%s    FAIL%s  %s\n' "$RED" "$RESET" "$label"
    FAILED+=("$label")
  fi
}

# Tools run through the project's dev env (`uv run --extra dev`) so they execute
# under the project's Python interpreter (3.14), exactly like CI.
#
# Do NOT use `uvx --from mypy mypy` here. mypy parses source with the AST of the
# interpreter it runs under and has no backport parser for new syntax, so under
# uvx's default (older) Python it cannot parse PEP 695 type-parameter syntax
# (`def retry[T](...)`, src/agency_audit/loop/retry.py) and reports a false
# `Expected '('` syntax error. The mypy version is irrelevant — the same mypy
# 2.1.0 passes under 3.14. See https://github.com/python/mypy/issues/15238.

# --- Optional auto-fix pass -------------------------------------------------
if [ "$FIX" -eq 1 ]; then
  printf '%s==> Applying auto-fixes (ruff check --fix, ruff format)%s\n' "$BOLD" "$RESET"
  uv run --extra dev ruff check --fix src/ tests/
  uv run --extra dev ruff format src/ tests/
fi

# --- CHECKS -----------------------------------------------------------------
# The test run enables coverage so the `fail_under` gate in pyproject.toml is
# enforced here exactly as in CI. (A bare `pytest` for single-test dev iteration
# deliberately skips coverage — the gate lives in this script and in CI.)
run "Lint (ruff check)"          uv run --extra dev ruff check src/ tests/
run "Format check (ruff format)" uv run --extra dev ruff format --check src/ tests/
run "No new DB mocks in tests"   uv run python scripts/check_test_db_mocks.py
run "Type check (mypy)"          uv run --extra dev mypy src/
run "Tests + coverage (pytest)"  uv run --extra dev pytest --cov=src/agency_audit --cov-report=term-missing

# --- Summary ----------------------------------------------------------------
echo
if [ "${#FAILED[@]}" -eq 0 ]; then
  printf '%s%sAll QA checks passed.%s\n' "$BOLD" "$GREEN" "$RESET"
  exit 0
else
  printf '%s%s%d check(s) failed:%s\n' "$BOLD" "$RED" "${#FAILED[@]}" "$RESET"
  for label in "${FAILED[@]}"; do
    printf '  %s- %s%s\n' "$RED" "$label" "$RESET"
  done
  exit 1
fi
