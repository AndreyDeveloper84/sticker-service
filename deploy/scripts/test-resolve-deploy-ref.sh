#!/usr/bin/env bash
# Regression test for resolve-deploy-ref.sh. Builds a throwaway fixture
# (bare "origin" + working clone) and covers all DEPLOY_REF modes:
#   unset -> latest origin/main; <branch> -> latest origin/<branch>;
#   <sha> -> exact commit; unknown ref -> fail closed (HEAD unchanged).
# Run: bash deploy/scripts/test-resolve-deploy-ref.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVE="$SCRIPT_DIR/resolve-deploy-ref.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

git init -q --bare "$tmp/origin.git"
git init -q "$tmp/seed"
cd "$tmp/seed"
git config user.email test@example.com
git config user.name test
git config commit.gpgsign false
echo main1 > f && git add f && git commit -qm main1
git branch -M main
git remote add origin "$tmp/origin.git"
git push -q origin main
git checkout -qb dev
echo dev1 > f && git commit -qam dev1
git push -q origin dev
dev1_sha="$(git rev-parse HEAD)"

# what /opt/sticker-service looks like on the server
git clone -q "$tmp/origin.git" "$tmp/work"

# advance origin AFTER the clone, so resolution must really fetch
cd "$tmp/seed"
git checkout -q main
echo main2 > f && git commit -qam main2
git push -q origin main
main2_sha="$(git rev-parse HEAD)"
git checkout -q dev
echo dev2 > f && git commit -qam dev2
git push -q origin dev
dev2_sha="$(git rev-parse HEAD)"

cd "$tmp/work"

fail=0
expect_head() {
    local name="$1" want="$2" got
    got="$(git rev-parse HEAD)"
    if [ "$got" = "$want" ]; then
        echo "PASS: ${name} -> ${want}"
    else
        echo "FAIL: ${name} -> got ${got}, want ${want}" >&2
        fail=1
    fi
}

DEPLOY_REF="" "$RESOLVE" > /dev/null
expect_head "DEPLOY_REF unset (latest origin/main)" "$main2_sha"

DEPLOY_REF="dev" "$RESOLVE" > /dev/null
expect_head "DEPLOY_REF=dev (latest origin/dev)" "$dev2_sha"

DEPLOY_REF="$dev1_sha" "$RESOLVE" > /dev/null
expect_head "DEPLOY_REF=<sha> (exact commit)" "$dev1_sha"

before="$(git rev-parse HEAD)"
if DEPLOY_REF="no-such-ref" "$RESOLVE" > /dev/null 2>&1; then
    echo "FAIL: unknown ref was accepted" >&2
    fail=1
elif [ "$(git rev-parse HEAD)" != "$before" ]; then
    echo "FAIL: unknown ref moved HEAD (not fail closed)" >&2
    fail=1
else
    echo "PASS: unknown ref fails closed, HEAD unchanged"
fi

[ "$fail" -eq 0 ] || exit 1
echo "OK: all deploy-ref resolution tests passed"
