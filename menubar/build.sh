#!/usr/bin/env bash
# Compiles Core + App with plain swiftc and assembles an ad-hoc signed
# build/ProxyRates.app. CORE_SRC overrides the Core source directory.
set -euo pipefail
cd "$(dirname "$0")"

CORE_SRC="${CORE_SRC:-Sources/Core}"
# The default SDK (MacOSX27.0) fails to import Swift with the CLT toolchain.
SDK=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
FLAGS=(-sdk "$SDK" -target arm64-apple-macos14.0 -swift-version 6)

mkdir -p build
swiftc "${FLAGS[@]}" -parse-as-library -O -module-name ProxyRates -o build/ProxyRates \
    "$CORE_SRC"/*.swift Sources/App/*.swift

APP=build/ProxyRates.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp build/ProxyRates "$APP/Contents/MacOS/ProxyRates"
cp Info.plist "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
echo "$PWD/$APP"
