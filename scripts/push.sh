#!/usr/bin/env bash
# Push to GitHub only if no commit reachable from HEAD contains secrets or private host details.
#   scripts/push.sh [remote] [branch]
# Private patterns (server IP, domains, host paths) go one per line into .push-denylist (git-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."
REMOTE="${1:-origin}"; BRANCH="${2:-main}"
fail() { echo "push refused: $*" >&2; exit 1; }

if ! git diff --quiet || ! git diff --cached --quiet; then fail "uncommitted changes"; fi
if git ls-files | grep -qE '(^|/)\.env$|\.pem$|\.key$|(^|/)config/projects\.toml$|(^|/)\.push-denylist$'; then
  fail "a local-only file is tracked"
fi
if git rev-list HEAD | xargs git grep -lE 'BEGIN [A-Z ]*PRIVATE KEY' -- 2>/dev/null | grep -q .; then
  fail "private key material in history"
fi
if [[ -f .push-denylist ]]; then
  while IFS= read -r pat; do
    [[ -z "$pat" || "$pat" == \#* ]] && continue
    if git log -p --all -F -S "$pat" --format=%h | grep -q .; then fail "denylisted text found in history: ${pat:0:3}…"; fi
  done < .push-denylist
fi
git push "$REMOTE" "$BRANCH"
