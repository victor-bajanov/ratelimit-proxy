#!/usr/bin/env bash
# Checks the built ProxyRates.app: layout, Info.plist, signature, label
# rendering and a smoke launch. Run menubar/build.sh first.
set -euo pipefail
cd "$(dirname "$0")"

APP=build/ProxyRates.app
BIN=$APP/Contents/MacOS/ProxyRates
PLIST=$APP/Contents/Info.plist

ok() { echo "ok - $*"; }
fail() { echo "not ok - $*"; exit 1; }

# 1. Executable: present, arm64, deployment target 14.0
[[ -x $BIN ]] || fail "executable $BIN exists"
ok "executable exists"
archs=$(lipo -archs "$BIN")
[[ $archs == arm64 ]] || fail "lipo -archs is arm64 (got: $archs)"
ok "arch arm64"
otool -l "$BIN" | grep -qE '^ *minos 14\.0$' || fail "otool -l shows minos 14.0"
ok "minos 14.0"

# 2. Info.plist
plutil -lint "$PLIST" >/dev/null || fail "plutil -lint $PLIST"
ok "plist lints"
pb() { /usr/libexec/PlistBuddy -c "Print :$1" "$PLIST" 2>/dev/null || true; }
expect_key() {
    local got
    got=$(pb "$1")
    [[ $got == "$2" ]] || fail "$1 is '$2' (got: '$got')"
    ok "$1 = $2"
}
expect_key CFBundleExecutable ProxyRates
expect_key CFBundleIdentifier local.ratelimit-proxy.menubar
expect_key CFBundlePackageType APPL
expect_key LSMinimumSystemVersion 14.0
expect_key LSUIElement true
expect_key NSAppTransportSecurity:NSAllowsLocalNetworking true

# 3. Signature
codesign --verify "$APP" 2>/dev/null || fail "codesign --verify $APP"
ok "codesign verifies"

# 4. Label render hook: three PNGs, 16 pt tall at 2x
TMP=$(mktemp -d "${TMPDIR:-/tmp}/proxyrates-check.XXXXXX")
PID=""
cleanup() {
    if [[ -n $PID ]]; then kill "$PID" 2>/dev/null || true; fi
    rm -rf "$TMP"
}
trap cleanup EXIT
"$BIN" --render-label "$TMP" >/dev/null 2>&1 &
PID=$!
for _ in $(seq 1 100); do kill -0 "$PID" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$PID" 2>/dev/null; then fail "--render-label exits within 10 s"; fi
rc=0; wait "$PID" || rc=$?
PID=""
(( rc == 0 )) || fail "--render-label exits 0 (got: $rc)"
ok "--render-label exits 0"
widths=()
for state in live offline waiting; do
    f=$TMP/label-$state.png
    [[ -f $f ]] || fail "$f exists"
    h=$(sips -g pixelHeight "$f" | awk '/pixelHeight/ {print $2}')
    w=$(sips -g pixelWidth "$f" | awk '/pixelWidth/ {print $2}')
    [[ $h == 32 ]] || fail "label-$state.png height 32 (got: $h)"
    (( w >= 80 )) || fail "label-$state.png width >= 80 (got: $w)"
    ok "label-$state.png ${w}x${h}"
    widths+=("$w")
done
# The status item must not shift as the value changes: all states equally wide.
[[ ${widths[0]} == "${widths[1]}" && ${widths[1]} == "${widths[2]}" ]] \
    || fail "label widths equal (got: ${widths[*]})"
ok "label widths equal (${widths[0]})"

# 5. Smoke launch against the discard port; the argument domain overrides the
#    stored settings, so this never touches :8787 or the user's defaults, and
#    -loginItemInitialised stops it registering build/ as a login item.
"$BIN" -endpointURL http://127.0.0.1:9/__proxy/rates -pollIntervalSeconds 1 -loginItemInitialised YES >/dev/null 2>&1 &
PID=$!
sleep 3
kill -0 "$PID" 2>/dev/null || fail "app still running after 3 s"
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
PID=""
ok "smoke launch survives 3 s"

echo "all ok"
