#!/usr/bin/env bash
#
# open-pr.sh — publish the current branch as a pull request against master.
#
# Merges the latest origin/master into the branch, runs scripts/qa.sh, pushes
# the branch, creates the pull request with `gh pr create` unless the branch
# already has one, then waits until CI registers the checks and watches them.
# `gh pr checks --watch` alone exits with "no checks reported" when it runs
# before GitHub registers the checks (https://github.com/cli/cli/issues/7401).
#
# Usage:
#   scripts/open-pr.sh [gh pr create options]   e.g. --title "..." --body "..."
#   scripts/open-pr.sh --help                   Show this help.
#
# Re-run it after resolving merge conflicts, or to push new commits to a branch
# that has a pull request: it then skips `gh pr create`. Exits non-zero when the
# merge, the QA gate, the push or a CI check fails.

set -euo pipefail

cd "$(dirname "$0")/.."

case "${1:-}" in
  -h | --help)
    sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac

branch=$(git symbolic-ref --short HEAD)
if [[ "$branch" == "master" ]]; then
  echo "open-pr.sh: on master; create a feature branch first." >&2
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "open-pr.sh: uncommitted changes; commit or stash them first." >&2
  exit 1
fi

git fetch origin
if ! git merge --no-edit origin/master; then
  echo "open-pr.sh: merging origin/master failed; resolve the conflicts," \
    "commit, and re-run." >&2
  exit 1
fi

scripts/qa.sh

git push -u origin HEAD

if ! gh pr view --json number >/dev/null 2>&1; then
  gh pr create --base master "$@"
fi
pr=$(gh pr view --json number --jq .number)

# Wait up to 5 minutes for CI to register the checks of the pushed commit.
for _ in $(seq 30); do
  if [[ $(gh pr view "$pr" --json statusCheckRollup --jq '.statusCheckRollup | length') -gt 0 ]]
  then
    break
  fi
  sleep 10
done

gh pr checks "$pr" --watch --interval 20
