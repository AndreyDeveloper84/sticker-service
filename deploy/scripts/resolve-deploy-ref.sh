#!/usr/bin/env bash
# Resolve DEPLOY_REF to an exact commit and check it out (detached HEAD).
# Must run from the repository checkout on the deploy server.
#
# DEPLOY_REF modes:
#   unset    -> latest origin/main (fast-forward pull on main)
#   <branch> -> latest origin/<branch> (fetched explicitly, resolved via
#               refs/remotes/origin/<branch> -- never DWIM branch creation,
#               which conflicts with --detach)
#   <sha>    -> that exact commit, must exist after fetch (rollback)
#
# Unknown refs fail closed: non-zero exit, no checkout.
# Messages go to stderr; the resolved commit sha is printed on stdout.
set -euo pipefail

DEPLOY_REF="${DEPLOY_REF:-}"

if [ -z "$DEPLOY_REF" ]; then
    echo "[deploy] fetching latest main" >&2
    git fetch origin main
    git checkout main
    git pull --ff-only origin main
    git rev-parse HEAD
    exit 0
fi

echo "[deploy] resolving ref ${DEPLOY_REF}" >&2
git fetch origin

if git show-ref --verify --quiet "refs/remotes/origin/${DEPLOY_REF}"; then
    target="$(git rev-parse "refs/remotes/origin/${DEPLOY_REF}^{commit}")"
elif git rev-parse --verify --quiet "${DEPLOY_REF}^{commit}" > /dev/null; then
    target="$(git rev-parse "${DEPLOY_REF}^{commit}")"
else
    echo "[deploy] ERROR: unknown ref ${DEPLOY_REF} (not a remote branch, not a known commit)" >&2
    exit 1
fi

git checkout --detach "$target"
echo "[deploy] deploying commit ${target}" >&2
echo "$target"
