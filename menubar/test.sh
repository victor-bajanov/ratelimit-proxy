#!/usr/bin/env bash
# Build and run the Core test harness (Foundation only, plain swiftc).
set -euo pipefail
cd "$(dirname "$0")"

if grep -lE '^import (SwiftUI|AppKit|Charts|Cocoa)' Sources/Core/*.swift; then
    echo "Core must import Foundation only (see files above)" >&2
    exit 1
fi

SDK=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
FLAGS=(-sdk "$SDK" -target arm64-apple-macos14.0 -swift-version 6)

mkdir -p build
swiftc "${FLAGS[@]}" -module-name ProxyRatesTests -o build/core-tests \
    Sources/Core/*.swift Tests/*.swift

set +e
build/core-tests
status=$?
set -e
exit "$status"
