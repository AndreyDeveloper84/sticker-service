#!/usr/bin/env bash
# Resolve DEPLOY_REF to an exact commit and check it out (detached HEAD).
# Must run from the repository checkout on the deploy server.
#
# DEPLOY_REF modes:
#   unset    -> latest origin/main (fast-forward pull on main)
#   <branch> -> latest origin/<branch> (fetched explicitly, resolved via
#               refs/remotes/origin/<branch> -- never DWIM branch creation,
#               which conflicts with --detach)
#   <sha>    -> that exact commit, must exist after fetch (rollback).
#               A sha that is already present locally is checked out
#               WITHOUT fetching: a rollback must not depend on GitHub
#               being reachable.
#
# Network: github.com resolves to several anycast IPs and one of them is
# unreachable from the staging VPS (connect timeout after ~130 s), so a
# single `git fetch` fails on a bad DNS answer. Every fetch is therefore
# retried up to DEPLOY_FETCH_ATTEMPTS times (default 3) with a
# DEPLOY_FETCH_RETRY_DELAY-second pause (default 15); each attempt is a new
# git process and re-resolves DNS, so a retry usually lands on a good IP.
#
# Unknown refs fail closed: non-zero exit, no checkout.
# Messages go to stderr; the resolved commit sha is printed on stdout.
set -euo pipefail

DEPLOY_REF="${DEPLOY_REF:-}"
DEPLOY_FETCH_ATTEMPTS="${DEPLOY_FETCH_ATTEMPTS:-3}"
DEPLOY_FETCH_RETRY_DELAY="${DEPLOY_FETCH_RETRY_DELAY:-15}"

# fetch_with_retry <git fetch args...>
fetch_with_retry() {
    local attempt=1
    while true; do
        if git fetch "$@"; then
            return 0
        fi
        if [ "$attempt" -ge "$DEPLOY_FETCH_ATTEMPTS" ]; then
            echo "[deploy] ERROR: git fetch $* failed after ${attempt} attempt(s)" >&2
            return 1
        fi
        echo "[deploy] git fetch $* failed (attempt ${attempt}/${DEPLOY_FETCH_ATTEMPTS}), retrying in ${DEPLOY_FETCH_RETRY_DELAY}s" >&2
        sleep "$DEPLOY_FETCH_RETRY_DELAY"
        attempt=$((attempt + 1))
    done
}

# local_sha_available <ref>: true when <ref> looks like a commit sha (7-40
# hex chars), is not also the name of a branch, and resolves locally.
local_sha_available() {
    local ref="$1"
    case "$ref" in
        *[!0-9a-f]*) return 1 ;;
    esac
    [ "${#ref}" -ge 7 ] && [ "${#ref}" -le 40 ] || return 1
    if git show-ref --verify --quiet "refs/heads/${ref}" \
        || git show-ref --verify --quiet "refs/remotes/origin/${ref}"; then
        return 1
    fi
    git rev-parse --verify --quiet "${ref}^{commit}" > /dev/null
}

if [ -z "$DEPLOY_REF" ]; then
    echo "[deploy] fetching latest main" >&2
    fetch_with_retry origin main
    git checkout main
    git merge --ff-only origin/main
    git rev-parse HEAD
    exit 0
fi

echo "[deploy] resolving ref ${DEPLOY_REF}" >&2
if local_sha_available "$DEPLOY_REF"; then
    echo "[deploy] commit ${DEPLOY_REF} already present locally, skipping fetch" >&2
else
    fetch_with_retry origin
fi

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
