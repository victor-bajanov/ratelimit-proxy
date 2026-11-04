# Plan: menu-bar throughput app for /__proxy/rates (4 Nov 2026)

A native macOS menu-bar app that polls `GET /__proxy/rates`, shows a sparkline
plus the current value of one metric in the menu bar, and opens a panel with
Swift Charts time series for every metric. Branch `menubar-throughput-app`.

## Binding rules for every task

- **Never restart, modify or signal the live proxy** (`proxy.py` on :8787).
  Read-only `curl -s http://127.0.0.1:8787/__proxy/rates` is fine. The app
  itself only issues GETs.
- **TDD, no exceptions**: failing test first, watch it fail (a compile error
  for a not-yet-existing symbol counts as the failure), implement, watch it
  pass. Commit nothing yourself unless the orchestrator says so.
- Python, if any, uses PEP 723 + `uv run`. (None planned.)
- No SwiftPM, no Xcode project, no XCTest, no Swift Testing. Plain `swiftc`.

## Toolchain (verified 4 Nov 2026 with scratch compiles)

`swiftc` 6.3.3, Command Line Tools only, machine on macOS 26.7.

- The default SDK (MacOSX27.0) fails on any file:
  `Swift.swiftmodule/arm64e-apple-macos.swiftinterface: error: cannot suppress
  '~Copyable' ...`. **Always** pass `-sdk $SDK` with
  `SDK=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk`.
- SwiftUI `MenuBarExtra(.window)`, Swift Charts (`LineMark` with a `series:`
  per segment), `@Observable`, `@AppStorage`, `NSImage(size:flipped:drawingHandler:)`
  with `isTemplate = true` and `URLSession.data(from:)` in a `Task` all compile
  in Swift 6 language mode against 26.5 with deployment target 14.0, in ~2.5 s.
- `otool -l` on the result shows `minos 14.0` (it reports `sdk 27.0`; harmless).
- An ad-hoc signed bundle with `LSUIElement=true` launched with `open` and ran
  with no Dock icon.

Exact working command lines:

```sh
SDK=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
FLAGS="-sdk $SDK -target arm64-apple-macos14.0 -swift-version 6"

# test harness: Core + Tests in one module; Tests/main.swift is the entry point
swiftc $FLAGS -module-name ProxyRatesTests -o build/core-tests \
  Sources/Core/*.swift Tests/*.swift

# app: Core + App in one module; @main App struct, so -parse-as-library
swiftc $FLAGS -parse-as-library -O -module-name ProxyRates -o build/ProxyRates \
  Sources/Core/*.swift Sources/App/*.swift

codesign --force --sign - build/ProxyRates.app && codesign --verify build/ProxyRates.app
```

Harness gotchas found: `Tests/main.swift` must `import Foundation` for `exit`;
test-file globals need `nonisolated(unsafe) var` under Swift 6. Verified a
deliberately failing `expectEqual` prints `FAIL Tests/X.swift:5 ... expected 5.0
got 4.0` and exits 1; passing exits 0.

Because Core and App (or Core and Tests) are compiled into one module, Core
symbols may stay `internal`. The API below is written without `public`.

## Layout

```
menubar/
  .gitignore            build/                                    (T2)
  Info.plist            static, copied into the bundle            (T2)
  build.sh              compile + assemble + ad-hoc sign          (T2)
  check_bundle.sh       T2's test: bundle layout, plist, smoke    (T2)
  test.sh               build + run the Core harness              (T1)
  README.md                                                       (T3)
  Sources/Core/*.swift  Foundation only                           (T1)
  Sources/App/*.swift   SwiftUI / AppKit / Charts                 (T2)
  Tests/*.swift         harness + tests                           (T1)
```

App name `ProxyRates`, bundle `menubar/build/ProxyRates.app`, identifier
`local.ratelimit-proxy.menubar`.

## Core public API (pinned: T2 codes against exactly this)

All types are value types and `Sendable` unless stated. Files are T1's choice;
suggested: `Metric.swift`, `RatesSnapshot.swift`, `SampleStore.swift`,
`ValueFormat.swift`, `Sparkline.swift`, `Settings.swift`.

```swift
import Foundation

// MARK: Metric
enum MetricGroup: String, CaseIterable, Sendable, Identifiable {
    case requests, tokens, cacheRead, credits
    var id: String { rawValue }
    var title: String      // "Requests", "Tokens/s", "Cache read tokens/s", "Credits/s"
    var metrics: [Metric]  // requests: [.rpsStarted, .rpsFinished, .inFlight]
                           // tokens:   [.tpsInput, .tpsOutput, .tpsCacheWrite]
                           // cacheRead:[.tpsCacheRead]   credits: [.tpsCredits]
}

enum Metric: String, CaseIterable, Sendable, Identifiable {
    case inFlight = "in_flight"
    case rpsStarted = "rps.started", rpsFinished = "rps.finished"
    case tpsInput = "tps.input", tpsOutput = "tps.output"
    case tpsCacheWrite = "tps.cache_write", tpsCacheRead = "tps.cache_read"
    case tpsCredits = "tps.credits"
    // allCases order is the declaration order above.
    static let defaultSparkline: Metric = .tpsCredits
    init(stored: String?)   // rawValue lookup; nil/unknown -> .tpsCredits
    var id: String { rawValue }
    var path: [String]      // rawValue split on "."  e.g. ["rps","started"]
    var name: String        // legend: "In flight","Started","Finished","Input",
                            //  "Output","Cache write","Cache read","Credits"
    var menuTitle: String   // selector: "Requests in flight","Requests started/s",
                            //  "Requests finished/s","Input tokens/s","Output tokens/s",
                            //  "Cache write tokens/s","Cache read tokens/s","Credits/s"
    var unit: String        // "in flight" | "req/s" | "tok/s" (4 token kinds) | "credits/s"
    var menuBarSuffix: String // "" for .inFlight, "/s" for all others
    var group: MetricGroup
}

// MARK: Decoding
enum RatesDecodeError: Error, Equatable { case invalidJSON, notAnObject, noMetrics }

struct RatesSnapshot: Equatable, Sendable {
    var values: [Metric: Double]
    init(values: [Metric: Double])
    subscript(metric: Metric) -> Double? { get }
    /// JSONSerialization-based and tolerant: extra keys ignored; a missing key,
    /// null, string or JSON bool leaves that metric absent. Ints and floats both
    /// accepted. Throws .invalidJSON (unparseable/empty), .notAnObject (top level
    /// not a dictionary), .noMetrics (object but none of the 8 metrics present).
    static func decode(from data: Data) throws -> RatesSnapshot
}

// MARK: Samples
struct Sample: Equatable, Sendable {
    let time: Date
    let snapshot: RatesSnapshot?        // nil = gap marker (failed poll)
    var isGap: Bool { snapshot == nil }
}

enum ConnectionStatus: Equatable, Sendable {
    case waiting                 // no samples yet
    case live(updated: Date)     // last sample is a snapshot; its time
    case unreachable(since: Date)// last sample is a gap; time of the FIRST gap
                                 // in the trailing run of gaps
}

struct ChartPoint: Equatable, Sendable, Identifiable {
    let metric: Metric
    let time: Date
    let value: Double
    let segment: Int             // per metric, starts at 0
    var seriesKey: String { "\(metric.rawValue)#\(segment)" }  // Charts `series:`
    var id: String { "\(seriesKey)@\(time.timeIntervalSinceReferenceDate)" }
}

struct SampleStore: Sendable {
    static let defaultRetention: TimeInterval = 3600
    static let defaultCapacity = 4000
    init(retention: TimeInterval = 3600, capacity: Int = 4000)
    private(set) var samples: [Sample]   // ascending by time
    var status: ConnectionStatus { get }
    /// Value from the last sample if it is a snapshot containing the metric, else nil.
    func latestValue(_ metric: Metric) -> Double?
    mutating func record(_ snapshot: RatesSnapshot, at time: Date)
    mutating func recordGap(at time: Date)
    mutating func removeAll()
    /// Points for each metric in `metrics` order, samples with time >= since.
    /// Segment rules per metric: a gap or a sample lacking the metric closes the
    /// open segment (next point starts segment+1; consecutive gaps bump once);
    /// a time step > breakAfter between consecutive emitted points also starts
    /// a new segment (covers sleep/wake with no gap markers).
    func points(for metrics: [Metric], since: Date, breakAfter: TimeInterval) -> [ChartPoint]
}
```

`record`/`recordGap` semantics: if `time` is earlier than the last sample's
time (wall clock moved back), clear all samples first. Append. Then drop every
sample with `sample.time < time - retention`; then, if `count > capacity`, drop
oldest until `count == capacity`.

```swift
// MARK: Formatting
enum ValueFormat {
    /// Non-finite -> "–" (U+2013). 0 -> "0". |v| < 0.05 -> "<0.1" (negative: "0").
    /// Tiers 1, k (1e3), M (1e6), B (1e9): scaled < 10 -> one decimal with a
    /// trailing ".0" dropped; otherwise rounded integer; a value that rounds up
    /// to 1000 in a tier moves to the next tier ("1k", not "1000"). POSIX
    /// formatting (no locale separators). Negative: "-" + magnitude.
    static func compact(_ v: Double) -> String
    /// compact(v) + " " + metric.unit, e.g. "669 credits/s", "9 in flight".
    static func full(_ v: Double, metric: Metric) -> String
    /// unreachable -> "off"; value nil -> "–"; else compact + menuBarSuffix.
    static func menuBar(_ v: Double?, metric: Metric, unreachable: Bool) -> String
}

// MARK: Sparkline
struct SparkPoint: Equatable, Sendable { var x: Double; var y: Double }  // y up from bottom

enum Sparkline {
    static let width = 40.0, height = 16.0, inset = 1.0
    static let window: TimeInterval = 300
    /// Drops points outside [from, to]; groups by segment (order kept, empty
    /// groups dropped). x = (t - from)/(to - from) * width.
    /// y = inset + max(v,0)/maxV * (height - 2*inset), maxV = max visible value
    /// clamped >= 0; if maxV == 0 every y = inset (flat at the bottom).
    /// to <= from -> [].
    static func geometry(_ points: [ChartPoint], from: Date, to: Date,
                         width: Double = 40, height: Double = 16,
                         inset: Double = 1) -> [[SparkPoint]]
}

// MARK: Settings
enum SettingsKey {
    static let sparklineMetric = "sparklineMetric"     // String (Metric.rawValue)
    static let pollInterval = "pollIntervalSeconds"    // Int
    static let endpointURL = "endpointURL"             // String
    static let chartRange = "chartRangeSeconds"        // Int
}

enum PollInterval {
    static let choices = [1, 2, 5, 10, 30]
    static let defaultSeconds = 5
    static func validated(_ s: Int?) -> Int            // in choices ? s : 5
    /// Line-break threshold for points(...): max(3 * interval, 10).
    static func breakThreshold(_ s: Int) -> TimeInterval
    /// Delay until the next poll: max(0, interval - elapsed).
    static func nextDelay(interval: Int, elapsed: TimeInterval) -> TimeInterval
}

enum ChartRange: Int, CaseIterable, Sendable, Identifiable {
    case fiveMinutes = 300, fifteenMinutes = 900, hour = 3600
    static let `default`: ChartRange = .fiveMinutes
    var id: Int { rawValue }
    var label: String           // "5m", "15m", "1h"
    var seconds: TimeInterval
    init(stored: Int?)          // unknown/nil -> .fiveMinutes
}

enum EndpointURL {
    static let defaultString = "http://127.0.0.1:8787/__proxy/rates"
    static var `default`: URL { get }
    /// Trims whitespace; returns a URL only if scheme is http/https (case-
    /// insensitive) and host is non-empty; otherwise nil.
    static func validate(_ s: String) -> URL?
}

struct AppSettings: Equatable, Sendable {
    var sparklineMetric: Metric
    var pollInterval: Int
    var endpoint: URL
    var chartRange: ChartRange
    static let `default`: AppSettings   // .tpsCredits, 5, EndpointURL.default, .fiveMinutes
    /// Each field falls back to its default independently when missing/invalid.
    static func load(from defaults: UserDefaults) -> AppSettings
    func save(to defaults: UserDefaults)  // writes the four keys above
}
```

## T1: Core + harness

Files: `menubar/Sources/Core/*.swift`, `menubar/Tests/*.swift`, `menubar/test.sh`.
Do not create `menubar/.gitignore` (T2 owns it); leave `menubar/build/` untracked.

`test.sh` (bash, `set -euo pipefail`, `cd "$(dirname "$0")"`):
1. Fail if `grep -lE '^import (SwiftUI|AppKit|Charts|Cocoa)' Sources/Core/*.swift` matches.
2. `mkdir -p build`, compile with the harness command above, run `build/core-tests`.
3. Exit with the test binary's status.

Harness: `Tests/Harness.swift` with `nonisolated(unsafe)` counters,
`expect(_ cond: @autoclosure () -> Bool, _ msg: String, file: #fileID, line: #line)`,
`expectEqual<T: Equatable>(_ a: T, _ b: T, ...)`,
`expectClose(_ a: Double?, _ b: Double, tol: Double = 1e-9, ...)`,
`expectThrows<E: Error & Equatable>(_ e: E, _ body: () throws -> Void, ...)`.
Each failure prints `FAIL file:line message expected X got Y`. `Tests/main.swift`
imports Foundation, calls one function per test file (`metricTests()`,
`decodeTests()`, ...), prints `N checks, F failures`, `exit(F == 0 ? 0 : 1)`.
`Tests/Fixtures.swift` holds the payload below as a raw string literal and a
helper `t(_ s: Double) -> Date` = `Date(timeIntervalSince1970: 1_800_000_000 + s)`.

Real payload fixture:
`{"window_s":60,"in_flight":9,"requests":{"started":53,"finished":57},"rps":{"started":0.883,"finished":0.95},"tokens":{"input":4723,"output":10761,"cache_write":75012,"cache_read":1582641,"credits":40130},"tps":{"input":78.7,"output":179.35,"cache_write":1250.2,"cache_read":26377.35,"credits":668.8}}`

Tests (write each red first, then make it pass):

Metric
- `allCases` is the 8 cases in declared order; rawValues as pinned; `path` of `.rpsStarted` is `["rps","started"]`, of `.inFlight` `["in_flight"]`.
- `Metric(stored: "tps.output") == .tpsOutput`; `nil`, `""`, `"bogus"` -> `.tpsCredits`.
- Every metric's `group.metrics` contains it; groups partition all 8; `MetricGroup.requests.metrics == [.rpsStarted, .rpsFinished, .inFlight]`.
- `unit`/`menuBarSuffix`/`name`/`menuTitle` spot checks for `.inFlight`, `.tpsCacheRead`, `.tpsCredits`.

Decode
- Real payload: all 8 values exact (9, 0.883, 0.95, 78.7, 179.35, 1250.2, 26377.35, 668.8).
- Pretty-printed live shape with long floats (`404.53333333333336`) decodes.
- Extra keys (top-level `"foo":1`, `"tps":{"extra":3,...}`, nested objects) ignored.
- `tps.credits` missing: 7 values, `snap[.tpsCredits] == nil`.
- `rps` object missing entirely: no rps metrics, others present.
- `"in_flight":"9"`, `"in_flight":null`, `"in_flight":true` each leave `.inFlight` absent.
- `"rps":5` (not an object) leaves both rps metrics absent.
- `{}` and `{"window_s":60}` throw `.noMetrics`; `[]` throws `.notAnObject`;
  `<html>`, empty `Data()` throw `.invalidJSON`.

SampleStore
- Empty store: `status == .waiting`, `latestValue` nil.
- Record at t(0), t(5): ascending; `latestValue(.tpsCredits)` is the last one; `status == .live(updated: t(5))`.
- Gap after snapshot: `status == .unreachable(since: t(10))`; second gap at t(15) keeps `since: t(10)`; snapshot at t(20) -> `.live(t(20))`; `latestValue` nil while last is a gap.
- Time pruning: samples at t(0), t(1), then record at t(3600) keeps t(0) (boundary kept), at t(3601) drops t(0) and keeps t(1). Gaps prune the same way.
- Capacity: `SampleStore(capacity: 5)`, 7 records -> 5 newest.
- One hour at 1 s: records t(0)...t(3999) with defaults -> `count == 3601`, first time t(399) (t(3999) - 3600, boundary kept), count never above 4000.
- Clock backwards: record t(100), t(200), then t(50) -> samples == [t(50)] only.
- `removeAll` -> empty, `.waiting`.
- `points`: values t(0),t(5),t(10), gap t(15), values t(20),t(25) -> segments [0,0,0,1,1]; gaps emit no points.
- Leading gap then values -> first point segment 0. Two consecutive gaps -> one bump.
- Snapshot lacking `.tpsCredits` at t(10) breaks credits (segment bump) but not `.tpsInput`.
- `breakAfter: 15`: points at t(0), t(5), t(60) -> segments [0,0,1].
- `since` filter excludes earlier samples; result ordered by `metrics` argument, then time; segment numbering independent per metric; `seriesKey == "tps.input#1"`.

ValueFormat.compact
- 0 -> "0"; 0.883 -> "0.9"; 0.06 -> "0.1"; 0.049 -> "<0.1"; 1.0 -> "1"; 9.96 -> "10";
  78.7 -> "79"; 999.4 -> "999"; 999.6 -> "1k"; 1000 -> "1k"; 1234 -> "1.2k";
  1250.2 -> "1.3k"; 9949 -> "9.9k"; 9960 -> "10k"; 26377.35 -> "26k";
  999_499 -> "999k"; 999_600 -> "1M"; 1_500_000 -> "1.5M"; 2.5e9 -> "2.5B";
  -3 -> "-3"; -0.01 -> "0"; `.nan`, `.infinity` -> "–".
- full: (668.8, .tpsCredits) -> "669 credits/s"; (9, .inFlight) -> "9 in flight"; (26377.35, .tpsCacheRead) -> "26k tok/s".
- menuBar: (668.8, .tpsCredits, false) -> "669/s"; (9, .inFlight, false) -> "9"; (0.883, .rpsStarted, false) -> "0.9/s"; (nil, _, false) -> "–"; (668.8, _, true) -> "off".

Sparkline.geometry (from t(0) to t(300), defaults)
- All zeros at t(0), t(150), t(300) -> one segment, ys all 1, xs 0, 20, 40.
- Values 0, 5, 10 -> ys 1, 8, 15.
- Segmented input (gap between) -> two arrays, scaled to the max across both.
- Single-point segment -> one-element array kept.
- Negative value -> y = inset; points at t(-1) and t(301) dropped.
- `to <= from` -> `[]`; empty input -> `[]`.

Settings
- `PollInterval.validated`: 1,2,5,10,30 pass; 7, 0, -5, nil -> 5. `breakThreshold`: 1 -> 10, 5 -> 15, 30 -> 90. `nextDelay(5, 0.3)` -> 4.7; `(5, 6)` -> 0.
- `ChartRange(stored: 900) == .fifteenMinutes`; 1800, nil -> `.fiveMinutes`; labels "5m","15m","1h".
- `EndpointURL.validate`: default string ok; `"  http://127.0.0.1:8787/__proxy/rates\n"` ok (trimmed); `"HTTPS://example.com/r"` ok; `""`, `"localhost:8787"`, `"ftp://x/y"`, `"http://"`, `"not a url"` -> nil.
- `AppSettings.load` on a fresh `UserDefaults(suiteName: "menubar-tests-\(UUID())")` -> `.default`; `save` then `load` round-trips non-default values; stored `pollIntervalSeconds = 7` -> 5 while other fields keep their stored values; `sparklineMetric = "foo"` -> `.tpsCredits`; `endpointURL = "nonsense"` -> default URL. Remove the suite's persistent domain afterwards.

Acceptance (T1):
```sh
menubar/test.sh                      # exit 0, last line "N checks, 0 failures"
! grep -rlE '^import (SwiftUI|AppKit|Charts|Cocoa)' menubar/Sources/Core
```

## T2: App/UI + bundle

Files: `menubar/Sources/App/*.swift`, `menubar/build.sh`, `menubar/check_bundle.sh`,
`menubar/Info.plist`, `menubar/.gitignore` (content: `build/`). Never edit Core;
if something is missing, report it rather than adding logic to App.

**Working in parallel with T1.** Until Core exists, build against a stub:
copy the pinned API above into
`/private/tmp/claude-501/-Users-victorb-code-claude-tools-ratelimit-proxy/bdc2fb53-162b-4592-910b-c2e4c3cbbdec/scratchpad/menubar/core-stub/Stub.swift`
with trivial bodies, and run `CORE_SRC=<that dir> menubar/build.sh`. `build.sh`
reads `CORE_SRC="${CORE_SRC:-Sources/Core}"`. Final acceptance uses the real Core.

`build.sh` (bash, `set -euo pipefail`, `cd "$(dirname "$0")"`): compile with the
app command above into `build/ProxyRates`; rebuild `build/ProxyRates.app` from
scratch (`Contents/MacOS/ProxyRates`, `Contents/Info.plist` copied from
`Info.plist`); `codesign --force --sign -`; print the bundle path.

`Info.plist` keys: CFBundleExecutable `ProxyRates`, CFBundleIdentifier
`local.ratelimit-proxy.menubar`, CFBundleName `ProxyRates`, CFBundleDisplayName
`Proxy Rates`, CFBundlePackageType `APPL`, CFBundleShortVersionString `0.1.0`,
CFBundleVersion `1`, LSMinimumSystemVersion `14.0`, LSUIElement `true`,
NSHighResolutionCapable `true`, NSAppTransportSecurity -> NSAllowsLocalNetworking
`true` (plain http works for loopback/local hosts only; remote endpoints need https).

`check_bundle.sh` (written FIRST, run red before `build.sh` exists, then green):
1. `build/ProxyRates.app/Contents/MacOS/ProxyRates` exists and is executable;
   `lipo -archs` prints `arm64`; `otool -l` shows `minos 14.0`.
2. `plutil -lint` passes; PlistBuddy values equal those above for
   CFBundleExecutable, CFBundleIdentifier, CFBundlePackageType,
   LSMinimumSystemVersion, LSUIElement (`true`), NSAllowsLocalNetworking (`true`).
3. `codesign --verify build/ProxyRates.app`.
4. Label render hook: run `ProxyRates --render-label <tmpdir>`; exit 0; files
   `label-live.png`, `label-offline.png`, `label-waiting.png` exist; each
   `sips -g pixelHeight` is 32 (16 pt at 2x) and width >= 80.
5. Smoke launch: start the binary directly in the background with
   `-endpointURL http://127.0.0.1:9/__proxy/rates -pollIntervalSeconds 1`
   (NSArgumentDomain overrides UserDefaults, so it never touches :8787 or the
   user's stored settings), wait 3 s, assert the pid is alive, `kill` it.
   Use `trap` to kill it on any failure.
Each step prints `ok - ...` or `not ok - ...`; exit non-zero on the first failure.

App behaviour (file split is T2's choice; suggested `ProxyRatesApp.swift`,
`Poller.swift`, `MenuBarLabel.swift`, `PanelView.swift`, `ChartsView.swift`,
`SettingsSection.swift`, `RenderLabel.swift`):

- **Entry**: `@main struct ProxyRatesApp: App`. `init()` first checks
  `CommandLine.arguments` for `--render-label <dir>`: render the three label
  states from a synthetic store (live: a 5 min ramp with one gap; offline:
  same data + trailing gap; waiting: empty) to PNG at 2x via
  `NSBitmapImageRep`, then `exit(0)`. Otherwise `Poller.shared.start()`.
  Scene: `MenuBarExtra { PanelView() } label: { MenuBarLabel() }
  .menuBarExtraStyle(.window)`. No other scenes.
- **Poller**: `@MainActor @Observable final class Poller`, `static let shared`.
  Holds `var store = SampleStore()`, `var lastError: String?`,
  `var settings: AppSettings` (from `AppSettings.load(from: .standard)`).
  `start()` cancels any running `Task` and starts a loop: note start time;
  GET `settings.endpoint` with an ephemeral `URLSession` (request timeout 3 s,
  `reloadIgnoringLocalCacheData`); non-200, transport error or decode error
  -> `store.recordGap(at: Date())`, `lastError` = short description (e.g.
  `"Could not connect"`, `"HTTP 502"`, `"Bad response"`); success ->
  `store.record(snapshot, at: Date())`, `lastError = nil`; then sleep
  `PollInterval.nextDelay(interval:elapsed:)`. Polling runs from launch,
  independent of whether the panel is open. `apply(_ new: AppSettings)`:
  saves to `.standard`; if the endpoint changed, `store.removeAll()`; if the
  endpoint or interval changed, restart the loop.
- **Menu-bar label**: a single template `NSImage` (`isTemplate = true`) drawn
  with `NSImage(size:flipped:false, drawingHandler:)`: sparkline of the selected
  metric (`store.points([m], since: now - Sparkline.window, breakAfter:
  PollInterval.breakThreshold(interval))` -> `Sparkline.geometry(..., from: now -
  300, to: now)`), stroked 1.5 pt, one-point segments drawn as a 1.5 pt dot;
  then 3 pt spacing and `ValueFormat.menuBar(...)` in
  `NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)`, vertically
  centred, height 16. Draw in black; template handling gives light/dark.
  Offline (`status` is `.unreachable`): sparkline at alpha 0.35 and text "off".
  Waiting: a 1 pt dashed baseline and "–". Shown via `Image(nsImage:)` as the
  whole label (do not rely on HStack layout inside a MenuBarExtra label).
  `now` = `Date()` at render; the label re-renders on every store change.
- **Panel** (`PanelView`, width 380): header row with status: `.live` ->
  "Live · updated HH:mm:ss" secondary; `.unreachable(since)` -> red
  `Label("Proxy unreachable since HH:mm:ss", systemImage:
  "exclamationmark.triangle.fill")` plus `lastError` and the endpoint URL in
  caption; `.waiting` -> "Waiting for first poll…". Then segmented range Picker
  over `ChartRange.allCases` bound to `@AppStorage(SettingsKey.chartRange)`,
  and a menu Picker "Menu bar" over `Metric.allCases` (`menuTitle`) bound to
  `settings.sparklineMetric` via `poller.apply`.
- **Charts**: one `Chart` per `MetricGroup.allCases` in order (Requests,
  Tokens/s, Cache read tokens/s, Credits/s), height 110, title = `group.title`
  plus latest values as `ValueFormat.full` per metric. Data:
  `store.points(for: group.metrics, since: now - range.seconds, breakAfter:
  PollInterval.breakThreshold(settings.pollInterval))`. Marks:
  `LineMark(x: .value("Time", p.time), y: .value("Value", p.value),
  series: .value("Series", p.seriesKey)).foregroundStyle(by: .value("Metric",
  p.metric.name))` plus `.interpolationMethod(.linear)`; a lone point in its
  segment also gets a `PointMark` (symbolSize 12) so it is visible.
  `.chartXScale(domain: (now - range)...now)`, linear y from 0. Colours via
  `.chartForegroundStyleScale` with system colours: Started/Input `.blue`,
  Finished/Output `.green`, In flight/Cache write `.orange`, Cache read
  `.purple`, Credits `.pink` (SwiftUI `Color.blue` etc. are the system
  semantic colours). Legend at bottom for multi-metric charts; hidden for
  single-metric charts. Empty data -> chart still shows axes and "No data".
- **Settings section** (inline in the panel, toggled by a gear button in the
  footer, so no separate window has to fight LSUIElement activation): poll
  interval Picker over `PollInterval.choices` ("1 s"...); endpoint
  `TextField` + "Apply" button enabled only when `EndpointURL.validate`
  succeeds, inline red "Enter an http(s) URL" otherwise; "Reset to defaults"
  applies `AppSettings.default`. All changes go through `poller.apply`.
- **Footer**: gear toggle, "Quit" (`NSApplication.shared.terminate(nil)`, ⌘Q).

TDD for UI: each step starts by extending `check_bundle.sh` (layout/plist
first, then `--render-label`, then smoke) and watching it fail; view code that
has no observable check must still compile with `build.sh` and keep
`check_bundle.sh` green. After a green build, launch `open
menubar/build/ProxyRates.app` once to eyeball both appearances (System
Settings > Appearance), then quit it.

Acceptance (T2, with real Core):
```sh
menubar/build.sh && menubar/check_bundle.sh   # all "ok", exit 0
menubar/test.sh                               # still green
git status --short menubar                    # build/ not listed
```

## T3: Docs (after T1 and T2 land)

Files: `menubar/README.md`, one line in `README.md`.

`menubar/README.md`: what it shows (menu-bar sparkline + value of one metric,
panel with four charts, gaps when the proxy is down, "off" in the menu bar);
requirements (macOS 14+, Command Line Tools; why `-sdk MacOSX26.5.sdk`);
build `menubar/build.sh`; run `open menubar/build/ProxyRates.app` (optionally
copy to `~/Applications`, add to Login Items); test `menubar/test.sh` and
`menubar/check_bundle.sh`; settings (sparkline metric, poll 1/2/5/10/30 s,
endpoint URL, chart range; UserDefaults domain `local.ratelimit-proxy.menubar`,
reset with `defaults delete local.ratelimit-proxy.menubar`; plain http only for
local hosts); data notes (60 s trailing window per poll, tokens land when a
request finishes so the series is spiky, cache read on its own chart because it
is ~100x the rest, history is in memory only and lasts 1 h).

`README.md`: after the "For live traffic" paragraph add one line:
`For a menu-bar view of the same numbers, see [menubar/](menubar/README.md).`

Acceptance (T3): links resolve (`ls menubar/README.md`), every command in the
README runs as written.

## Integration check (orchestrator)

```sh
menubar/test.sh && menubar/build.sh && menubar/check_bundle.sh
curl -s http://127.0.0.1:8787/__proxy/rates >/dev/null   # proxy untouched, still up
```
Then the user eyeballs the live app in light and dark mode.
