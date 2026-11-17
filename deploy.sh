#!/usr/bin/env bash
# Routine deploy: tests → git push → publish to Azure → live edge-case checks.
#
#   ./deploy.sh              # full deploy
#   ./deploy.sh --no-push    # skip git push (e.g. deploying a commit that's already pushed)
#
# Refuses to deploy uncommitted changes, so what runs in Azure is always a commit on GitHub.
# One-time Azure setup (resources, scale settings, alerts) lives in deploy/provision.sh.
set -euo pipefail
cd "$(dirname "$0")"

URL="https://whatsnext-mcp.azurewebsites.net/mcp"
PUSH=1
[[ "${1:-}" == "--no-push" ]] && PUSH=0

step() { printf '\n== %s\n' "$1"; }

step "Checks"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "✗ Uncommitted changes. Commit first, so the deployed code matches a commit:"
  git status --short
  exit 1
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$BRANCH" == "main" ]] || { echo "✗ On branch '$BRANCH', not main."; exit 1; }
echo "✓ clean tree on main at $(git rev-parse --short HEAD)"

step "Unit tests"
.venv/bin/python -m pytest -q

if [[ "$PUSH" == "1" ]]; then
  step "Push"
  git push
fi

step "Publish to Azure (remote build; usually ~5 min)"
START=$SECONDS
deploy/provision.sh publish
echo "✓ published in $(( (SECONDS - START) / 60 ))m$(( (SECONDS - START) % 60 ))s"

step "Live edge-case checks against $URL"
# The first request after a publish can take a few seconds while the new instance starts.
if .venv/bin/python edge_test.py "$URL"; then
  printf '\n✓ Deployed %s and verified live.\n' "$(git rev-parse --short HEAD)"
else
  printf '\n✗ Deployed, but live checks failed. Look before walking away (App Insights: whatsnext-mcp).\n'
  exit 1
fi
