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

# --- network-failure modes (staging VPS cannot always reach github.com) ---
export DEPLOY_FETCH_RETRY_DELAY=0

# A sha that is already local must resolve WITHOUT fetching: point origin at
# a path that does not exist, so any fetch would fail.
git remote set-url origin "$tmp/no-such-origin.git"
if DEPLOY_REF="$dev2_sha" "$RESOLVE" > /dev/null 2> "$tmp/err"; then
    expect_head "DEPLOY_REF=<local sha> with origin unreachable (fetch skipped)" "$dev2_sha"
    grep -q "skipping fetch" "$tmp/err" || { echo "FAIL: skip-fetch message missing" >&2; fail=1; }
else
    echo "FAIL: local sha resolution required the network" >&2
    fail=1
fi

# A branch ref with origin unreachable retries and then fails closed.
before="$(git rev-parse HEAD)"
if DEPLOY_REF="dev" "$RESOLVE" > /dev/null 2> "$tmp/err"; then
    echo "FAIL: branch ref resolved without a reachable origin" >&2
    fail=1
elif [ "$(git rev-parse HEAD)" != "$before" ]; then
    echo "FAIL: failed fetch moved HEAD (not fail closed)" >&2
    fail=1
elif [ "$(grep -c "retrying in" "$tmp/err")" -ne 2 ] || ! grep -q "failed after 3 attempt" "$tmp/err"; then
    echo "FAIL: expected 3 fetch attempts, got:" >&2
    cat "$tmp/err" >&2
    fail=1
else
    echo "PASS: unreachable origin -> 3 fetch attempts, fails closed, HEAD unchanged"
fi
git remote set-url origin "$tmp/origin.git"

# Transient failure: a git wrapper fails the first two fetches (simulating the
# bad anycast IP), the third succeeds -> the deploy must still land.
real_git="$(command -v git)"
mkdir -p "$tmp/bin"
cat > "$tmp/bin/git" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "fetch" ]; then
    n=\$(cat "$tmp/fetch_count" 2>/dev/null || echo 0)
    n=\$((n + 1)); echo "\$n" > "$tmp/fetch_count"
    if [ "\$n" -le 2 ]; then
        echo "fatal: unable to access 'origin': Failed to connect (simulated)" >&2
        exit 128
    fi
fi
exec "$real_git" "\$@"
EOF
chmod +x "$tmp/bin/git"
cd "$tmp/seed"
git checkout -q dev
echo dev3 > f && git commit -qam dev3
git push -q origin dev
dev3_sha="$(git rev-parse HEAD)"
cd "$tmp/work"
rm -f "$tmp/fetch_count"
if PATH="$tmp/bin:$PATH" DEPLOY_REF="dev" "$RESOLVE" > /dev/null 2> "$tmp/err"; then
    expect_head "DEPLOY_REF=dev with 2 transient fetch failures (retry)" "$dev3_sha"
    [ "$(cat "$tmp/fetch_count")" -eq 3 ] || { echo "FAIL: expected 3 fetch calls, got $(cat "$tmp/fetch_count")" >&2; fail=1; }
else
    echo "FAIL: transient fetch failures were not retried:" >&2
    cat "$tmp/err" >&2
    fail=1
fi

[ "$fail" -eq 0 ] || exit 1
echo "OK: all deploy-ref resolution tests passed"
