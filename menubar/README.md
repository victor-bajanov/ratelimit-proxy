# ProxyRates menu-bar app

A small macOS menu-bar app that polls the proxy's `/__proxy/rates` endpoint
and plots the numbers it returns. It only reads that endpoint; it never talks
to `api.anthropic.com` and never changes the proxy.

## What it shows

The menu bar shows a sparkline of one metric over the last 5 minutes, followed
by its latest value (credits/s by default). The label keeps a fixed minimum
width, so it doesn't jump around as the number changes. Before the first poll
it shows a dashed line and `–`. When the proxy can't be reached the line
fades and the value reads `off`.

Click it to open a panel with five charts:

* Requests/s: started and finished
* In flight: a small chart of its own, because a count of around 9 would
  flatten rates of about 1/s
* Tokens/s: input, output and cache write
* Cache read tokens/s
* Credits/s

Each chart shows its latest values next to the title. A failed poll leaves a
gap in the lines. Lines also break where two points are more than three poll
intervals apart (at least 10 s), using the interval in force when the points
were taken. So time the proxy was down shows up as a break, not as a line
drawn across it. While the proxy is down the panel header says since when,
the error, and the endpoint it is trying. The "since" time stays correct even
when the outage lasts longer than the 1 h of history.

## Requirements

* macOS 14 or later, Apple silicon (the build targets `arm64-apple-macos14.0`)
* Xcode Command Line Tools (`xcode-select --install`). Xcode and SwiftPM are
  not needed; the scripts call `swiftc` directly.

The scripts pass `-sdk /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk`
because the default SDK (MacOSX27.0) fails to import Swift with the Command
Line Tools toolchain. If your Command Line Tools ship a different SDK, change
the `SDK=` line in `build.sh` and `test.sh`.

## Build and run

Run the `menubar/...` commands in this README from the repo root.

```sh
menubar/build.sh
open menubar/build/ProxyRates.app
```

`build.sh` compiles everything into one binary, assembles
`menubar/build/ProxyRates.app` and signs it ad hoc. The app has no Dock icon;
quit it from the Quit button in its panel.

To keep it around, copy the app to `~/Applications` and add it under System
Settings > General > Login Items:

```sh
cp -R menubar/build/ProxyRates.app ~/Applications/
```

## Tests

```sh
menubar/test.sh           # Core logic tests
menubar/check_bundle.sh   # checks the built app; run build.sh first
```

`test.sh` builds and runs the tests for the Foundation-only Core code
(parsing, history, formatting, sparkline geometry, settings) and fails if Core
imports SwiftUI or AppKit. `check_bundle.sh` checks the bundle layout,
`Info.plist`, the signature, renders the menu-bar label for the live,
offline and waiting states, and launches the app for 3 seconds against an
unused port, so it never touches the proxy on :8787 or your saved settings.

## Settings

In the panel:

* Range: 5m, 15m or 1h for the charts
* Menu bar: which metric the menu-bar sparkline and value show

Under the gear button:

* Poll every: 1, 2, 5 (default), 10 or 30 seconds
* Endpoint: defaults to `http://127.0.0.1:8787/__proxy/rates`. Any http or
  https URL is accepted. The app's `Info.plist` sets
  `NSAllowsLocalNetworking`, which is meant to allow plain http to local hosts
  such as 127.0.0.1 and localhost; use https for any remote endpoint. If App
  Transport Security blocks a request, the panel shows "Plain http blocked
  (use https)". Changing the endpoint clears the history.
* Reset to defaults

Settings live in the UserDefaults domain `local.ratelimit-proxy.menubar`.
Reset to defaults in the panel restores the defaults. To remove the stored
settings entirely, quit the app and run:

```sh
defaults delete local.ratelimit-proxy.menubar
```

## About the numbers

* Each poll returns the proxy's trailing 60-second window, so every point is
  a 60 s average, not an instant reading.
* Tokens count when a request finishes, because that is when the usage
  arrives, so the token and credit series are spiky: a long stream adds all
  its output at once.
* Cache read has its own chart because it runs about 100x the other token
  kinds and would flatten them.
* History is kept in memory only, for 1 hour. Quitting the app loses it.
